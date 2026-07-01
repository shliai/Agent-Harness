from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseGuardrail(ABC):
    name: str = "base"

    @abstractmethod
    def check(self, context: dict[str, Any]) -> str | None:
        ...


class GuardrailPipeline:
    """Guardrail 流水线：按顺序执行所有护栏检查"""

    def __init__(self) -> None:
        self.pipes: list[BaseGuardrail] = []

    def add(self, guardrail: BaseGuardrail) -> None:
        self.pipes.append(guardrail)

    def check_input(self, text: str) -> str:
        result = self._run_checks({"type": "input", "content": text})
        return result

    def check_output(self, text: str) -> str:
        result = self._run_checks({"type": "output", "content": text})
        return result

    def check_tool_output(self, text: str) -> str:
        result = self._run_checks({"type": "tool_output", "content": text})
        return result

    def _run_checks(self, context: dict[str, Any]) -> str:
        for guardrail in self.pipes:
            result = guardrail.check(context)
            if result is not None:
                return result
        return context.get("content", "")
