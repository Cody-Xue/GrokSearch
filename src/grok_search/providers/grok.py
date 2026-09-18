import httpx
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.stop import stop_base
from tenacity.wait import wait_base
from .base import BaseSearchProvider
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
_EXHAUSTED_MARKERS = ("no available accounts", "no accounts available", "account pool exhausted")
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "too many requests", "ratelimit")


class GrokUpstreamError(Exception):
    """An upstream failure that the caller should see, with retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "upstream_error",
        retryable: bool = True,
        retry_after: Optional[float] = None,
        status: Optional[int] = None,
        exhausted: bool = False,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status
        self.exhausted = exhausted

    @property
    def is_rate_limit(self) -> bool:
        return self.error_type == "rate_limit"


def _is_exhausted(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _EXHAUSTED_MARKERS)


def _make_stream_error(error_obj, status: Optional[int] = None) -> GrokUpstreamError:
    code = ""
    if isinstance(error_obj, dict):
        msg = str(error_obj.get("message") or json.dumps(error_obj, ensure_ascii=False))
        code = str(error_obj.get("code") or error_obj.get("type") or "")
        text = f"{msg} [{code}]" if code else msg
    else:
        text = str(error_obj)
    code_l = code.lower()
    lowered = text.lower()
    exhausted = _is_exhausted(lowered)
    rate_limited = exhausted or "rate_limit" in code_l or code_l == "429" or any(m in lowered for m in _RATE_LIMIT_MARKERS)
    if rate_limited:
        return GrokUpstreamError(text, error_type="rate_limit", retryable=not exhausted, status=status or 429, exhausted=exhausted)
    if any(c in code_l for c in _NON_RETRYABLE_ERROR_CODES):
        return GrokUpstreamError(text, error_type=code_l or "invalid_request", retryable=False, status=status)
    if any(t in code_l for t in _RETRYABLE_ERROR_TYPES):
        return GrokUpstreamError(text, error_type=code_l, retryable=True, status=status)
    retryable = not any(m in lowered for m in _NON_RETRYABLE_ERROR_MARKERS)
    return GrokUpstreamError(text, error_type=code_l or "upstream_error", retryable=retryable, status=status)


def _parse_retry_after(response) -> Optional[float]:
    """解析 Retry-After 头（支持秒数或 HTTP 日期格式）"""
    header = response.headers.get("Retry-After")
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
            if isinstance(exc, httpx.RemoteProtocolError):
                return self._base_wait(retry_state) + self._protocol_error_base
        return self._base_wait(retry_state)

    def _parse_retry_after(self, response) -> Optional[float]:
        return _parse_retry_after(response)


class _StopWhenRetryBudgetExhausted(stop_base):
    """停止条件：按本次调用已累计的重试等待时间（不含请求本身的耗时）计算预算。

    一次搜索请求本身常常要跑一两分钟，所以不能用 stop_after_delay 那种"自调用开始的总时间"；
    否则第一次尝试之后就再也不会重试。这里只累计重试之间的睡眠时间（tenacity 的 idle_for），
    并在上游要求的 Retry-After 会让累计等待超出预算时立刻放弃，而不是白等。
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


@dataclass
class GrokResponse:
    content: str = ""
    annotations: list[dict] = field(default_factory=list)


class GrokSearchProvider(BaseSearchProvider):
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4-fast"):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "Grok"

    @property
    def _breaker_key(self) -> tuple[str, str]:
        return (self.api_url, self.model)

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
            "tools": [{"type": "web_search"}],
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
        current_event = ""
        status = getattr(response, "status_code", None)

        def collect(data):
            if not isinstance(data, dict):
                return
            error = data.get("error")
            if error:
                raise _make_stream_error(error, status)
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return
            choice = choices[0]
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

        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                # SSE: a blank line ends the current event, so the event type resets.
                current_event = ""
                continue

            if line.startswith("event:"):
                saw_sse = True
                current_event = line[6:].strip().lower()
                continue

            # 兼容 "data: {...}" 和 "data:{...}" 两种 SSE 格式
            if line.startswith("data:"):
                saw_sse = True
                if line in ("data: [DONE]", "data:[DONE]"):
                    continue
                json_str = line[5:].lstrip()
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    if current_event == "error":
                        raise _make_stream_error(f"upstream error frame: {json_str[:500]}", status)
                    continue
                if current_event == "error":
                    raise _make_stream_error(data.get("error", data) if isinstance(data, dict) else data, status)
                collect(data)
            elif not saw_sse:
                full_body_buffer.append(line)

        if not saw_sse and full_body_buffer:
            try:
                collect(json.loads("\n".join(full_body_buffer)))
            except json.JSONDecodeError:
                pass

        await log_info(ctx, f"content: {result.content}", config.debug_enabled)

        return result

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None) -> str:
        result = await self._execute_stream_result_with_retry(headers, payload, ctx)
        return result.content

    async def _request_once(self, client, headers: dict, payload: dict, ctx, key) -> GrokResponse:
        async with client.stream(
            "POST",
            f"{self.api_url}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            status = response.status_code
            if status >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                if status == 429:
                    retry_after = _parse_retry_after(response)
                    exhausted = _is_exhausted(body)
                    breaker.record_failure(key, retry_after=retry_after, exhausted=exhausted, reason=body.strip()[:120])
                    raise GrokUpstreamError(
                        f"HTTP 429: {body.strip()[:300]}",
                        error_type="rate_limit",
                        retryable=not exhausted,
                        retry_after=retry_after,
                        status=429,
                        exhausted=exhausted,
                    )
                raise GrokUpstreamError(
                    f"HTTP {status}: {body.strip()[:300]}",
                    error_type=f"http_{status}",
                    retryable=status in RETRYABLE_STATUS_CODES,
                    status=status,
                )
            try:
                result = await self._parse_streaming_result(response, ctx)
            except GrokUpstreamError as e:
                if e.is_rate_limit:
                    breaker.record_failure(key, retry_after=e.retry_after, exhausted=e.exhausted, reason=str(e)[:120])
                raise
        if not result.content and not result.annotations:
            raise GrokUpstreamError(
                "upstream stream contained no content",
                error_type="empty_response",
                retryable=True,
                status=status,
            )
        return result

    async def _execute_stream_result_with_retry(self, headers: dict, payload: dict, ctx=None) -> GrokResponse:
        """执行带重试、并发上限与熔断保护的流式 HTTP 请求"""
        key = self._breaker_key
        breaker.check(key)

        timeout = httpx.Timeout(connect=6.0, read=180.0, write=10.0, pool=None)
        stop = stop_after_attempt(config.retry_max_attempts + 1)
        budget = config.retry_budget_s
        if budget > 0:
            stop = stop | _StopWhenRetryBudgetExhausted(budget)

        def _log_before_retry(retry_state):
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            sleep = getattr(retry_state.next_action, "sleep", "?")
            logger.warning(
                f"Grok request failed (model={payload.get('model')}, attempt {retry_state.attempt_number}), "
                f"retrying in {sleep}s: {exc}"
            )

        settled = False
        try:
            async with get_semaphore():
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async for attempt in AsyncRetrying(
                        stop=stop,
                        wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                        retry=retry_if_exception(lambda e: _is_retryable_exception(e) and not breaker.is_open(key)),
                        before_sleep=_log_before_retry,
                        reraise=True,
                    ):
                        with attempt:
                            result = await self._request_once(client, headers, payload, ctx, key)
                            breaker.record_success(key)
                            settled = True
                            return result
        finally:
            if not settled:
                breaker.release_probe(key)
