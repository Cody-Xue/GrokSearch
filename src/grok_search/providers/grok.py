import asyncio
import codecs
import httpx
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.stop import stop_base
from tenacity.wait import wait_base
from .base import BaseSearchProvider
from ..errors import classify_exception, describe_transport_error, redact, summarize_error_body
from ..models import routing_base
from ..utils import build_search_prompt
from ..logger import log_info, logger
from ..config import DEFAULT_MODEL, config
from ..throttle import breaker, get_semaphore, BreakerOpen


# The time block must be plain ASCII apart from our own weekday name. On Windows CPython builds the name of
# datetime.now().astimezone() from the C runtime's strftime("%Z"), which converts the localized display
# name ("中国标准时间") through the ANSI code page: a code page that cannot encode it yields "??????", and
# an LC_CTYPE mismatch yields mojibake or lone surrogates. The numeric UTC offset is derived from
# localtime/gmtime and is reliable, so the block carries the offset plus an IANA name only when one can be
# read from the configuration (TZ, /etc/localtime, /etc/timezone) and agrees with that offset.
_LOCALTIME_LINK = "/etc/localtime"
_TIMEZONE_FILE = "/etc/timezone"
_IANA_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}")


def _format_utc_offset(offset) -> str:
    """'UTC+08:00', 'UTC-03:30', 'UTC+05:45'; a missing offset counts as UTC."""
    minutes = round(offset.total_seconds() / 60) if offset is not None else 0
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{mins:02d}"


def _iana_candidates(local_now: datetime) -> list[str]:
    candidates = []
    key = getattr(local_now.tzinfo, "key", None)
    if isinstance(key, str):
        candidates.append(key)
    tz_env = (os.environ.get("TZ") or "").strip().lstrip(":")
    if tz_env:
        candidates.append(tz_env.split("zoneinfo/")[-1] if "zoneinfo/" in tz_env else tz_env)
    try:
        target = os.readlink(_LOCALTIME_LINK)
        if "zoneinfo/" in target:
            candidates.append(target.rsplit("zoneinfo/", 1)[1])
    except (OSError, ValueError):
        pass
    try:
        with open(_TIMEZONE_FILE, encoding="ascii") as f:
            candidates.append(f.read(100).strip())
    except (OSError, ValueError):
        pass
    return candidates


def _iana_zone_name(local_now: datetime) -> str:
    """An ASCII IANA key for the local zone whose offset right now equals the local offset, or ""."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python < 3.9
        return ""
    offset = local_now.utcoffset()
    for key in _iana_candidates(local_now):
        if not key.isascii() or not _IANA_KEY_RE.fullmatch(key):
            continue
        try:
            if local_now.astimezone(ZoneInfo(key)).utcoffset() == offset:
                return key
        except Exception:
            # Unknown key, no tz database (Windows without the tzdata package), unreadable file.
            continue
    return ""


def get_local_time_info() -> str:
    """获取本地时间信息，注入到每次搜索的用户消息前（时区用 ASCII 的 UTC 偏移 + 可确定时的 IANA 名称）"""
    try:
        local_now = datetime.now().astimezone()
        offset = local_now.utcoffset()
    except Exception:
        local_now = datetime.now(timezone.utc)
        offset = None
    zone = _format_utc_offset(offset)
    try:
        iana = _iana_zone_name(local_now) if offset is not None else ""
    except Exception:
        iana = ""
    if iana:
        zone += f" ({iana})"

    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    # Digits formatted here rather than by the C runtime's strftime, which is locale-dependent on Windows.
    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.year:04d}-{local_now.month:02d}-{local_now.day:02d} ({weekday})\n"
        f"- Time: {local_now.hour:02d}:{local_now.minute:02d}:{local_now.second:02d}\n"
        f"- Timezone: {zone}\n"
    )


RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# Cloudflare edge errors from a Cloudflare-fronted gateway. 520/521/523/530 mean the request
# failed before or outside the search, so one more attempt is cheap. 522 and 524 are timeouts;
# a 524 means the origin already worked for ~100 s, and retrying would re-run the whole search.
_EDGE_RETRY_ONCE = {520, 521, 523, 530}
_TIMEOUT_STATUS_HINTS = {
    522: " - the gateway could not be reached in time",
    524: " - the search ran longer than the gateway's proxy allows; narrow the query or use a lighter model (not retried automatically)",
}

# grok2api-style proxies may answer a failed request with HTTP 200 and carry
# the error inside the SSE stream ("event: error" frames or an `error` object
# in a data payload). Classification decides whether a retry can help:
# upstream/rate-limit errors may succeed on another pooled account, while
# auth/parameter errors never will.
_RETRYABLE_ERROR_TYPES = ("upstream_error", "rate_limit", "server_error", "timeout", "overloaded")
_NON_RETRYABLE_ERROR_CODES = (
    "invalid_request_error", "auth_error", "authentication_error",
    "permission_denied", "model_not_found", "not_found_error",
    # Responses API: the prompt was refused by policy; resending it can get the model blocked on the gateway.
    "invalid_prompt",
)
_NON_RETRYABLE_ERROR_MARKERS = (
    "unauthorized", "invalid_api_key", "invalid api key",
    "invalid model", "model_not_found",
)
# An exhausted pool: no retry can help until the pool recovers, so the breaker opens at once.
_EXHAUSTED_MARKERS = (
    "no available accounts", "no accounts available", "account pool exhausted",
    # CLIProxyAPI (CPA): every credential for the model is blocked or cooling down.
    "auth_unavailable", "no auth available", "model_cooldown", "cooling down",
    # xAI free-tier credentials (CPA passes this 429 through once, then cools the model down).
    "free-usage-exhausted",
)
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "too many requests", "ratelimit")
# Gateway answers that no retry can change: no usable credential for the model at all
# (CPA "auth_not_found", terminal "authentication_error"), or an explicit "retryable": false.
_AUTH_FAILURE_KINDS = ("authentication_error", "upstream_authentication_required")
_AUTH_FAILURE_MARKERS = ("auth_not_found",)

# A retry is started only when at least this much of the call's wall-clock limit is left.
_MIN_ATTEMPT_WINDOW_S = 5.0

# GROK_API_MODE -> endpoint under GROK_API_URL.
_API_PATHS = {"chat": "/chat/completions", "responses": "/responses"}
# Responses API incomplete_details.reason -> the chat finish_reason it corresponds to.
_RESPONSES_FINISH = {"max_output_tokens": "length", "content_filter": "content_filter"}
_USD_TICKS = 10_000_000_000
_MAX_SEARCH_QUERIES = 10


class GrokUpstreamError(Exception):
    """An upstream failure that the caller should see, with retry metadata.

    The message is redacted (API keys, bearer tokens) because it reaches the tool output,
    the log file and the breaker state, and proxies sometimes echo request headers.
    ``retry_class`` names failures that are retried at most once per call.
    ``result_fields`` carries what the upstream reported about a stream that ended without an answer
    (finish_reason, usage, ...), in the shape of GrokResponse.result_fields().
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "upstream_error",
        retryable: bool = True,
        retry_after: Optional[float] = None,
        status: Optional[int] = None,
        exhausted: bool = False,
        retry_class: Optional[str] = None,
        result_fields: Optional[dict] = None,
    ):
        super().__init__(redact(message, 600))
        self.error_type = error_type
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status
        self.exhausted = exhausted
        self.retry_class = retry_class
        self.result_fields = result_fields or {}

    @property
    def is_rate_limit(self) -> bool:
        return self.error_type == "rate_limit"


