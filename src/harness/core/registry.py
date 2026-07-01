from __future__ import annotations

import logging
from typing import Any

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


