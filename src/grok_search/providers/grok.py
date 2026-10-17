import asyncio
import codecs
import httpx
import json
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
from ..config import config
from ..throttle import breaker, get_semaphore, BreakerOpen


def get_local_time_info() -> str:
    """获取本地时间信息，注入到每次搜索的用户消息前"""
    try:
        local_tz = datetime.now().astimezone().tzinfo
        local_now = datetime.now(local_tz)
    except Exception:
        local_now = datetime.now(timezone.utc)

    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {local_now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {local_now.tzname() or 'Local'}\n"
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

    @property
    def incomplete(self) -> bool:
        """The upstream ended the answer early: output cap, content filter or another non-stop reason."""
        finish = (self.finish_reason or "").lower()
        native = (self.native_finish_reason or "").lower()
        return finish in ("length", "content_filter") or native not in ("", "stop")

    def compact_usage(self) -> Optional[dict]:
        if not isinstance(self.usage, dict):
            return None
        out = {name: self.usage[name] for name in ("prompt_tokens", "completion_tokens") if isinstance(self.usage.get(name), int)}
        details = self.usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else self.usage.get("reasoning_tokens")
        if isinstance(reasoning, int):
            out["reasoning_tokens"] = reasoning
        return out or None

    def result_fields(self) -> dict:
        """Completion state and token usage that web_search adds to a successful result."""
        out: dict = {}
        if self.incomplete:
            out.update(incomplete=True, finish_reason=self.finish_reason, native_finish_reason=self.native_finish_reason)
        usage = self.compact_usage()
        if usage:
            out["usage"] = usage
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
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4-fast"):
        # "https://host/v1/" must not become "https://host/v1//chat/completions" (404 on CPA).
        super().__init__((api_url or "").strip().rstrip("/"), (api_key or "").strip())
        self.model = model

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
        platform = _scrub_surrogates(platform or "")
        instructions = _scrub_surrogates(instructions or "")
        platform_prompt = ""
        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        time_context = get_local_time_info() + "\n"
        user_content = time_context + query + platform_prompt
        if instructions and instructions.strip():
            user_content += "\n\n[Additional instructions from the caller]\n" + instructions.strip() + "\n"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_search_prompt(config.search_style)},
                {"role": "user", "content": user_content},
            ],
            "stream": True,
            "tools": [{"type": "web_search"}, {"type": "x_search"}],
        }

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
            collect(data)
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
                collect(json.loads("\n".join(full_body_buffer)))
                result.done_seen = True  # a complete JSON document cannot be cut off
            except json.JSONDecodeError:
                pass

        await log_info(ctx, f"content: {result.content}", config.debug_enabled)

        return result

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None) -> str:
        result = await self._execute_stream_result_with_retry(headers, payload, ctx)
        return result.content

    async def _request_once(self, client, headers: dict, payload: dict, ctx, key) -> GrokResponse:
        try:
            async with client.stream(
                "POST",
                f"{self.api_url}/chat/completions",
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
