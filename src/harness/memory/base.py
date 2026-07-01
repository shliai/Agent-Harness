from __future__ import annotations

from abc import ABC, abstractmethod

from harness.domain.models import AgentMessage


class AbstractMemory(ABC):
    @abstractmethod
    def add(self, message: AgentMessage) -> None:
        ...

    @abstractmethod
    def get_context(self) -> list[AgentMessage]:
        ...

    @abstractmethod
    def clear(self) -> None:
        ...
