from __future__ import annotations

import logging
from typing import Any

from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.logistics_query")

class LogisticsQueryTool(BaseTool):
    spec = ToolSpec(
        name="logistics_query",
        description="查询物流轨迹，根据物流单号返回详细的运输节点和状态",
        parameters={
            "type": "object",
            "properties": {
                "logistics_no": {
                    "type": "string",
                    "description": "物流单号，如 SF1234567890",
                }
            },
            "required": ["logistics_no"],
        },
    )

    def __init__(self) -> None:
        from harness.tools.context import EnumerationGuard

        self._guard = EnumerationGuard(max_misses=8, window_seconds=900, cooldown_seconds=1800)

    async def run(self, **kwargs: Any) -> str:
        import asyncio

        from harness.storage import db as store
        from harness.tools.context import current_session_id

        import re as _re

        no = kwargs.get("logistics_no", "").strip()
        if not no:
            return "请输入物流单号"
        key = f"logistics_query:{current_session_id.get() or 'anonymous'}"
        blocked = self._guard.check(key)
        if blocked:
            logger.warning("物流查询熔断中: %s", key)
            return blocked

        if not _re.fullmatch(r"(?i)(SF|YT|ZTO|JD|EMS)\d{9,12}", no):
            self._guard.record_miss(f"logistics_query:{current_session_id.get() or 'a'}")
            return f"物流单号 {no} 格式不正确，应为承运商前缀+数字（如 SF1234567890），请核对后重试"

        tracking = await asyncio.to_thread(store.get_logistics, no)
        if not tracking:
            self._guard.record_miss(key)
            return f"未找到物流单号 {no} 的轨迹信息"
        self._guard.record_hit(key)

        lines = [f"物流单号：{no}"]
        latest_status = tracking[-1].split(maxsplit=1)[-1] if tracking else "未知"
        lines.append(f"最新状态：{latest_status}")
        lines.append("")
        lines.append("详细轨迹：")
        for i, point in enumerate(tracking, 1):
            lines.append(f"  {i}. {point}")

        logger.info("物流查询: %s -> %s", no, latest_status)
        return "\n".join(lines)
