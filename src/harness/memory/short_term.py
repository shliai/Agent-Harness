from __future__ import annotations

import logging

from harness.domain.models import AgentMessage
from harness.memory.base import AbstractMemory

logger = logging.getLogger("harness.memory.short_term")


class ShortTermMemory(AbstractMemory):
    """短期记忆：只追加列表，达到阈值后由外部显式压缩裁剪

    KV-cache 友好设计：窗口内不做滑动淘汰（淘汰会使前缀位移、
    打穿全部前缀缓存）。消息只追加；当规模达到 compress 阈值时，
    由 AgentLoop 将较旧部分压缩为滚动摘要并调用 trim_to() 裁剪，
    此后继续追加——两次压缩之间消息数组严格 append-only。

    track_full=True 时额外维护一份不裁剪的全量列表（all_messages()），
    供落盘与压缩统计使用。
    """

    def __init__(self, window_size: int = 100, track_full: bool = False) -> None:
        self.window_size = window_size
        self._messages: list[AgentMessage] = []
        self._full: list[AgentMessage] | None = [] if track_full else None

    def add(self, message: AgentMessage) -> None:
        self._messages.append(message)
        if self._full is not None:
            self._full.append(message)
        logger.debug("记忆添加: role=%s, content=%s...", message.role.value, message.content[:30])

    def get_context(self) -> list[AgentMessage]:
        return list(self._messages)

    def all_messages(self) -> list[AgentMessage]:
        """全量历史（track_full=True 时），否则退化为当前列表"""
        return list(self._full) if self._full is not None else self.get_context()

    def split_for_compression(self, keep_recent: int) -> tuple[list[AgentMessage], list[AgentMessage]]:
        """按保留条数切分为 (较旧部分, 最近部分)，供滚动摘要使用"""
        keep = max(keep_recent, 2)
        msgs = self.all_messages()
        return msgs[:-keep], msgs[-keep:]

    def trim_to(self, recent: list[AgentMessage]) -> None:
        """压缩后裁剪：仅保留最近部分（唯一会移除消息的入口）"""
        self._messages = list(recent)
        if self._full is not None:
            self._full = list(recent)
        logger.info("短期记忆已裁剪至 %d 条", len(recent))

    def clear(self) -> None:
        self._messages.clear()
        if self._full is not None:
            self._full.clear()
        logger.info("短期记忆已清空")
