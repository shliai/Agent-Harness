from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from harness.domain.models import AgentMessage

logger = logging.getLogger("harness.memory.conversation_history")

MAX_AGE_HOURS = 24


class ConversationHistory:
    """会话历史持久化：按 session_id 将完整对话保存到 JSON 文件，回来时可恢复"""

    def __init__(self, base_path: Path = Path("./data/conversations")) -> None:
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._cleanup_old()

    def _cleanup_old(self) -> None:
        now = time.time()
        limit = MAX_AGE_HOURS * 3600
        removed = 0
        for p in self.base_path.glob("*.json"):
            if now - p.stat().st_mtime > limit:
                p.unlink()
                removed += 1
        if removed:
            logger.info("清理过期会话文件: %d 个", removed)

    def save(self, session_id: str, messages: list[AgentMessage]) -> Path:
        path = self.base_path / f"{session_id}.json"
        data = {
            "session_id": session_id,
            "updated_at": datetime.now().isoformat(),
            "messages": [m.model_dump() for m in messages],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        logger.debug("会话历史已保存: %s (%d 条)", path, len(messages))
        return path

    def load(self, session_id: str) -> list[AgentMessage] | None:
        path = self.base_path / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            messages = [AgentMessage(**m) for m in data.get("messages", [])]
            logger.info("会话历史已恢复: %s (%d 条)", path, len(messages))
            return messages
        except Exception as e:
            logger.warning("恢复会话历史失败: %s", e)
            return None

    def delete(self, session_id: str) -> None:
        path = self.base_path / f"{session_id}.json"
        if path.exists():
            path.unlink()
            logger.info("会话历史已删除: %s", path)

    def list_sessions(self) -> list[str]:
        return [p.stem for p in self.base_path.glob("*.json")]
