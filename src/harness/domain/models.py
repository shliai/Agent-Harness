from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChatRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class AgentMessage(BaseModel):
    role: ChatRole
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)

    def to_llm_format(self) -> dict[str, str]:
        msg: dict[str, str] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            msg["name"] = self.tool_name
        return msg


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{datetime.now().timestamp():.0f}")
    tool_name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    tool_call_id: str
    tool_name: str
    success: bool
    output: str
    duration_ms: float = 0.0


class StepRecord(BaseModel):
    step_index: int
    thought: str
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    round_index: int | None = None  # 同一轮模型推理产出的多个工具调用共享同一分组键
    timestamp: datetime = Field(default_factory=datetime.now)


class AgentResult(BaseModel):
    answer: str
    steps: list[StepRecord] = Field(default_factory=list)
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    success: bool = True
    error: str | None = None
