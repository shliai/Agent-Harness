from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "required": [],
    })


class BaseTool(ABC):
    spec: ToolSpec

    def set_llm(self, llm: Any) -> None:
        pass

    @abstractmethod
    async def run(self, **kwargs: Any) -> str:
        ...
