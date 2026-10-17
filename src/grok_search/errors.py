"""Structured, redacted errors for the HTTP backends (Tavily, Firecrawl, arXiv, /models).

Every backend failure becomes a ProviderError with one category from a small,
fixed vocabulary, so callers can tell configuration problems (auth, quota,
local proxy environment) from transient ones (rate limit, timeout, upstream
5xx) and from pages that simply had no extractable content. Details are
truncated and scrubbed of API keys before they leave the process.
"""
import html
import json
import os
import re
from typing import Optional

import httpx

# category -> short meaning. The category is the stable, machine-readable part.
CATEGORIES = {
    "auth": "API key missing, invalid or not permitted",
    "quota": "plan or credit limit reached",
    "rate_limit": "rate limited, retry later",
    "timeout": "no answer in time",
    "proxy_config": "local proxy environment (HTTP(S)_PROXY / NO_PROXY) is invalid",
    "network": "connection failed (DNS, TLS or connection refused)",
    "upstream_error": "provider-side server error",
    "bad_request": "request rejected (for example an unsupported or invalid URL)",
    "http_error": "unexpected HTTP status",
    "extract_failed": "the provider could not extract this page",
    "empty_content": "the provider answered without content",
    "parse_error": "unexpected response body",
    "config": "invalid endpoint configuration",
    "disabled": "disabled by configuration",
    "not_configured": "API key not configured",
    "unknown": "unexpected error",
}

RETRYABLE_CATEGORIES = frozenset({"rate_limit", "timeout", "upstream_error", "network"})
SKIP_CATEGORIES = frozenset({"disabled", "not_configured"})

_SECRET_ENV = ("GROK_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY")
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1***"),
    # Authorization values without "Bearer" (Basic credentials, bare keys in echoed JSON headers).
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:basic\s+)?)(?!bearer\b)[^\s\"',;}]{6,}"), r"\1***"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;}]+"), r"\1***"),
    (re.compile(r"(?i)([?&]key=)[^\s\"'&#]+"), r"\1***"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\b(?:tvly|fc|sk|xai)-[A-Za-z0-9_-]{6,}"), "***"),
)


def redact(text, limit: int = 300) -> str:
    """Remove known secrets from the whole text, then collapse whitespace and truncate.

    Callers must pass the full text: truncating first could cut a key in two, and the
    remaining fragment would no longer match the configured value.
    """
    out = str(text or "")
    for name in _SECRET_ENV:
        value = (os.getenv(name) or "").strip()
        if len(value) >= 6:
            out = out.replace(value, "***")
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    out = " ".join(out.split())
    return out if len(out) <= limit else out[:limit] + "…"


class ProviderError(Exception):
    """A classified backend failure; str() is a one-line, redacted summary."""

    def __init__(
        self,
        provider: str,
        category: str,
        detail: str = "",
        *,
        status: Optional[int] = None,
        retryable: Optional[bool] = None,
    ):
        self.provider = provider
        self.category = category if category in CATEGORIES else "unknown"
        self.status = status
        self.detail = redact(detail)
        self.retryable = (self.category in RETRYABLE_CATEGORIES) if retryable is None else retryable
        super().__init__(self.summary())

    @property
    def skipped(self) -> bool:
        """True when the provider was not tried at all (disabled or no key)."""
        return self.category in SKIP_CATEGORIES

    def summary(self) -> str:
        text = f"{self.provider}: {self.category}"
        if self.status is not None:
            text += f" (HTTP {self.status})"
        if self.detail:
            text += f" — {self.detail}"
        return text

    def to_dict(self) -> dict:
        data = {"provider": self.provider, "category": self.category}
        if self.status is not None:
            data["status"] = self.status
        if self.detail:
            data["detail"] = self.detail
        return data


# Cloudflare edge errors (the gateway is behind Cloudflare). Their bodies are HTML pages whose
# first few hundred characters are boilerplate, so the code is named instead of quoting the page.
CLOUDFLARE_ERRORS = {
    520: "origin returned an unknown error",
    521: "origin refused the connection (gateway down)",
    522: "connection to origin timed out",
    523: "origin unreachable",
    524: "origin timed out (no response within ~100 s)",
    525: "TLS handshake with origin failed",
    526: "invalid TLS certificate at origin",
    530: "origin or tunnel error",
}
_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_CF_PLAIN = re.compile(r"^\s*error code:\s*5\d\d\b", re.IGNORECASE)


def summarize_error_body(text, *, status: Optional[int] = None, content_type: str = "", server: str = "", limit: int = 300) -> str:
    """Short, redacted error text: Cloudflare edge errors by name, HTML pages by their <title>."""
    raw = str(text or "")
    head = raw[:4096]
    looks_html = "html" in (content_type or "").lower() or head.lstrip()[:1] == "<"
    if status in CLOUDFLARE_ERRORS and (
        "cloudflare" in (server or "").lower() or "cloudflare" in raw.lower() or _CF_PLAIN.match(head)
    ):
        return f"Cloudflare {status}: {CLOUDFLARE_ERRORS[status]}"
    if looks_html:
        match = _HTML_TITLE.search(raw)
        title = html.unescape(match.group(1)) if match else ""
        return redact(title, limit) or "HTML error page"
    return redact(raw, limit)


