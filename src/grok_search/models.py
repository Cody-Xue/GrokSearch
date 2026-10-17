"""Model names and the proxy's model list.

OpenAI-compatible proxies such as CLIProxyAPI (CPA) accept a thinking/reasoning
suffix in parentheses after the model name -- "grok-4(high)", "gemini(8192)",
"model(-1)", "provider://model(auto)" -- and strip it before routing, so
/v1/models lists only base names. web_search(model=...) therefore validates the
*base* name against that list and still sends the full name upstream.

The list is cached per (url, key) for GROK_MODELS_CACHE_TTL_S seconds. Failed or
empty fetches are never cached; a failure only suppresses refetching for
FAILURE_BACKOFF_S, and that backoff also applies to the refresh a cache miss
triggers, so a dead /models endpoint costs one timeout per backoff window rather
than one per waiting call. A name missing from a list older than
MISS_REFRESH_MIN_AGE_S triggers one refresh before it is rejected; concurrent
misses share that refresh. A stale list never rejects a name, and
get_config_info refreshes the cache whenever it runs.

On OpenRouter (GROK_API_URL contains "openrouter") a model needs the ":online"
variant to search the web: the suffix is ignored for validation and added to the
name that is sent, as it is for the default model and switch_model.

/models also lists models that cannot answer /chat/completions (text-to-speech,
voice, transcription, image/video generation, embeddings, moderation, realtime,
rerankers). Gateways such as CPA carry no capability field, so these are recognised
by name (non_chat_kind): a per-call or switch_model name of that kind is rejected
before anything is sent, and such models are never suggested. When the gateway does
declare what a model outputs (OpenRouter's architecture.output_modalities /
architecture.modality, remembered per API URL by remember_capabilities), that
declaration wins: openai/gpt-5-image or openai/gpt-audio answer chat completions
with text on OpenRouter and are accepted, a model declared without text output is
rejected whatever its name. The configured default (GROK_MODEL / config file) is not
blocked; get_config_info warns about it instead.
"""
import asyncio
import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import apply_online_suffix, config, uses_online_suffix
from .errors import ProviderError, classify_exception

# Values CPA accepts inside the parentheses besides a numeric token budget (case-insensitive).
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "auto", "none")
# A cached list younger than this is trusted for a miss instead of being refetched.
MISS_REFRESH_MIN_AGE_S = 30.0
# After a failed fetch, callers within this window get the failure without refetching.
FAILURE_BACKOFF_S = 15.0

# Longer names are rejected before anything is sent or saved.
MAX_MODEL_NAME_LEN = 200
_ONLINE = ":online"

_SUFFIX_RE = re.compile(r"^(?P<base>.*?[^\s(])\((?P<value>[^()]*)\)$")
_BUDGET_RE = re.compile(r"^-?\d+$")

# Name tokens of models that cannot serve /chat/completions, by kind. A token only counts as a whole
# name part (between "-", "_", "/", ".", ":" or the ends; digits may follow, as in "tts1"), so
# "grok-2-vision-1212", "grok-code-fast-1" and "deepseek-chat" stay chat models. Vision models chat
# and are not listed.
_NON_CHAT_TOKENS = {
    "audio/speech": ("tts", "voice", "audio", "speech", "whisper", "transcribe", "transcription"),
    "image generation": ("image", "images", "imagine", "imagen", "dall-e", "dalle"),
    "video generation": ("video", "sora", "veo"),
    "embedding": ("embed", "embeds", "embedding", "embeddings"),
    "moderation": ("moderation",),
    "realtime": ("realtime",),
    "rerank": ("rerank", "reranker"),
}
_NON_CHAT_KIND = {token: kind for kind, tokens in _NON_CHAT_TOKENS.items() for token in tokens}
_NON_CHAT_RE = re.compile(
    r"(?:^|[-_/.:])(" + "|".join(sorted(map(re.escape, _NON_CHAT_KIND), key=len, reverse=True)) + r")(?=$|[-_/.:\d])",
    re.IGNORECASE,
)
_FAMILY_RE = re.compile(r"[a-z]+")


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


def model_name_problem(name: str) -> str:
    """Why a (stripped) model name can never be valid; empty when it is acceptable as a name."""
    if not name:
        return "model name is empty"
    if len(name) > MAX_MODEL_NAME_LEN:
        return f"model name is {len(name)} characters long (limit {MAX_MODEL_NAME_LEN})"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "model name contains control characters"
    return ""


def _non_chat_token(name: str) -> str:
    match = _NON_CHAT_RE.search(name or "")
    return match.group(1) if match else ""


def non_chat_kind(name: str) -> str:
    """The kind of non-chat model a name denotes ("audio/speech", "image generation", ...); "" for a chat model.

    Name-based because /models carries no capability metadata.
    """
    token = _non_chat_token(name)
    return _NON_CHAT_KIND[token.lower()] if token else ""


