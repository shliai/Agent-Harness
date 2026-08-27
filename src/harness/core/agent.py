from __future__ import annotations

import logging
from typing import Any

from harness.config import settings
from harness.core.loop import ReActLoop
from harness.core.registry import Registry
from harness.domain.models import AgentResult
from harness.guardrails.base import GuardrailPipeline
from harness.llm.base import AbstractLLMClient
from harness.llm.factory import LLMFactory
from harness.memory.conversation_history import ConversationHistory
from harness.memory.learning import LearningStore
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from harness.tools.subtask_dispatch import SubTaskDispatchTool

logger = logging.getLogger("harness.core.agent")


class Agent:
    def __init__(
        self,
        llm: AbstractLLMClient | None = None,
        registry: Registry | None = None,
        guardrails: GuardrailPipeline | None = None,
        tracer: Tracer | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.llm = llm or LLMFactory.create()
        # 小模型（可空）：旁路低风险调用（事实抽取/重排）专用，省成本
        self.cheap_llm = LLMFactory.create_cheap()
        self.registry = registry or Registry()
        self.guardrails = guardrails or GuardrailPipeline()
        self.tracer = tracer or Tracer(enabled=settings.tracing_enabled)
        self.metrics = metrics or MetricsCollector()
        self.conversation_history = ConversationHistory()
        self.learning_store = LearningStore()
        self.loop = ReActLoop(
            llm=self.llm,
            cheap_llm=self.cheap_llm,
            registry=self.registry,
            guardrails=self.guardrails,
            tracer=self.tracer,
            metrics=self.metrics,
            conversation_history=self.conversation_history,
            max_iterations=settings.max_iterations,
            learning_store=self.learning_store,
        )
        self.registry.register_tool(
            SubTaskDispatchTool(llm=self.llm, registry=self.registry, guardrails=self.guardrails)
        )
        logger.info("Agent 初始化 | llm=%s tools=%d guardrails=%d",
                     type(self.llm).__name__,
                     len(self.registry.list_tools()),
                     len(self.guardrails.pipes))

    async def run(
        self, user_input: str, session_id: str | None = None, user_id: str | None = None
    ) -> AgentResult:
        return await self.loop.execute(user_input, session_id=session_id, user_id=user_id)

    def get_trace_log(self) -> list[dict[str, Any]]:
        return self.tracer.get_log()

    def metrics_summary(self) -> str:
        return self.metrics.summary()
