from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.guardrails.base import BaseGuardrail

logger = logging.getLogger("harness.guardrails.audit")


class AuditLogger(BaseGuardrail):
    """审计日志：记录所有 Guardrail 检查结果（passed / blocked）

    写文件是同步 IO，统一走线程池，避免阻塞事件循环。
    """

    name = "audit_logger"

    def __init__(self, log_path: Path = Path("./data/audit_logs")) -> None:
        self.log_path = log_path
        self.log_path.mkdir(parents=True, exist_ok=True)
        self._io_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()  # 持引用，防止任务被 GC

    def check(self, context: dict[str, Any]) -> str | None:
        self.record(
            type_=context.get("type"),
            content=context.get("content", ""),
            result="passed",
            session_id=context.get("session_id"),
        )
        return None

    def record_blocked(self, context: dict[str, Any], reason: str) -> None:
        """护栏拦截时调用：保证被拦截事件也留有审计记录"""
        self.record(
            type_=context.get("type"),
            content=context.get("content", ""),
            result="blocked",
            session_id=context.get("session_id"),
            reason=reason,
        )

    def record(
        self,
        type_: Any,
        content: str,
        result: str,
        session_id: Any = None,
        reason: str | None = None,
    ) -> None:
        from harness.guardrails.output_filter import mask_sensitive

        record: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "type": type_,
            "session_id": session_id,
            # 隐私合规：审计留存前对内容做敏感信息掩码（含用户原始输入）
            "content_preview": mask_sensitive(str(content))[:100],
            "result": result,
        }
        if reason:
            record["reason"] = str(reason)[:200]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_record(record)
            return
        task = loop.create_task(self._awrite_record(record))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _rotate_if_needed(self, path: Path) -> None:
        """单文件超过 audit_rotate_mb 时滚动为 _N 序号文件"""
        from harness.config import settings

        limit = getattr(settings, "audit_rotate_mb", 16) * 1024 * 1024
        if not path.exists() or path.stat().st_size < limit:
            return
        n = 1
        while path.with_suffix(f".{n}.jsonl").exists():
            n += 1
        path.rename(path.with_suffix(f".{n}.jsonl"))
        logger.info("审计日志已轮转 -> %s", path.with_suffix(f".{n}.jsonl").name)

    def _write_record(self, record: dict[str, Any]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.log_path / f"audit_{today}.jsonl"
        self._rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def _awrite_record(self, record: dict[str, Any]) -> None:
        async with self._io_lock:
            await asyncio.to_thread(self._write_record, record)
