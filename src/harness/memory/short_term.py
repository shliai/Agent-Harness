from __future__ import annotations

import logging
from collections import deque

from harness.domain.models import AgentMessage
from harness.memory.base import AbstractMemory

logger = logging.getLogger("harness.memory.short_term")


class ShortTermMemory(AbstractMemory):
    """短期记忆：滑动窗口，保留最近 N 轮对话"""

    def __init__(self, window_size: int = 20) -> None:
        self.window_size = window_size
        self._messages: deque[AgentMessage] = deque(maxlen=window_size)

    def add(self, message: AgentMessage) -> None:
        self._messages.append(message)
        logger.debug("记忆添加: role=%s, content=%s...", message.role.value, message.content[:30])

    def get_context(self) -> list[AgentMessage]:
        return list(self._messages)

    def clear(self) -> None:
        self._messages.clear()
        logger.info("短期记忆已清空")
