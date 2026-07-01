from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.guardrails.base import BaseGuardrail

logger = logging.getLogger("harness.guardrails.audit")


class AuditLogger(BaseGuardrail):
    """审计日志：记录所有 Guardrail 检查结果"""

    name = "audit_logger"

    def __init__(self, log_path: Path = Path("./data/audit_logs")) -> None:
        self.log_path = log_path
        self.log_path.mkdir(parents=True, exist_ok=True)

    def check(self, context: dict[str, Any]) -> str | None:
        record = {
            "timestamp": datetime.now().isoformat(),
            "type": context.get("type"),
            "content_preview": context.get("content", "")[:100],
            "result": "passed",
        }
        self._write_record(record)
        return None

    def _write_record(self, record: dict[str, Any]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.log_path / f"audit_{today}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
