from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.memory.session")


class SessionManager:
    """会话管理：会话创建、状态快照、恢复"""

    def __init__(self, base_path: Path = Path("./data/sessions")) -> None:
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def generate_session_id(user_id: str = "") -> str:
        raw = f"{user_id}-{datetime.now().isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def save_snapshot(self, session_id: str, state: dict[str, Any]) -> Path:
        path = self.base_path / f"{session_id}.json"
        state["_snapshot_time"] = datetime.now().isoformat()
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("会话快照已保存: %s", path)
        return path

    def load_snapshot(self, session_id: str) -> dict[str, Any] | None:
        path = self.base_path / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("会话快照已加载: %s", path)
            return data
        except Exception as e:
            logger.warning("加载会话快照失败: %s", e)
            return None
