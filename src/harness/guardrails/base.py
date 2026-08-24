from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from harness.domain.exceptions import GuardrailError

if TYPE_CHECKING:
    from harness.guardrails.audit_logger import AuditLogger


class BaseGuardrail(ABC):
    name: str = "base"

    @abstractmethod
    def check(self, context: dict[str, Any]) -> str | None:
        ...


class GuardrailPipeline:
    """Guardrail 流水线：按顺序执行所有护栏检查

    - 任一护栏拦截（抛 GuardrailError）即短路停止，异常向上传播
    - 拦截事件自动写入审计日志（若流水线中配置了 AuditLogger）
    - context 可携带 session_id，供限流等护栏做按 key 隔离
    """

    def __init__(self) -> None:
        self.pipes: list[BaseGuardrail] = []
        self._audit: AuditLogger | None = None

    def add(self, guardrail: BaseGuardrail) -> None:
        from harness.guardrails.audit_logger import AuditLogger  # 局部导入避免循环依赖

        if isinstance(guardrail, AuditLogger):
            self._audit = guardrail
        self.pipes.append(guardrail)

    def check_input(self, text: str, session_id: str | None = None) -> str:
        return self._run_checks({"type": "input", "content": text, "session_id": session_id})

    def check_output(self, text: str, session_id: str | None = None) -> str:
        return self._run_checks({"type": "output", "content": text, "session_id": session_id})

    def check_tool_output(self, text: str, session_id: str | None = None) -> str:
        return self._run_checks(
            {"type": "tool_output", "content": text, "session_id": session_id}
        )

    def _run_checks(self, context: dict[str, Any]) -> str:
        for guardrail in self.pipes:
            try:
                result = guardrail.check(context)
            except GuardrailError as e:
                if self._audit is not None:
                    try:
                        self._audit.record_blocked(context, str(e))
                    except Exception:
                        pass  # 审计失败不应影响拦截本身
                raise
            if result is not None:
                return result
        return context.get("content", "")
