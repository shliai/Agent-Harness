from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

from harness.config import settings
from harness.domain.models import ToolCall, ToolResult

logger = logging.getLogger("harness.observability.tracer")


class Tracer:
    """调用链追踪：记录 Agent 执行的每一步

    - 容量受 tracer_max_records 约束（deque 自动淘汰最旧记录），防内存泄漏
    - 每条记录带 session_id，支持按会话过滤，避免跨请求混淆
    """

    def __init__(self, enabled: bool = True, max_records: int | None = None) -> None:
        self.enabled = enabled
        limit = max_records if max_records is not None else settings.tracer_max_records
        self._log: deque[dict[str, Any]] = deque(maxlen=max(limit, 1))

    def record_step(
        self,
        step_index: int,
        thought: str,
        tool_call: ToolCall | None,
        tool_result: ToolResult | None,
        session_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return

        record: dict[str, Any] = {
            "step": step_index,
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "thought": (thought or "")[:200],
        }
        if tool_call:
            record["tool_call"] = tool_call.model_dump()
        if tool_result:
            record["tool_result"] = tool_result.model_dump()

        self._log.append(record)
        logger.debug("Trace step %d: %s", step_index, (thought or "")[:60])

    def get_log(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            return list(self._log)
        return [r for r in self._log if r.get("session_id") == session_id]

    def clear(self) -> None:
        self._log.clear()
