from __future__ import annotations

import logging
from collections import deque

from harness.domain.models import AgentMessage
from harness.memory.base import AbstractMemory

logger = logging.getLogger("harness.memory.short_term")


class ShortTermMemory(AbstractMemory):
    """短期记忆：滑动窗口，保留最近 N 轮对话

    track_full=True 时额外维护一份不裁剪的全量列表（all_messages()），
    供会话持久化与压缩使用：LLM 只看窗口内内容，落盘保留完整历史。
    """

    def __init__(self, window_size: int = 20, track_full: bool = False) -> None:
        self.window_size = window_size
        self._messages: deque[AgentMessage] = deque(maxlen=window_size)
        self._full: list[AgentMessage] | None = [] if track_full else None

    def add(self, message: AgentMessage) -> None:
        self._messages.append(message)
        if self._full is not None:
            self._full.append(message)
        logger.debug("记忆添加: role=%s, content=%s...", message.role.value, message.content[:30])

    def get_context(self) -> list[AgentMessage]:
        return list(self._messages)

    def all_messages(self) -> list[AgentMessage]:
        """全量历史（track_full=True 时），否则退化为当前窗口"""
        return list(self._full) if self._full is not None else self.get_context()

    def clear(self) -> None:
        self._messages.clear()
        if self._full is not None:
            self._full.clear()
        logger.info("短期记忆已清空")