def response_error_message(response) -> str:
    """Best-effort error text from Tavily, Firecrawl or OpenAI-style JSON bodies."""
    try:
        data = response.json()
    except Exception:
        try:
            text = response.text
        except Exception:
            return ""
        headers = getattr(response, "headers", None) or {}
        content_type = headers.get("content-type", "")
        status = getattr(response, "status_code", None)
        if "html" in content_type.lower() or text.lstrip()[:1] == "<" or status in CLOUDFLARE_ERRORS:
            return summarize_error_body(text, status=status, content_type=content_type, server=headers.get("server", ""))
        return text
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict) and detail.get("error"):
            return str(detail["error"])
        if isinstance(detail, str) and detail:
            return detail
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or json.dumps(error, ensure_ascii=False))
        if error:
            return str(error)
        if data.get("message"):
            return str(data["message"])
    return json.dumps(data, ensure_ascii=False) if data else ""


def category_for_status(status: int, message: str = "") -> str:
    lowered = (message or "").lower()
    if status == 401:
        return "auth"
    if status == 403:
        # Tavily answers 403 for "URL is not supported"; everyone else uses it for permissions.
        return "bad_request" if ("not supported" in lowered or "unsupported" in lowered) else "auth"
    if status in (402, 432, 433):
        return "quota"
    if status == 429:
        return "rate_limit"
    if status in (408, 504, 522, 524):
        return "timeout"
    if 400 <= status < 500:
        return "bad_request"
    if status >= 500:
        return "upstream_error"
    return "http_error"


def no_proxy_has_bracketed_ipv6() -> bool:
    return any("[" in (os.getenv(name) or "") for name in ("NO_PROXY", "no_proxy"))


def _looks_like_proxy_env_problem(text: str) -> bool:
    lowered = text.lower()
    return "proxy" in lowered or ("invalid port" in lowered and no_proxy_has_bracketed_ipv6())


_TRANSPORT_HINTS = (
    (httpx.ConnectTimeout, "connection timed out"),
    (httpx.ReadTimeout, "no data received within the read timeout"),
    (httpx.WriteTimeout, "timed out while sending the request"),
    (httpx.PoolTimeout, "timed out waiting for a free connection"),
    (httpx.ProxyError, "the proxy refused or failed the tunnel"),
    # mihomo/Clash close the CONNECT tunnel without a reason when the upstream is unreachable.
    (httpx.ConnectError, "proxy tunnel closed or upstream unreachable"),
    (httpx.ReadError, "connection dropped while reading the response"),
    (httpx.WriteError, "connection dropped while sending the request"),
    (httpx.RemoteProtocolError, "upstream closed the connection mid-response"),
)


def describe_transport_error(exc: BaseException) -> str:
    """One-line description of a transport failure; httpx often leaves str(exc) empty."""
    name = type(exc).__name__
    text = str(exc).strip()
    if text:
        return f"{name}: {text}"
    for cls, hint in _TRANSPORT_HINTS:
        if isinstance(exc, cls):
            return f"{hint} ({name})"
    return name


def classify_exception(provider: str, exc: BaseException) -> ProviderError:
    """Map an exception raised while talking to a provider onto a ProviderError."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        message = response_error_message(exc.response)
        return ProviderError(provider, category_for_status(status, message), message, status=status)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(provider, "timeout", describe_transport_error(exc))
    if isinstance(exc, httpx.ProxyError):
        return ProviderError(provider, "proxy_config", describe_transport_error(exc))
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        text = str(exc) or type(exc).__name__
        if _looks_like_proxy_env_problem(text):
            return ProviderError(
                provider,
                "proxy_config",
                text + " (check HTTP(S)_PROXY / NO_PROXY; a bracketed IPv6 entry such as [::1] in NO_PROXY breaks httpx)",
            )
        return ProviderError(provider, "config", text)
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return ProviderError(provider, "network", describe_transport_error(exc))
    if isinstance(exc, json.JSONDecodeError):
        return ProviderError(provider, "parse_error", f"invalid JSON: {exc}")
    return ProviderError(provider, "unknown", f"{type(exc).__name__}: {exc}")


_BRACKETED_V6 = re.compile(r"^\[([0-9A-Fa-f:.]+(?:%[\w.-]+)?)\](?::\d+)?$")
_V6_CIDR = re.compile(r"^([0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*)/\d{1,3}$")


def sanitize_no_proxy_env() -> list[str]:
    """Rewrite NO_PROXY entries that httpx cannot parse.

    httpx 0.28 turns a bracketed IPv6 entry such as "[::1]" (written by Clash Verge,
    mihomo and similar tools) into the pattern "all://*[::1]" and raises
    InvalidURL ("Invalid port: ':1]'") whenever a client is created, which takes
    every tool down. IPv6 CIDR entries fail the same way. Such entries are
    rewritten to the bare address ("[::1]" -> "::1", "::1/128" -> "::1").
    Returns the names of the variables that were changed.
    """
    changed: list[str] = []
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw or ("[" not in raw and "/" not in raw):
            continue
        items: list[str] = []
        for item in raw.split(","):
            item = item.strip()
            match = _BRACKETED_V6.match(item) or _V6_CIDR.match(item)
            items.append(match.group(1) if match else item)
        value = ",".join(dict.fromkeys(item for item in items if item))
        if value != raw:
            os.environ[name] = value
            changed.append(name)
    return changed
