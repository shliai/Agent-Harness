from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator

from harness.domain.models import AgentMessage


class AbstractLLMClient(ABC):
    @abstractmethod
    def chat(self, messages: list[AgentMessage], temperature: float | None = None) -> str:
        ...

    @abstractmethod
    def stream_chat(self, messages: list[AgentMessage], temperature: float | None = None) -> Generator[str, None, None]:
        ...
