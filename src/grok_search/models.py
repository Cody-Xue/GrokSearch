"""Model names and the proxy's model list.

OpenAI-compatible proxies such as CLIProxyAPI (CPA) accept a thinking/reasoning
suffix in parentheses after the model name -- "grok-4(high)", "gemini(8192)",
"model(-1)", "provider://model(auto)" -- and strip it before routing, so
/v1/models lists only base names. web_search(model=...) therefore validates the
*base* name against that list and still sends the full name upstream.

The list is cached per (url, key) for GROK_MODELS_CACHE_TTL_S seconds. Failed or
empty fetches are never cached (a failure only suppresses refetching for a short
backoff so a burst of parallel calls does not queue behind a dead endpoint), a
name missing from a cached list triggers one forced refresh before it is
rejected, a stale list never rejects a name, and get_config_info refreshes the
cache whenever it runs.
"""
import asyncio
import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import config
from .errors import ProviderError, classify_exception

# Values CPA accepts inside the parentheses besides a numeric token budget (case-insensitive).
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "auto", "none")
# A cached list younger than this is trusted for a miss instead of being refetched.
MISS_REFRESH_MIN_AGE_S = 30.0
# After a failed fetch, callers within this window get the failure without refetching.
FAILURE_BACKOFF_S = 15.0

_SUFFIX_RE = re.compile(r"^(?P<base>.*?[^\s(])\((?P<value>[^()]*)\)$")
_BUDGET_RE = re.compile(r"^-?\d+$")


@dataclass
class ModelName:
    raw: str
    base: str
    suffix: Optional[str] = None  # text inside the parentheses; None when there is no suffix
    suffix_kind: str = "none"  # none | level | budget | empty | unknown


def parse_model_name(model: str) -> ModelName:
    raw = (model or "").strip()
    match = _SUFFIX_RE.match(raw)
    if not match:
        return ModelName(raw, raw)
    value = match.group("value").strip()
    if not value:
        kind = "empty"
    elif _BUDGET_RE.match(value):
        kind = "budget"
    elif value.lower() in THINKING_LEVELS:
        kind = "level"
    else:
        kind = "unknown"
    return ModelName(raw, match.group("base"), value, kind)


def base_is_listed(base: str, models) -> bool:
    listed = set(models or ())
    forms = [base]
    if "://" in base:  # provider://model may be listed with or without the provider prefix
        forms.append(base.split("://", 1)[1])
    return any(form in listed for form in forms)


async def fetch_available_models(api_url: str, api_key: str, timeout_s: float = 10.0) -> list[str]:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(
            f"{api_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    models: list[str] = []
    for item in (data or {}).get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


@dataclass
class ModelList:
    models: list[str] = field(default_factory=list)
    fetched_at: float = 0.0
    from_cache: bool = False
    stale: bool = False  # True when a refresh failed and the last good list is returned
    error: Optional[ProviderError] = None


class ModelListCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._failures: dict[tuple[str, str], tuple[float, ProviderError]] = {}
        self._locks: dict[tuple[str, str], tuple[object, asyncio.Lock]] = {}

    def now(self) -> float:
        return self._clock()

    def _lock(self, key) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        held = self._locks.get(key)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Lock())
            self._locks[key] = held
        return held[1]

    def put(self, api_url: str, api_key: str, models: list[str]) -> None:
        """Store a list fetched elsewhere (get_config_info); empty lists are ignored."""
        if models:
            key = (api_url, api_key)
            self._entries[key] = (self._clock(), list(models))
            self._failures.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()
        self._failures.clear()

    async def get(self, api_url: str, api_key: str, *, force_refresh: bool = False) -> ModelList:
        key = (api_url, api_key)
        async with self._lock(key):
            cached = self._entries.get(key)
            now = self._clock()
            if cached and not force_refresh and now - cached[0] < config.models_cache_ttl_s:
                return ModelList(list(cached[1]), cached[0], from_cache=True)
            failure = self._failures.get(key)
            if failure and not force_refresh and now - failure[0] < FAILURE_BACKOFF_S:
                return self._failed(cached, failure[1])
            try:
                models = await fetch_available_models(api_url, api_key)
            except Exception as e:
                error = classify_exception("models", e)
                self._failures[key] = (now, error)
                return self._failed(cached, error)
            if not models:
                error = ProviderError("models", "empty_content", "/models returned no model IDs")
                self._failures[key] = (now, error)
                return self._failed(cached, error)
            self._entries[key] = (now, list(models))
            self._failures.pop(key, None)
            return ModelList(list(models), now)

    @staticmethod
    def _failed(cached, error: ProviderError) -> ModelList:
        if cached:
            return ModelList(list(cached[1]), cached[0], from_cache=True, stale=True, error=error)
        return ModelList(error=error)


@dataclass
class ModelCheck:
    ok: bool
    model: str  # the full name to send upstream
    status: str  # default | default_base | listed | listed_after_refresh | unverified | invalid
    base: str = ""
    detail: str = ""
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {"status": self.status, "model": self.model}
        if self.base and self.base != self.model:
            data["base"] = self.base
        if self.detail:
            data["detail"] = self.detail
        if self.suggestions:
            data["suggestions"] = self.suggestions
        return data


async def check_model(requested: str, *, default_model: str, api_url: str, api_key: str, cache: ModelListCache) -> ModelCheck:
    """Decide whether a per-call model name may be sent upstream; the full name is always kept."""
    requested = (requested or "").strip()
    default_raw = default_model[: -len(":online")] if default_model.endswith(":online") else default_model
    if requested in (default_model, default_raw):
        return ModelCheck(True, requested, "default")

    name = parse_model_name(requested)
    if name.suffix_kind != "unknown" and name.base == parse_model_name(default_raw).base:
        return ModelCheck(True, requested, "default_base", name.base)

    def listed(models) -> bool:
        return bool(models) and (requested in models or base_is_listed(name.base, models))

    listing = await cache.get(api_url, api_key)
    if name.suffix_kind == "unknown":
        if listing.models and requested in listing.models:
            return ModelCheck(True, requested, "listed", name.base)
        return ModelCheck(
            False, requested, "invalid", name.base,
            f"unsupported thinking suffix '({name.suffix})': use a token budget such as (8192) or one of "
            + ", ".join(THINKING_LEVELS),
        )
    if listed(listing.models):
        return ModelCheck(True, requested, "listed", name.base)
    if listing.models and not listing.stale and listing.from_cache and cache.now() - listing.fetched_at >= MISS_REFRESH_MIN_AGE_S:
        listing = await cache.get(api_url, api_key, force_refresh=True)
        if not listing.stale and listed(listing.models):
            return ModelCheck(True, requested, "listed_after_refresh", name.base)
    if not listing.models or listing.stale:
        reason = listing.error.summary() if listing.error else "model list unavailable"
        return ModelCheck(True, requested, "unverified", name.base, f"could not validate against /models ({reason}); sent as-is")
    suggestions = difflib.get_close_matches(name.base, listing.models, n=5, cutoff=0.4)
    return ModelCheck(False, requested, "invalid", name.base, f"'{name.base}' is not in the proxy's /models list", suggestions)
