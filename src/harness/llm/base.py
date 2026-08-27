from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from harness.domain.models import AgentMessage


@dataclass
class LLMReply:
    """一次 LLM 调用的完整返回：内容 + 本次调用的 token 用量。

    token 用量随调用结果一起返回，避免共享可变状态在并发请求下串号。
    tool_calls：原生 function calling 返回的结构化工具调用（仅当调用方
    显式传入 tools 时才有）；文本协议模式下恒为 None。
    """

    content: str
    total_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)


class AbstractLLMClient(ABC):
    @abstractmethod
    async def chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None,
        tools: list[dict] | None = None, tool_call_sink: dict | None = None,
    ) -> LLMReply:
        ...

    @abstractmethod
    async def stream_chat_async(
        self, messages: list[AgentMessage], temperature: float | None = None,
        tools: list[dict] | None = None, tool_call_sink: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        ...