# Per API URL: model id -> whether its /models entry declares text output (only ids that declare anything).
_DECLARED_TEXT_OUTPUT: dict[str, dict[str, bool]] = {}


def declared_text_output(item) -> Optional[bool]:
    """Whether a /models entry says the model outputs text (OpenRouter: architecture.output_modalities, or
    architecture.modality "text+image->text+image"); None when it says nothing."""
    architecture = item.get("architecture") if isinstance(item, dict) else None
    if not isinstance(architecture, dict):
        return None
    outputs = architecture.get("output_modalities")
    if isinstance(outputs, list) and outputs:
        return any(isinstance(o, str) and o.strip().lower() == "text" for o in outputs)
    modality = architecture.get("modality")
    if isinstance(modality, str) and "->" in modality:
        return "text" in [part.strip().lower() for part in modality.split("->", 1)[1].split("+")]
    return None


def remember_capabilities(api_url: str, items) -> None:
    """Store what the /models entries of api_url declare about text output (see declared_text_output)."""
    declared = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            value = declared_text_output(item)
            if value is not None:
                declared[item["id"]] = value
    if len(_DECLARED_TEXT_OUTPUT) > 16:
        _DECLARED_TEXT_OUTPUT.clear()
    _DECLARED_TEXT_OUTPUT[api_url] = declared


def model_kind(name: str, api_url: str = "") -> str:
    """non_chat_kind, overridden by what the gateway at api_url declared for this model: "" when it
    declared text output, "no text output" when it declared other output only."""
    declared = _DECLARED_TEXT_OUTPUT.get(api_url, {})
    raw = (name or "").strip()
    for form in dict.fromkeys((raw, raw[: -len(_ONLINE)] if raw.endswith(_ONLINE) else raw)):
        if form in declared:
            return "" if declared[form] else "no text output"
    return non_chat_kind(name)


def _model_tail(name: str) -> str:
    """The model part of "provider://vendor/model": what families and prefixes are compared on."""
    return (name or "").lower().split("://")[-1].rsplit("/", 1)[-1]


def _model_family(name: str) -> str:
    match = _FAMILY_RE.match(_model_tail(name))
    return match.group(0) if match else ""


def suggest_models(base: str, models, n: int = 5, cutoff: float = 0.4) -> list[str]:
    """Chat models close to base, best first; non-chat models (non_chat_kind) are never suggested.

    Ranked by: same family as base ("grok", "gpt", ...), a name that extends base ("grok-4.20" ->
    "grok-4.20-multi-agent-0309"), string similarity, then the shorter name. Candidates of the same
    family or that extend base are kept at any similarity; others need at least `cutoff`.
    """
    typed = (base or "").strip().lower()
    if not typed:
        return []
    family = _model_family(typed)
    ranked = []
    for candidate in dict.fromkeys(m for m in models or () if isinstance(m, str) and m):
        if non_chat_kind(candidate) or candidate.lower() == typed:
            continue
        lowered = candidate.lower()
        same_family = bool(family) and _model_family(lowered) == family
        extends = lowered.startswith(typed) or _model_tail(lowered).startswith(_model_tail(typed))
        ratio = difflib.SequenceMatcher(None, typed, lowered).ratio()
        if same_family or extends or ratio >= cutoff:
            ranked.append((not same_family, not extends, -ratio, len(candidate), candidate))
    ranked.sort()
    return [entry[-1] for entry in ranked[:n]]


