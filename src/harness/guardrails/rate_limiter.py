from __future__ import annotations

import time
from typing import Any

from harness.domain.exceptions import RateLimitError
from harness.guardrails.base import BaseGuardrail


class RateLimiter(BaseGuardrail):
    """速率限制：滑动窗口 + 按 key（session_id）隔离，互不影响"""

    name = "rate_limiter"

    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
        max_keys: int = 10000,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys  # 防止恶意伪造 session_id 撑爆内存
        self._buckets: dict[str, list[float]] = {}

    def check(self, context: dict[str, Any]) -> str | None:
        if context.get("type") not in ("input",):
            return None

        key = str(context.get("session_id") or "anonymous")
        now = time.time()
        window_start = now - self.window_seconds

        # 惰性淘汰空桶，防止 key 无限增长
        if len(self._buckets) > self.max_keys:
            self._buckets = {
                k: ts for k, ts in self._buckets.items() if ts and ts[-1] > window_start
            }

        bucket = self._buckets.setdefault(key, [])
        bucket[:] = [t for t in bucket if t > window_start]

        if len(bucket) >= self.max_requests:
            wait = int(bucket[0] + self.window_seconds - now) + 1
            raise RateLimitError(f"请求过于频繁，请 {wait} 秒后再试")

        bucket.append(now)
        return None
