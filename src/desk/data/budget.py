"""Rolling fapi request-weight budget.

Binance bills the shared IP 2400 weight per minute. This object is the
ceiling for this process only. It does not retry. A 418, a 429, or a
used-weight header above the pause line sets a pause; callers stop sending
until that pause is over.
"""

from __future__ import annotations

import logging

# Shipped default. The paper loop on the shared host uses this number.
DEFAULT_WEIGHT_PER_MINUTE = 300
# A mis-set config cannot spend more than this. 600 is still a quarter of
# the 2400/min IP limit.
ABSOLUTE_MAX_WEIGHT_PER_MINUTE = 600
DEFAULT_USED_WEIGHT_PAUSE = 1200
# No Retry-After: do not try again inside the same minute, and do not
# treat an IP ban as a one-second blip.
DEFAULT_429_PAUSE_MS = 120_000
DEFAULT_418_PAUSE_MS = 900_000


class WeightBudget:
    """Hard cap on request weight in a rolling window.

    `allow` does not record the spend. Call `charge` when the request is
    actually sent, including when the response is 418 or 429.
    """

    def __init__(
        self,
        *,
        max_weight_per_minute: int = DEFAULT_WEIGHT_PER_MINUTE,
        pause_above: int = DEFAULT_USED_WEIGHT_PAUSE,
        window_ms: int = 60_000,
    ):
        self.max_weight = max(1, int(max_weight_per_minute))
        self.pause_above = int(pause_above)
        self.window_ms = int(window_ms)
        self._events: list[tuple[int, int]] = []
        self.pause_until_ms = 0
        self.pause_reason: str | None = None
        self.last_used_weight: int | None = None

    @classmethod
    def from_settings(cls, settings: dict) -> WeightBudget:
        data = settings.get("data") or {}
        raw = int(data.get("request_weight_per_minute") or DEFAULT_WEIGHT_PER_MINUTE)
        if raw > ABSOLUTE_MAX_WEIGHT_PER_MINUTE:
            logging.warning(
                "request_weight_per_minute %s is above the cap %s; using the cap",
                raw,
                ABSOLUTE_MAX_WEIGHT_PER_MINUTE,
            )
            raw = ABSOLUTE_MAX_WEIGHT_PER_MINUTE
        if raw < 1:
            raw = 1
        pause = int(data.get("used_weight_pause") or DEFAULT_USED_WEIGHT_PAUSE)
        return cls(max_weight_per_minute=raw, pause_above=pause)

    def spent(self, now_ms: int) -> int:
        self._trim(now_ms)
        return sum(weight for _ts, weight in self._events)

    def allow(self, weight: int, now_ms: int) -> tuple[bool, str]:
        """Whether one more request of `weight` may be sent. Does not charge."""
        now_ms = int(now_ms)
        weight = int(weight)
        if weight < 1:
            return False, "weight"
        if now_ms < self.pause_until_ms:
            return False, self.pause_reason or "paused"
        if self.spent(now_ms) + weight > self.max_weight:
            return False, "budget"
        return True, "ok"

    def charge(self, weight: int, now_ms: int) -> None:
        self._events.append((int(now_ms), int(weight)))

    def observe_used_weight(self, used: int | None, now_ms: int) -> None:
        """Back off when the IP's own 1-minute counter is already high."""
        if used is None:
            return
        self.last_used_weight = int(used)
        if self.last_used_weight > self.pause_above:
            self.pause_for(now_ms, self.window_ms, "used_weight")

    def observe_rejection(self, status: int, retry_after_seconds: float | None, now_ms: int) -> None:
        """Pause for Retry-After. A missing header still pauses. No retry here."""
        status = int(status)
        if retry_after_seconds is None or retry_after_seconds <= 0:
            wait_ms = DEFAULT_418_PAUSE_MS if status == 418 else DEFAULT_429_PAUSE_MS
        else:
            wait_ms = int(float(retry_after_seconds) * 1000)
        wait_ms = max(int(wait_ms), 1000)
        reason = "http_418" if status == 418 else "http_429"
        self.pause_for(now_ms, wait_ms, reason)

    def pause_for(self, now_ms: int, wait_ms: int, reason: str) -> None:
        until = int(now_ms) + max(0, int(wait_ms))
        if until >= self.pause_until_ms:
            self.pause_until_ms = until
            self.pause_reason = reason

    def retry_in_ms(self, now_ms: int) -> int:
        """How long fapi should stay quiet. Zero means a request may be sent."""
        now_ms = int(now_ms)
        if now_ms < self.pause_until_ms:
            return self.pause_until_ms - now_ms
        self._trim(now_ms)
        if self._events and self.spent(now_ms) >= self.max_weight:
            oldest = self._events[0][0]
            return max(0, oldest + self.window_ms - now_ms)
        return 0

    def _trim(self, now_ms: int) -> None:
        # An event ages out once it is a full window old. `ts == now - window` is outside.
        cutoff = int(now_ms) - self.window_ms
        self._events = [(ts, weight) for ts, weight in self._events if ts > cutoff]
