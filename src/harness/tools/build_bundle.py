from __future__ import annotations

import json
import logging
import time
from typing import Any

from harness.tools.base import BaseTool, ToolSpec
from harness.memory.working_memory import WorkingMemory

logger = logging.getLogger("harness.tools.build_bundle")


class BuildBundleTool(BaseTool):
    """组合/套装/全家桶确定性生成工具：
    输入品牌、总预算、需覆盖的品类列表，自动并发检索每品类最优款并汇总输出。
    完全不依赖模型拆解，内部直接调用 knowledge_retrieval（结构化参数）聚合结果。
    """

    spec = ToolSpec(
        name="build_bundle",
        description=(
            "为组合/套装/全家桶/多套配置类需求生成方案。"
            "输入品牌、总预算、品类列表，自动并发检索每品类最优款并汇总；"
            "比模型手动拆解更可靠，适用于'3万配4套小米全家桶'等场景。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "brand": {
                    "type": "string",
                    "description": "品牌，如 小米/红米/华为",
                },
                "total_budget": {
                    "type": "number",
                    "description": "总预算上限（元）",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需覆盖的品类列表，如 ['手机', '笔记本', '平板', '耳机']",
                },
                "budget_allocation": {
                    "type": "object",
                    "description": "可选：每品类预算分配，如 {'手机': 10000, '笔记本': 15000}；未提供则均分",
                    "additionalProperties": {"type": "number"},
                },
                "top_k_per_category": {
                    "type": "integer",
                    "description": "每品类返回候选数，默认 3",
                    "default": 3,
                },
            },
            "required": ["brand", "total_budget", "categories"],
        },
    )

    def __init__(self) -> None:
        self._kr_tool = None  # 惰性初始化 knowledge_retrieval

    def _get_kr_tool(self):
        if self._kr_tool is None:
            from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool
            self._kr_tool = KnowledgeRetrievalTool()
        return self._kr_tool

    async def run(self, **kwargs: Any) -> str:
        brand = str(kwargs.get("brand") or "").strip()
        total_budget = float(kwargs.get("total_budget") or 0)
        categories = [str(c).strip() for c in (kwargs.get("categories") or []) if str(c).strip()]
        budget_allocation = kwargs.get("budget_allocation") or {}
        top_k = int(kwargs.get("top_k_per_category") or 3)

        if not brand:
            return "缺少品牌参数"
        if total_budget <= 0:
            return "总预算必须大于 0"
        if not categories:
            return "品类列表不能为空"

        # 预算分配：未指定则均分
        per_cat_budget = {}
        if budget_allocation:
            for cat in categories:
                if cat in budget_allocation:
                    per_cat_budget[cat] = float(budget_allocation[cat])
        if not per_cat_budget:
            avg = total_budget / len(categories)
            per_cat_budget = {cat: avg for cat in categories}

        # 并发检索每品类
        kr = self._get_kr_tool()
        results = {}
        for cat in categories:
            budget = per_cat_budget.get(cat, total_budget / len(categories))
            try:
                # 直接调用 knowledge_retrieval 结构化参数
                out = await kr.run(
                    category=cat,
                    brand=brand,
                    price_max=budget,
                    top_k=top_k,
                )
                results[cat] = {"budget": budget, "results": out}
                logger.info("build_bundle: %s (%s) 预算 %.0f 检索完成", cat, brand, budget)
            except Exception as e:
                logger.warning("build_bundle: %s 检索失败: %s", cat, e)
                results[cat] = {"budget": budget, "results": f"检索失败: {e}"}

        # 汇总输出
        lines = [f"【组合方案】{brand} 全家桶/套装配置（总预算 ≤{int(total_budget)} 元）", ""]
        grand_total = 0
        for cat in categories:
            cat_info = results[cat]
            budget = cat_info["budget"]
            lines.append(f"📦 {cat}（预算 ≤{int(budget)} 元）")
            lines.append(cat_info["results"])
            lines.append("")

        lines.append(f"—— 预算汇总：总上限 {int(total_budget)} 元，各品类上限之和 {sum(per_cat_budget.values()):.0f} 元")
        return "\n".join(lines)