def _is_exhausted(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _EXHAUSTED_MARKERS)


def _is_deterministic(error_obj, lowered: str) -> tuple[bool, bool]:
    """(no retry can help, it is a credential failure) for an error object and its lowered text."""
    kinds: set[str] = set()
    explicit = False
    if isinstance(error_obj, dict):
        kinds = {str(error_obj.get(name) or "").lower() for name in ("type", "code")}
        explicit = error_obj.get("retryable") is False
    auth = any(kind in kinds for kind in _AUTH_FAILURE_KINDS) or any(m in lowered for m in _AUTH_FAILURE_MARKERS)
    return auth or explicit, auth


def _cooldown_class(exhausted: bool, retry_after: Optional[float]) -> Optional[str]:
    """An exhausted pool with a known recovery time is waited out once; if it is still exhausted
    after that, waiting again is unlikely to help."""
    return "cooldown" if exhausted and retry_after is not None else None


def _reset_seconds(obj) -> Optional[float]:
    """CPA puts the cooldown into reset_seconds; error frames carry no Retry-After header."""
    if not isinstance(obj, dict):
        return None
    try:
        value = float(obj.get("reset_seconds"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _make_stream_error(error_obj, status: Optional[int] = None, envelope: Optional[dict] = None) -> GrokUpstreamError:
    """Classify an error delivered inside a stream (or a JSON body).

    ``envelope`` is the whole payload: xAI sends {"code": ..., "error": "<text>"}, so the code
    lives next to the error string rather than inside it.
    """
    code = ""
    retry_after = None
    if isinstance(error_obj, dict):
        msg = str(error_obj.get("message") or json.dumps(error_obj, ensure_ascii=False))
        code = str(error_obj.get("code") or error_obj.get("type") or "")
        retry_after = _reset_seconds(error_obj)
    else:
        msg = str(error_obj)
    if not code and isinstance(envelope, dict) and isinstance(envelope.get("code"), (str, int)):
        code = str(envelope["code"])
    if retry_after is None:
        retry_after = _reset_seconds(envelope)
    text = f"{msg} [{code}]" if code else msg
    code_l = code.lower()
    lowered = text.lower()
    deterministic, auth = _is_deterministic(error_obj, lowered)
    if deterministic:
        if any(c in code_l for c in _NON_RETRYABLE_ERROR_CODES):
            error_type = code_l
        else:
            error_type = "auth" if auth else (code_l or "upstream_error")
        return GrokUpstreamError(text, error_type=error_type, retryable=False, retry_after=retry_after, status=status)
    exhausted = _is_exhausted(lowered)
    rate_limited = exhausted or "rate_limit" in code_l or code_l == "429" or any(m in lowered for m in _RATE_LIMIT_MARKERS)
    if rate_limited:
        return GrokUpstreamError(
            text, error_type="rate_limit", retryable=not exhausted or retry_after is not None,
            retry_after=retry_after, status=status or 429, exhausted=exhausted,
            retry_class=_cooldown_class(exhausted, retry_after),
        )
    if any(c in code_l for c in _NON_RETRYABLE_ERROR_CODES):
        return GrokUpstreamError(text, error_type=code_l or "invalid_request", retryable=False, status=status)
    if any(t in code_l for t in _RETRYABLE_ERROR_TYPES):
        return GrokUpstreamError(text, error_type=code_l, retryable=True, retry_after=retry_after, status=status)
    retryable = not any(m in lowered for m in _NON_RETRYABLE_ERROR_MARKERS)
    return GrokUpstreamError(text, error_type=code_l or "upstream_error", retryable=retryable, retry_after=retry_after, status=status)


def _parse_retry_after(response) -> Optional[float]:
    """解析 Retry-After 头（支持秒数或 HTTP 日期格式）；接受 response 或 headers"""
    headers = getattr(response, "headers", response)
    header = headers.get("Retry-After") if headers is not None else None
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        retry_dt = parsedate_to_datetime(header)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        delay = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)
    except (TypeError, ValueError):
        return None


def _http_error(status: int, body: str, headers) -> GrokUpstreamError:
    """Classify a non-2xx answer of the gateway. Retry-After is honoured on every status."""
    retry_after = _parse_retry_after(headers)
    error_obj = None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error_obj = data.get("error", data)
    except ValueError:
        pass
    lowered = body.lower()
    # Redact the whole body first, then shorten it (an HTML error page is reduced to its title).
    summary = summarize_error_body(
        body, status=status, content_type=headers.get("content-type", ""), server=headers.get("server", ""),
    )
    message = summary if summary.startswith("Cloudflare ") else f"HTTP {status}: {summary}"
    if status == 429:
        exhausted = _is_exhausted(lowered)
        return GrokUpstreamError(
            message, error_type="rate_limit", retryable=not exhausted or retry_after is not None,
            retry_after=retry_after, status=429, exhausted=exhausted,
            retry_class=_cooldown_class(exhausted, retry_after),
        )
    deterministic, auth = _is_deterministic(error_obj, lowered)
    if deterministic and status in RETRYABLE_STATUS_CODES:
        # CPA answers 503 for "no credential for this model" too; the body says a retry cannot help.
        return GrokUpstreamError(
            message, error_type="auth" if auth else f"http_{status}", retryable=False,
            retry_after=retry_after, status=status,
        )
    if status == 503 and _is_exhausted(lowered):
        # CPA's 503 auth_unavailable: every credential of the model is blocked until Retry-After.
        return GrokUpstreamError(
            message, error_type="rate_limit", retryable=retry_after is not None, retry_after=retry_after,
            status=503, exhausted=True, retry_class=_cooldown_class(True, retry_after),
        )
    if status in _TIMEOUT_STATUS_HINTS:
        return GrokUpstreamError(
            message + _TIMEOUT_STATUS_HINTS[status], error_type="timeout", retryable=False,
            retry_after=retry_after, status=status,
        )
    if status in _EDGE_RETRY_ONCE:
        return GrokUpstreamError(
            message, error_type=f"http_{status}", retryable=True, retry_after=retry_after,
            status=status, retry_class="edge",
        )
    return GrokUpstreamError(
        message, error_type=f"http_{status}", retryable=status in RETRYABLE_STATUS_CODES,
        retry_after=retry_after, status=status,
    )


def _transport_error(exc: Exception, read_timeout: Optional[float]) -> GrokUpstreamError:
    """Name a transport failure (httpx often leaves the message empty) and decide whether to retry."""
    if isinstance(exc, httpx.ReadTimeout):
        waited = f" for {read_timeout:.0f}s" if read_timeout else ""
        # The request was sent, so the gateway may still be running the search: retried at most once.
        return GrokUpstreamError(
            f"upstream sent no data{waited} (ReadTimeout, GROK_READ_TIMEOUT_S)",
            error_type="timeout", retryable=True, retry_class="read_timeout",
        )
    if isinstance(exc, httpx.TimeoutException):
        return GrokUpstreamError(describe_transport_error(exc), error_type="timeout", retryable=True)
    if isinstance(exc, httpx.TransportError) and not isinstance(exc, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        return GrokUpstreamError(describe_transport_error(exc), error_type="network", retryable=True)
    error = classify_exception("grok", exc)
    return GrokUpstreamError(error.detail or type(exc).__name__, error_type=error.category, retryable=False)


def _scrub_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates (badly escaped JSON from the client), which UTF-8 cannot encode."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _waits_out_cooldown(exc) -> bool:
    """An exhausted pool that said when it recovers: the retry sleeps exactly that long (if it fits
    the retry budget and the call's limit), so the breaker this very failure opened must not veto it.
    Without a recovery time an exhausted pool is not retried at all."""
    return isinstance(exc, GrokUpstreamError) and exc.exhausted and exc.retry_after is not None


def _is_retryable_exception(exc) -> bool:
    """检查异常是否可重试"""
    if isinstance(exc, BreakerOpen):
        return False
    if isinstance(exc, GrokUpstreamError):
        return exc.retryable
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


class _WaitWithRetryAfter(wait_base):
    """等待策略：优先使用上游给出的 Retry-After，否则使用指数退避"""

    def __init__(self, multiplier: float, max_wait: int):
        self._base_wait = wait_random_exponential(multiplier=multiplier, max=max_wait)
        self._protocol_error_base = 3.0

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, GrokUpstreamError) and exc.retry_after is not None:
                return float(exc.retry_after)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = _parse_retry_after(exc.response)
                if retry_after is not None:
                    return retry_after
            if isinstance(exc, httpx.RemoteProtocolError) or isinstance(getattr(exc, "__cause__", None), httpx.RemoteProtocolError):
                return self._base_wait(retry_state) + self._protocol_error_base
        return self._base_wait(retry_state)

    def _parse_retry_after(self, response) -> Optional[float]:
        return _parse_retry_after(response)


class _StopWhenRetryBudgetExhausted(stop_base):
    """停止条件：按本次调用已累计的重试等待时间（不含请求本身的耗时）计算预算。

    一次搜索请求本身常常要跑一两分钟，所以不能用 stop_after_delay 那种"自调用开始的总时间"；
    否则第一次尝试之后就再也不会重试。这里只累计重试之间的睡眠时间（tenacity 的 idle_for），
    并在上游要求的 Retry-After 会让累计等待超出预算时立刻放弃，而不是白等。
    调用的总耗时由 GROK_SEARCH_TIMEOUT_S（_CallState / _StopNearDeadline）单独限制。
    """

    def __init__(self, budget_s: float):
        self.budget_s = budget_s

    def __call__(self, retry_state) -> bool:
        slept = float(getattr(retry_state, "idle_for", 0.0) or 0.0)
        if slept >= self.budget_s:
            return True
        if not retry_state.outcome or not retry_state.outcome.failed:
            return False
        exc = retry_state.outcome.exception()
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is None and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = _parse_retry_after(exc.response)
        if retry_after is None:
            return False
        return slept + float(retry_after) > self.budget_s


class _CallState:
    """Bookkeeping for one search call: wall-clock limit, attempts, once-only retries, probe token."""

    def __init__(self, limit_s: float, read_timeout_s: float):
        self.started = time.monotonic()
        self.limit_s = limit_s
        self.read_timeout_s = read_timeout_s
        self.attempts = 0
        self.last_error: Optional[BaseException] = None
        self.limited: dict[str, int] = {}
        self.probe = None
        self.settled = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> Optional[float]:
        return None if not self.limit_s else self.limit_s - self.elapsed()

    def note_failure(self, exc: BaseException) -> None:
        self.last_error = exc
        retry_class = getattr(exc, "retry_class", None)
        if retry_class:
            self.limited[retry_class] = self.limited.get(retry_class, 0) + 1

    def may_retry(self, exc: BaseException) -> bool:
        retry_class = getattr(exc, "retry_class", None)
        if not retry_class:
            return True
        if self.limited.get(retry_class, 0) > 1:
            return False
        if retry_class == "read_timeout":
            # Another full wait must still fit into the call's limit, or the retry only burns tokens.
            remaining = self.remaining()
            return remaining is None or remaining >= self.read_timeout_s
        return True

    def deadline_error(self) -> GrokUpstreamError:
        if self.attempts == 0:
            text = (
                f"no free Grok request slot within {self.limit_s:.0f}s (GROK_SEARCH_TIMEOUT_S; "
                f"GROK_MAX_CONCURRENCY={config.max_concurrency} searches were already running)"
            )
        else:
            text = f"no complete answer within {self.limit_s:.0f}s (GROK_SEARCH_TIMEOUT_S) after {self.attempts} attempt(s)"
            if self.last_error is not None:
                text += f"; last error: {self.last_error}"
        return GrokUpstreamError(text, error_type="timeout", retryable=False)

    def annotate(self, exc: GrokUpstreamError) -> None:
        if exc.error_type in ("timeout", "network", "truncated_stream") and self.attempts:
            exc.args = (redact(f"{exc} (gave up after {self.attempts} attempt(s), {self.elapsed():.0f}s)", 600),)


class _StopNearDeadline(stop_base):
    """停止条件：调用的总时限（GROK_SEARCH_TIMEOUT_S）不够再等 Retry-After 并完成一次尝试时停止。"""

    def __init__(self, call: _CallState):
        self.call = call

    def __call__(self, retry_state) -> bool:
        remaining = self.call.remaining()
        if remaining is None:
            return False
        retry_after = 0.0
        if retry_state.outcome and retry_state.outcome.failed:
            retry_after = float(getattr(retry_state.outcome.exception(), "retry_after", None) or 0.0)
        return remaining - retry_after < _MIN_ATTEMPT_WINDOW_S


@dataclass
class GrokResponse:
    content: str = ""
    annotations: list[dict] = field(default_factory=list)
    # How the stream ended. Not part of equality: two responses are equal when their answers are.
    finish_reason: Optional[str] = field(default=None, compare=False)
    native_finish_reason: Optional[str] = field(default=None, compare=False)
    done_seen: bool = field(default=False, compare=False)
    usage: Optional[dict] = field(default=None, compare=False)
    dropped_events: int = field(default=0, compare=False)
    # Queries of the upstream's own web/X searches (Responses API web_search_call / x_search_call items).
    search_queries: list[str] = field(default_factory=list, compare=False)
    # Search-call items the stream showed (searches and page opens), by item id.
    search_calls_seen: set = field(default_factory=set, compare=False)

    @property
    def incomplete(self) -> bool:
        """The upstream ended the answer early: output cap, content filter or another non-stop reason."""
        finish = (self.finish_reason or "").lower()
        native = (self.native_finish_reason or "").lower()
        return finish in ("length", "content_filter") or native not in ("", "stop")

    def compact_usage(self) -> Optional[dict]:
        """Token counts under the chat names (the Responses API's input/output_tokens are mapped onto them),
        plus the cost and the upstream's own search calls when the upstream reports them (xAI does)."""
        usage = self.usage
        if not isinstance(usage, dict):
            return None

        def count(value):
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        out = {}
        for name, alias in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
            value = count(usage.get(name, usage.get(alias)))
            if value is not None:
                out[name] = value
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
        reasoning = count(details.get("reasoning_tokens") if isinstance(details, dict) else usage.get("reasoning_tokens"))
        if reasoning is not None:
            out["reasoning_tokens"] = reasoning
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
        if cached:
            out["cached_tokens"] = cached
        ticks = usage.get("cost_in_usd_ticks")
        if isinstance(ticks, (int, float)) and not isinstance(ticks, bool) and ticks >= 0:
            try:
                cost = ticks / _USD_TICKS  # xAI bills in ticks: 1 USD = 10^10 ticks
            except OverflowError:
                cost = math.inf
            if math.isfinite(cost):
                out["cost_usd"] = round(cost, 4)
        tools = usage.get("server_side_tool_usage_details")
        if isinstance(tools, dict):
            for name in ("web_search_calls", "x_search_calls"):
                calls = count(tools.get(name))
                if calls:
                    out[name] = calls
        return out or None

    def search_queries_partial(self, usage: Optional[dict] = None) -> bool:
        """Whether search_queries leaves searches out: more distinct queries than are listed, or the upstream
        billed more search calls than the stream showed. A multi-agent model runs most of its searches in
        sub-agents whose calls never appear in the stream (73 billed, 5 shown: 2 searches and 3 page opens)."""
        usage = self.compact_usage() if usage is None else usage
        billed = sum((usage or {}).get(name, 0) for name in ("web_search_calls", "x_search_calls"))
        return len(self.search_queries) > _MAX_SEARCH_QUERIES or billed > len(self.search_calls_seen)

    def result_fields(self) -> dict:
        """Completion state and token usage that web_search adds to a successful result."""
        out: dict = {}
        if self.incomplete:
            out.update(incomplete=True, finish_reason=self.finish_reason, native_finish_reason=self.native_finish_reason)
        usage = self.compact_usage()
        if usage:
            out["usage"] = usage
        if self.search_queries:
            out["search_queries"] = self.search_queries[:_MAX_SEARCH_QUERIES]
            if self.search_queries_partial(usage):
                out["search_queries_partial"] = True
        if self.dropped_events:
            out["dropped_events"] = self.dropped_events
        return out

    def finish_reasons(self) -> str:
        return ", ".join(
            f"{name}={value}"
            for name, value in (("finish_reason", self.finish_reason), ("native_finish_reason", self.native_finish_reason))
            if value
        )

    def incomplete_note(self) -> str:
        return f"[回答不完整] 上游提前结束了生成（{self.finish_reasons()}），以上内容可能被截断或过滤。"


_SSE_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class _SSELineSplitter:
    """Split a UTF-8 byte stream into lines on CRLF, LF or CR only, as the SSE format defines.

    httpx's aiter_lines() uses str.splitlines(), which also breaks on U+0085, U+2028 and U+2029.
    JSON may carry those characters unescaped inside strings, so such a frame was cut in two and lost.
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
        self._partial: list[str] = []
        self._skip_lf = False

    def feed(self, data: bytes, final: bool = False) -> list[str]:
        text = self._decoder.decode(data, final)
        if self._skip_lf and text:
            # The previous chunk ended with "\r"; a "\n" here completes that CRLF.
            if text[0] == "\n":
                text = text[1:]
            self._skip_lf = False
        lines = []
        start = 0
        for match in _SSE_LINE_BREAK.finditer(text):
            self._partial.append(text[start:match.start()])
            lines.append("".join(self._partial))
            self._partial = []
            start = match.end()
        if start < len(text):
            self._partial.append(text[start:])
        if text.endswith("\r"):
            self._skip_lf = True
        if final and self._partial:
            lines.append("".join(self._partial))
            self._partial = []
        return lines


class GrokSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str = DEFAULT_MODEL, api_mode: Optional[str] = None):
        # "https://host/v1/" must not become "https://host/v1//chat/completions" (404 on CPA).
        super().__init__((api_url or "").strip().rstrip("/"), (api_key or "").strip())
        self.model = model
        self.api_mode = api_mode if api_mode in _API_PATHS else config.api_mode

    def get_provider_name(self) -> str:
        return "Grok"

    @property
    def _breaker_key(self) -> tuple[str, str]:
        # Keyed by the routed base model: thinking levels of one model share its account pool,
        # so "grok-4(high)" and "grok-4(low)" must not have separate breaker states.
        return (self.api_url, routing_base(self.model))

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None, instructions: str = "") -> str:
        """Keep the text-only provider API for existing callers."""
        result = await self.search_with_sources(query, platform, min_results, max_results, ctx, instructions=instructions)
        return result.content

    async def search_with_sources(
        self,
        query: str,
        platform: str = "",
        min_results: int = 3,
        max_results: int = 10,
        ctx=None,
        instructions: str = "",
    ) -> GrokResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        query = _scrub_surrogates(query or "")
        platform = _scrub_surrogates(platform or "").strip()
        instructions = _scrub_surrogates(instructions or "")
        platform_prompt = ""
        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        time_context = get_local_time_info() + "\n"
        user_content = time_context + query + platform_prompt
        if instructions and instructions.strip():
            user_content += "\n\n[Additional instructions from the caller]\n" + instructions.strip() + "\n"
        # The whole message, not only the caller's parts: nothing in it may break the UTF-8 request body.
        user_content = _scrub_surrogates(user_content)

        messages = [
            {"role": "system", "content": build_search_prompt(config.search_style)},
            {"role": "user", "content": user_content},
        ]
        tools = [{"type": "web_search"}, {"type": "x_search"}]
        if self.api_mode == "responses":
            # The same conversation as Responses API input; store=False keeps it off the upstream's storage.
            payload = {"model": self.model, "input": messages, "stream": True, "tools": tools, "store": False}
        else:
            payload = {"model": self.model, "messages": messages, "stream": True, "tools": tools}

        await log_info(ctx, f"platform_prompt: { query + platform_prompt}", config.debug_enabled)

        return await self._execute_stream_result_with_retry(headers, payload, ctx)

    async def _parse_streaming_response(self, response, ctx=None) -> str:
        result = await self._parse_streaming_result(response, ctx)
        return result.content

    async def _parse_streaming_result(self, response, ctx=None) -> GrokResponse:
        result = GrokResponse()
        full_body_buffer = []
        saw_sse = False
        event_type = ""
        data_lines: list[str] = []

        def collect(data):
            if not isinstance(data, dict):
                return
            error = data.get("error")
            if error:
                raise _make_stream_error(error, None, data)
            usage = data.get("usage")
            if isinstance(usage, dict):
                result.usage = usage
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return
            choice = choices[0]
            for name in ("finish_reason", "native_finish_reason"):
                value = choice.get(name)
                if isinstance(value, str) and value:
                    setattr(result, name, value)
            message = choice.get("delta")
            if not isinstance(message, dict):
                message = choice.get("message")
            if not isinstance(message, dict):
                return
            content = message.get("content")
            if isinstance(content, str):
                result.content += content
            # Citations often arrive in chunks with no content at all.
            annotations = message.get("annotations")
            if isinstance(annotations, list):
                result.annotations.extend(a for a in annotations if isinstance(a, dict))

        # Responses API: text parts and their annotations are collected while streaming and joined into the
        # answer at the end (assemble_responses). The final response object, when it carries the message
        # text, is authoritative: it survives an undecodable delta frame and holds every annotation.
        parts: dict = {}  # (output_index, content_index) -> {"deltas": [...], "done": text or None, "annotations": [...]}
        stray_annotations: list = []  # annotations that arrived before any text part
        final_parts: list = []  # [(text, annotations)] from the terminal response object

        def text_part(data) -> dict:
            key = (data.get("output_index"), data.get("content_index"))
            return parts.setdefault(key, {"deltas": [], "done": None, "annotations": []})

        def note_search(item):
            if not isinstance(item, dict) or not str(item.get("type") or "").endswith("search_call"):
                return
            action = item.get("action")
            # The terminal response object repeats the streamed items: an item is counted once by its id.
            item_id = item.get("id")
            result.search_calls_seen.add(item_id if isinstance(item_id, str) and item_id else json.dumps(item, sort_keys=True, default=str))
            query = action.get("query") if isinstance(action, dict) else None
            if isinstance(query, str) and query.strip() and query.strip() not in result.search_queries:
                result.search_queries.append(query.strip())

        def finish_response(response_obj):
            """response.completed / .incomplete / .failed, or a whole non-streaming response object."""
            result.done_seen = True
            if isinstance(response_obj.get("usage"), dict):
                result.usage = response_obj["usage"]
            status = str(response_obj.get("status") or "")
            error = response_obj.get("error")
            if status == "failed" or (error and status != "completed"):
                raise _make_stream_error(error or f"upstream response {status or 'failed'}", None, response_obj)
            if status == "completed":
                result.finish_reason = "stop"
            else:
                # incomplete (output cap, content filter), or cancelled / in_progress / queued: not a whole answer.
                details = response_obj.get("incomplete_details")
                reason = details.get("reason") if isinstance(details, dict) else None
                result.native_finish_reason = str(reason or status or "incomplete")
                result.finish_reason = _RESPONSES_FINISH.get(result.native_finish_reason, result.native_finish_reason)
            for item in response_obj.get("output") or []:
                note_search(item)
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        annotations = part.get("annotations")
                        final_parts.append((part["text"], annotations if isinstance(annotations, list) else []))

        def collect_responses(data):
            if not isinstance(data, dict):
                return
            kind = str(data.get("type") or "")
            if kind == "error" or (not kind.startswith("response") and data.get("error")):
                raise _make_stream_error(data.get("error") or data, None, data)
            if kind == "response.output_text.delta":
                if isinstance(data.get("delta"), str):
                    text_part(data)["deltas"].append(data["delta"])
            elif kind == "response.output_text.done":
                if isinstance(data.get("text"), str):
                    text_part(data)["done"] = data["text"]
            elif kind == "response.output_text.annotation.added":
                annotation = data.get("annotation")
                key = (data.get("output_index"), data.get("content_index"))
                if isinstance(annotation, dict):
                    # Its own part, else the part being written (an event without indices), else the first one.
                    target = parts.get(key) or (parts[next(reversed(parts))] if parts else None)
                    (target["annotations"] if target else stray_annotations).append(annotation)
            elif kind == "response.output_item.done":
                note_search(data.get("item"))
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                response_obj = data.get("response")
                if isinstance(response_obj, dict):
                    finish_response(response_obj)
                elif data.get("error") or kind == "response.failed":
                    raise _make_stream_error(data.get("error") or f"upstream {kind}", None, data)
            elif data.get("object") == "response":
                finish_response(data)

        def assemble_responses():
            if any(text for text, _ in final_parts):
                chosen = final_parts
            else:
                chosen = []
                for part in parts.values():
                    streamed = "".join(part["deltas"])
                    # output_text.done carries the part's whole text: it wins over deltas that lost a frame.
                    done = part["done"]
                    chosen.append((done if isinstance(done, str) and done else streamed, part["annotations"]))
                if chosen and stray_annotations:
                    chosen[0] = (chosen[0][0], stray_annotations + chosen[0][1])
            texts = []
            offset = 0
            for text, annotations in chosen:
                if not text:
                    # Sources without text: kept like chat-mode annotations that arrive without content.
                    result.annotations.extend(a for a in annotations if isinstance(a, dict))
                    continue
                if texts:
                    offset += 2  # "\n\n" between a second message or content part and the previous one
                for annotation in annotations:
                    if not isinstance(annotation, dict):
                        continue
                    if offset:
                        annotation = dict(annotation)
                        for name in ("start_index", "end_index"):
                            if isinstance(annotation.get(name), int):
                                annotation[name] += offset
                    result.annotations.append(annotation)
                texts.append(text)
                offset += len(text)
            result.content = "\n\n".join(texts)

        collect_event = collect_responses if self.api_mode == "responses" else collect

        def handle(payload: str, event: str) -> bool:
            """Process one event payload; False when it is not JSON."""
            if payload.strip() == "[DONE]":
                result.done_seen = True
                return True
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return False
            if event == "error":
                envelope = data if isinstance(data, dict) else None
                raise _make_stream_error(data.get("error", data) if envelope else data, None, envelope)
            collect_event(data)
            return True

        def dispatch():
            # SSE: a blank line ends the event; its data lines form one payload joined by "\n".
            nonlocal event_type, data_lines
            lines, event = data_lines, event_type
            data_lines, event_type = [], ""
            if not lines:
                return
            payload = "\n".join(lines)
            if handle(payload, event):
                return
            # Proxies that omit the blank line between events put several JSON documents into one event.
            failed = [line for line in lines if not handle(line, event)] if len(lines) > 1 else lines
            if not failed:
                return
            if event == "error":
                raise _make_stream_error(f"upstream error frame: {redact(payload, 500)}")
            result.dropped_events += len(failed)
            logger.warning(f"Grok stream: dropped {len(failed)} undecodable data frame(s): {redact(failed[0], 200)}")

        def feed_line(raw_line: str):
            nonlocal saw_sse, event_type
            line = raw_line.strip(" \t")
            if not line:
                dispatch()
                return
            if line.startswith(":"):
                return  # SSE comment, e.g. a keep-alive
            # 兼容 "data: {...}" 和 "data:{...}" 两种 SSE 格式
            name, sep, value = line.partition(":")
            if sep and name in ("data", "event", "id", "retry"):
                saw_sse = True
                if value.startswith(" "):
                    value = value[1:]
                if name == "data":
                    data_lines.append(value)
                elif name == "event":
                    event_type = value.strip().lower()
            elif not saw_sse:
                full_body_buffer.append(raw_line)

        splitter = _SSELineSplitter()
        async for chunk in response.aiter_bytes():
            for raw_line in splitter.feed(chunk):
                feed_line(raw_line)
        for raw_line in splitter.feed(b"", final=True):
            feed_line(raw_line)
        dispatch()

        if not saw_sse and full_body_buffer:
            try:
                collect_event(json.loads("\n".join(full_body_buffer)))
                result.done_seen = True  # a complete JSON document cannot be cut off
            except json.JSONDecodeError:
                pass
        if self.api_mode == "responses":
            assemble_responses()

        await log_info(ctx, f"content: {result.content}", config.debug_enabled)

        return result

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None) -> str:
        result = await self._execute_stream_result_with_retry(headers, payload, ctx)
        return result.content

    async def _request_once(self, client, headers: dict, payload: dict, ctx, key) -> GrokResponse:
        try:
            async with client.stream(
                "POST",
                f"{self.api_url}{_API_PATHS[self.api_mode]}",
                headers=headers,
                json=payload,
            ) as response:
                status = response.status_code
                if status >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _http_error(status, body, response.headers)
                result = await self._parse_streaming_result(response, ctx)
        except GrokUpstreamError as e:
            if e.is_rate_limit:
                breaker.record_failure(key, retry_after=e.retry_after, exhausted=e.exhausted, reason=redact(str(e), 120))
            raise
        except httpx.HTTPError as e:
            raise _transport_error(e, getattr(getattr(client, "timeout", None), "read", None)) from e
        if not result.content and not result.annotations:
            if result.incomplete:
                # The upstream itself stopped before the first token (output cap spent on reasoning, or the
                # content filter fired). The same prompt would end the same way; resending a filtered prompt
                # can also get the model blocked on the gateway, so this is not retried.
                raise GrokUpstreamError(
                    f"upstream ended the answer before any content ({result.finish_reasons()}); "
                    "retrying the same query would not help",
                    error_type="incomplete",
                    retryable=False,
                    status=status,
                    result_fields=result.result_fields(),
                )
            raise GrokUpstreamError(
                "upstream stream contained no content",
                error_type="empty_response",
                retryable=True,
                status=status,
                result_fields=result.result_fields(),
            )
        if not result.done_seen and not result.finish_reason:
            # A clean close without finish_reason or [DONE] cut the answer off somewhere.
            raise GrokUpstreamError(
                "upstream stream ended without finish_reason or [DONE]; the answer was cut off",
                error_type="truncated_stream",
                retryable=True,
            )
        return result

    async def _execute_stream_result_with_retry(self, headers: dict, payload: dict, ctx=None) -> GrokResponse:
        """执行带重试、并发上限、熔断保护与总时限（GROK_SEARCH_TIMEOUT_S）的流式 HTTP 请求"""
        key = self._breaker_key
        call = _CallState(config.search_timeout_s, config.read_timeout_s)
        call.probe = breaker.check(key)

        timeout = httpx.Timeout(connect=6.0, read=call.read_timeout_s, write=10.0, pool=None)
        stop = stop_after_attempt(config.retry_max_attempts + 1) | _StopNearDeadline(call)
        budget = config.retry_budget_s
        if budget > 0:
            stop = stop | _StopWhenRetryBudgetExhausted(budget)

        def _log_before_retry(retry_state):
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            sleep = getattr(retry_state.next_action, "sleep", "?")
            logger.warning(
                f"Grok request failed (model={payload.get('model')}, attempt {retry_state.attempt_number}), "
                f"retrying in {sleep}s: {redact(str(exc), 300)}"
            )

        def _should_retry(exc) -> bool:
            return (
                _is_retryable_exception(exc)
                and (_waits_out_cooldown(exc) or not breaker.is_open(key))
                and call.may_retry(exc)
            )

        async def run() -> GrokResponse:
            async with get_semaphore():
                if call.probe is None:
                    # Queued calls check again: the breaker may have opened while they waited for a slot.
                    call.probe = breaker.check(key)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async for attempt in AsyncRetrying(
                        stop=stop,
                        wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                        retry=retry_if_exception(_should_retry),
                        before_sleep=_log_before_retry,
                        reraise=True,
                    ):
                        with attempt:
                            call.attempts += 1
                            try:
                                result = await self._request_once(client, headers, payload, ctx, key)
                            except Exception as e:
                                call.note_failure(e)
                                raise
                            breaker.record_success(key)
                            call.settled = True
                            return result

        try:
            if call.limit_s:
                return await asyncio.wait_for(run(), timeout=call.limit_s)
            return await run()
        except asyncio.TimeoutError:
            raise call.deadline_error() from None
        except GrokUpstreamError as e:
            call.annotate(e)
            raise
        finally:
            if not call.settled:
                breaker.release_probe(key, call.probe)
