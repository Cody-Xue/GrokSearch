"""Client-side protection for the upstream Grok proxy.

Two mechanisms:

* a concurrency semaphore, so a parallel fan-out from the MCP client cannot
  fire more simultaneous requests than the proxy tolerates;
* a circuit breaker keyed by (api_url, model), so repeated 429s or an
  exhausted account pool for one model tier fail fast instead of being
  retried by every in-flight call.
"""
import asyncio
import threading
import time
from collections import deque
from typing import Callable, Optional

from .config import config

BreakerKey = tuple[str, str]


class BreakerOpen(Exception):
    """Raised when a call is refused because the breaker for its key is open."""

    def __init__(self, key: BreakerKey, retry_after_s: float, reason: str = ""):
        self.key = key
        self.retry_after_s = max(0.0, float(retry_after_s))
        self.reason = reason
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"circuit open for model {key[1]} at {key[0]}: retry after {self.retry_after_s:.0f}s{detail}"
        )


class CircuitBreaker:
    """Sliding-window breaker with half-open probing.

    Parameters left as ``None`` are read from the environment-backed config at
    call time, so tests and operators can tune them without restarting.
    """

    def __init__(
        self,
        threshold: Optional[int] = None,
        window_s: Optional[float] = None,
        cooldown_s: Optional[float] = None,
        max_cooldown_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._threshold = threshold
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self.clock = clock
        self._states: dict[BreakerKey, dict] = {}
        self._lock = threading.Lock()

    # -- tunables -----------------------------------------------------------
    @property
    def threshold(self) -> int:
        return self._threshold if self._threshold is not None else config.breaker_threshold

    @property
    def window_s(self) -> float:
        return self._window_s if self._window_s is not None else config.breaker_window_s

    @property
    def cooldown_s(self) -> float:
        return self._cooldown_s if self._cooldown_s is not None else config.breaker_cooldown_s

    @property
    def max_cooldown_s(self) -> float:
        return self._max_cooldown_s if self._max_cooldown_s is not None else config.breaker_max_cooldown_s

    # -- internals ----------------------------------------------------------
    def _state(self, key: BreakerKey) -> dict:
        return self._states.setdefault(key, {
            "failures": deque(),
            "open_until": None,
            "half_open": False,
            "probe_in_flight": False,
            "last_cooldown": 0.0,
            "reason": "",
            "opened_count": 0,
        })

    def _prune(self, st: dict, now: float) -> None:
        window = self.window_s
        while st["failures"] and now - st["failures"][0] > window:
            st["failures"].popleft()

    # -- public API ---------------------------------------------------------
    def check(self, key: BreakerKey) -> None:
        """Admit a call or raise BreakerOpen. Admits exactly one probe when half-open."""
        now = self.clock()
        with self._lock:
            st = self._state(key)
            if st["open_until"] is None:
                return
            if now < st["open_until"]:
                raise BreakerOpen(key, st["open_until"] - now, st["reason"])
            if st["probe_in_flight"]:
                raise BreakerOpen(key, 5.0, "half-open probe in flight")
            st["half_open"] = True
            st["probe_in_flight"] = True

    def is_open(self, key: BreakerKey) -> bool:
        now = self.clock()
        with self._lock:
            st = self._states.get(key)
            return bool(st and st["open_until"] is not None and now < st["open_until"])

    def record_failure(
        self,
        key: BreakerKey,
        retry_after: Optional[float] = None,
        exhausted: bool = False,
        reason: str = "",
    ) -> bool:
        """Record a rate-limit style failure. Returns True when the breaker (re)opens."""
        now = self.clock()
        with self._lock:
            st = self._state(key)
            st["failures"].append(now)
            self._prune(st, now)
            was_half_open = st["half_open"]
            trip = exhausted or was_half_open or len(st["failures"]) >= self.threshold
            if not trip:
                return False
            if retry_after is not None and retry_after > 0:
                cooldown = min(float(retry_after), self.max_cooldown_s)
            elif was_half_open and st["last_cooldown"] > 0:
                cooldown = min(st["last_cooldown"] * 2, self.max_cooldown_s)
            else:
                cooldown = self.cooldown_s
            st["open_until"] = now + cooldown
            st["last_cooldown"] = cooldown
            st["half_open"] = False
            st["probe_in_flight"] = False
            st["reason"] = reason or ("account pool exhausted" if exhausted else "repeated rate limits")
            st["opened_count"] += 1
            return True

    def record_success(self, key: BreakerKey) -> None:
        with self._lock:
            st = self._state(key)
            st["failures"].clear()
            st["open_until"] = None
            st["half_open"] = False
            st["probe_in_flight"] = False
            st["last_cooldown"] = 0.0
            st["reason"] = ""

    def release_probe(self, key: BreakerKey) -> None:
        """A half-open probe ended without a definitive verdict; let the next caller probe."""
        with self._lock:
            st = self._states.get(key)
            if st and st["half_open"]:
                st["probe_in_flight"] = False

    def snapshot(self) -> dict:
        now = self.clock()
        out: dict = {}
        with self._lock:
            for (url, model), st in self._states.items():
                self._prune(st, now)
                if st["open_until"] is None and not st["failures"]:
                    continue
                if st["open_until"] is None:
                    state = "closed"
                elif now < st["open_until"]:
                    state = "open"
                else:
                    state = "half-open"
                out[f"{model} @ {url}"] = {
                    "state": state,
                    "recent_429": len(st["failures"]),
                    "retry_after_s": round(max(0.0, (st["open_until"] or now) - now), 1),
                    "reason": st["reason"],
                    "opened_count": st["opened_count"],
                }
        return out

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


breaker = CircuitBreaker()

_semaphore_state: dict = {"loop": None, "sem": None, "size": None}


def get_semaphore() -> asyncio.Semaphore:
    """Per-event-loop semaphore sized by GROK_MAX_CONCURRENCY."""
    loop = asyncio.get_running_loop()
    size = config.max_concurrency
    if _semaphore_state["loop"] is not loop or _semaphore_state["size"] != size:
        _semaphore_state["loop"] = loop
        _semaphore_state["sem"] = asyncio.Semaphore(size)
        _semaphore_state["size"] = size
    return _semaphore_state["sem"]
