from __future__ import annotations

import logging
from harness.domain.exceptions import ToolNotFoundError
from harness.tools.base import BaseTool

logger = logging.getLogger("harness.core.registry")


class Registry:
    """全局注册中心：管理工具、LLM 等可插拔组件"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # ── 工具管理 ─────────────────────────────────

    def register_tool(self, tool: BaseTool) -> None:
        name = tool.spec.name
        self._tools[name] = tool
        logger.info("注册工具: %s — %s", name, tool.spec.description)

    def get_tool(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"工具未注册: {name}")
        return tool

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_tool_descriptions(self) -> str:
        lines: list[str] = []
        for name, tool in self._tools.items():
            lines.append(f"- {name}: {tool.spec.description}")
            lines.append(f"  参数: {tool.spec.parameters}")
        return "\n".join(lines)

    def get_tool_briefs(self) -> str:
        """紧凑版工具清单（每工具一行：名称 + 描述），供注入系统提示词。

        原生 function calling 下参数 schema 由 tools 载荷携带，提示词里
        只保留名称与职责描述——保证弱模型对工具的内联可见性，又不吃
        dict repr 的 token。
        """
        return "\n".join(
            f"- {tool.spec.name}：{tool.spec.description}"
            for tool in self._tools.values()
        )

    def get_openai_tools(self) -> list[dict]:
        """原生 function calling 的 tools 载荷：ToolSpec → OpenAI function schema"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in self._tools.values()
        ]


