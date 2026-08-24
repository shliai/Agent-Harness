from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from harness.domain.models import AgentMessage


@dataclass
class LLMReply:
    """一次 LLM 调用的完整返回：内容 + 本次调用的 token 用量。

    token 用量随调用结果一起返回，避免共享可变状态在并发请求下串号。
    """

    content: str
    total_tokens: int = 0


class AbstractLLMClient(ABC):
    @abstractmethod
    async def chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> LLMReply:
        ...

    @abstractmethod
    async def stream_chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None
    ) -> AsyncGenerator[str, None]:
        ...
