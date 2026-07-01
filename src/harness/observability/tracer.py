from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from harness.domain.models import ToolCall, ToolResult

logger = logging.getLogger("harness.observability.tracer")


class Tracer:
    """调用链追踪：记录 Agent 执行的每一步"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._log: list[dict[str, Any]] = []

    def record_step(
        self,
        step_index: int,
        thought: str,
        tool_call: ToolCall | None,
        tool_result: ToolResult | None,
    ) -> None:
        if not self.enabled:
            return

        record: dict[str, Any] = {
            "step": step_index,
            "timestamp": datetime.now().isoformat(),
            "thought": thought[:200],
        }
        if tool_call:
            record["tool_call"] = tool_call.model_dump()
        if tool_result:
            record["tool_result"] = tool_result.model_dump()

        self._log.append(record)
        logger.debug("Trace step %d: %s", step_index, thought[:60])

    def get_log(self) -> list[dict[str, Any]]:
        return self._log.copy()

    def clear(self) -> None:
        self._log.clear()
