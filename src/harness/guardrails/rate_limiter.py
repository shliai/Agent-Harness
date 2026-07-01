from __future__ import annotations

import time
from typing import Any


from harness.domain.exceptions import RateLimitError
from harness.guardrails.base import BaseGuardrail


class RateLimiter(BaseGuardrail):
    """速率限制：基于滑动窗口的请求频率控制"""

    name = "rate_limiter"

    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") not in ("input",):
            return None

        now = time.time()
        window_start = now - self.window_seconds

        self._timestamps = [t for t in self._timestamps if t > window_start]

        if len(self._timestamps) >= self.max_requests:
            wait = int(self._timestamps[0] + self.window_seconds - now)
            raise RateLimitError(f"请求过于频繁，请 {wait} 秒后再试")

        self._timestamps.append(now)
        return None