def routing_base(model: str) -> str:
    """The model a CPA-style proxy actually routes to: thinking suffix and ":online" removed.

    Used as the circuit-breaker key, so "grok-4(high)", "grok-4(low)" and "grok-4(8192)"
    share one breaker state. Parentheses that are not a thinking suffix are kept.
    """
    raw = (model or "").strip()
    if raw.endswith(":online"):
        raw = raw[: -len(":online")]
    name = parse_model_name(raw)
    return name.base if name.suffix_kind in ("level", "budget", "empty") else name.raw


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
    items = (data or {}).get("data", []) or [] if isinstance(data, dict) else []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    if models:
        remember_capabilities(api_url, items)
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

    async def get(self, api_url: str, api_key: str, *, refresh_older_than: Optional[float] = None) -> ModelList:
        """Cached list, fetched when expired.

        refresh_older_than: also refetch a list that is still within its TTL when it is at least
        this old (used after a cache miss). A list younger than that is returned as-is, so callers
        that queued behind a refresh reuse its result. A failure within FAILURE_BACKOFF_S is
        returned without fetching, for refreshes too.
        """
        key = (api_url, api_key)
        async with self._lock(key):
            cached = self._entries.get(key)
            now = self._clock()
            if cached:
                age = now - cached[0]
                limit = config.models_cache_ttl_s if refresh_older_than is None else min(refresh_older_than, config.models_cache_ttl_s)
                if age < limit:
                    return ModelList(list(cached[1]), cached[0], from_cache=True)
            failure = self._failures.get(key)
            if failure and now - failure[0] < FAILURE_BACKOFF_S:
                return self._failed(cached, failure[1])
            try:
                models = await fetch_available_models(api_url, api_key)
            except Exception as e:
                error = classify_exception("models", e)
                self._failures[key] = (self._clock(), error)  # backoff starts when the attempt ended
                return self._failed(cached, error)
            done = self._clock()
            if not models:
                error = ProviderError("models", "empty_content", "/models returned no model IDs")
                self._failures[key] = (done, error)
                return self._failed(cached, error)
            self._entries[key] = (done, list(models))
            self._failures.pop(key, None)
            return ModelList(list(models), done)

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
    reason: str = ""  # machine-readable cause of an invalid result, e.g. "not_chat_model"

    def to_dict(self) -> dict:
        data = {"status": self.status, "model": self.model}
        if self.base and self.base != self.model:
            data["base"] = self.base
        if self.detail:
            data["detail"] = self.detail
        if self.reason:
            data["reason"] = self.reason
        if self.suggestions:
            data["suggestions"] = self.suggestions
        return data


async def check_model(requested: str, *, default_model: str, api_url: str, api_key: str, cache: ModelListCache) -> ModelCheck:
    """Decide whether a per-call model name may be sent upstream; the full name is always kept.

    ModelCheck.model is the name to send: on OpenRouter it carries ":online" like the default model.
    """
    requested = (requested or "").strip()
    problem = model_name_problem(requested)
    if problem:
        return ModelCheck(False, requested, "invalid", detail=problem, reason="bad_name")
    lookup = requested[: -len(_ONLINE)] if uses_online_suffix(api_url) and requested.endswith(_ONLINE) else requested
    send = apply_online_suffix(lookup, api_url)
    default_raw = default_model[: -len(_ONLINE)] if default_model.endswith(_ONLINE) else default_model
    if lookup in (default_model, default_raw):
        return ModelCheck(True, send, "default")

    name = parse_model_name(lookup)
    if name.suffix_kind != "unknown" and name.base == parse_model_name(default_raw).base:
        return ModelCheck(True, send, "default_base", name.base)

    listing = await cache.get(api_url, api_key)
    kind = model_kind(name.base, api_url)
    if kind:
        # Listed or not, /chat/completions would refuse it (e.g. HTTP 400 "only supported on /v1/audio/speech").
        why = ("the proxy's /models entry declares no text output" if kind == "no text output"
               else f"{kind} model, name contains '{_non_chat_token(name.base)}'")
        return ModelCheck(
            False, requested, "invalid", name.base,
            f"'{name.base}' is not a chat/search model ({why}); web_search needs a chat model",
            suggest_models(name.base, listing.models), reason="not_chat_model",
        )

    def listed(models) -> bool:
        return bool(models) and (lookup in models or base_is_listed(name.base, models))

    if name.suffix_kind == "unknown":
        if listing.models and lookup in listing.models:
            return ModelCheck(True, send, "listed", name.base)
        detail = (f"unsupported thinking suffix '({name.suffix})': use a token budget such as (8192) or one of "
                  + ", ".join(THINKING_LEVELS))
        suggestions: list[str] = []
        # Both problems at once: "grok-99-fake(ultra)" used to be refused for its suffix alone, and only the
        # second attempt revealed that the model does not exist either.
        if listing.models and not listing.stale and not base_is_listed(name.base, listing.models):
            detail += f"; and '{name.base}' is not in the proxy's /models list"
            suggestions = suggest_models(name.base, listing.models)
        return ModelCheck(False, requested, "invalid", name.base, detail, suggestions, reason="unsupported_suffix")
    if listed(listing.models):
        return ModelCheck(True, send, "listed", name.base)
    if listing.models and not listing.stale and listing.from_cache and cache.now() - listing.fetched_at >= MISS_REFRESH_MIN_AGE_S:
        # One refresh for a miss; it honours the failure backoff and concurrent misses share it.
        listing = await cache.get(api_url, api_key, refresh_older_than=MISS_REFRESH_MIN_AGE_S)
        if not listing.stale and listed(listing.models):
            return ModelCheck(True, send, "listed_after_refresh", name.base)
    if not listing.models or listing.stale:
        reason = listing.error.summary() if listing.error else "model list unavailable"
        return ModelCheck(True, send, "unverified", name.base, f"could not validate against /models ({reason}); sent as-is")
    return ModelCheck(False, requested, "invalid", name.base, f"'{name.base}' is not in the proxy's /models list",
                      suggest_models(name.base, listing.models), reason="not_listed")
