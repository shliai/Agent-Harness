from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any

logger = logging.getLogger("harness.observability.metrics")


class MetricsCollector:
    """指标采集：统计 Token 消耗、工具调用次数、耗时等

    - 进程级实例跨请求累加（只增不减），线程安全
    - ReActLoop 每个请求另建独立实例做请求级统计，互不污染
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._tool_call_count: Counter[str] = Counter()
            self._total_tokens: int = 0
            self._total_llm_calls: int = 0
            self._total_duration_ms: float = 0.0
            self._start_time: float = time.perf_counter()

    def record_llm_call(self, tokens: int = 0) -> None:
        with self._lock:
            self._total_llm_calls += 1
            self._total_tokens += max(tokens, 0)

    def record_tool_call(self, tool_name: str) -> None:
        with self._lock:
            self._tool_call_count[tool_name] += 1

    def record_duration(self, ms: float) -> None:
        with self._lock:
            self._total_duration_ms += ms

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_llm_calls": self._total_llm_calls,
                "total_tokens": self._total_tokens,
                "tool_call_counts": dict(self._tool_call_count),
                "total_duration_ms": round(self._total_duration_ms, 2),
                "uptime_seconds": round(time.perf_counter() - self._start_time, 2),
            }

    def summary(self) -> str:
        s = self.snapshot()
        lines = [
            "===== 指标统计 =====",
            f"LLM 调用次数: {s['total_llm_calls']}",
            f"Token 消耗: {s['total_tokens']}",
            f"工具调用: {s['tool_call_counts']}",
            f"总耗时: {s['total_duration_ms']}ms",
            f"运行时间: {s['uptime_seconds']}s",
        ]
        return "\n".join(lines)
