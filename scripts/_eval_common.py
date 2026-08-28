"""评测专用 Agent 构建器（全部在线评测层共用）

与线上 `_build_agent` 的唯一差异：**禁用持久学习记忆（LearningStore）**。
理由（2026-08-26 定位确认，彼时为 ChromaDB 长期记忆，现已重构为确定性学习机制）：
1. 评测运行会把当轮确定性信号写入持久学习记忆（单用户 JSON）；
2. 后续用例/后续轮次召回这些残渣注入「相关历史记忆」，直接干扰工具路由
   （实测：同一条查询，启用污染记忆 invoked=[]，禁用后正确调用 order_query）；
3. 这使 tooluse/routing/workflow 分数随历史运行次数漂移（观测到 8/8→0/8→5/8）。

评测要度量的是 Agent 冷启动能力，因此读/写全部关闭；
短期窗口、工作记忆槽位、会话内多轮状态不受影响（session_id 相互独立）。
"""
from __future__ import annotations


def build_eval_agent():
    from harness.web.api import _build_agent

    agent = _build_agent()
    store = getattr(agent, "learning_store", None)
    if store is not None:
        store.enabled = False
    return agent


import time

# 每次评测进程唯一的运行戳：保证 session_id 不与历史运行冲突，
# 杜绝「上次失败的对话被恢复进上下文」造成的跨轮污染
_RUN_STAMP = time.strftime("%Y%m%d%H%M%S")


def S(tag: str) -> str:
    return f"{tag}-{_RUN_STAMP}"


# ── 终态/提案工具（任务列表式 ReAct）：不计入领域工具调用 ──
try:
    from harness.core.loop import FINALIZE_TOOL, PLAN_TOOL
except Exception:  # pragma: no cover - 仅在离线单测极端情况触发
    FINALIZE_TOOL, PLAN_TOOL = "respond", "plan"

NON_DOMAIN_TOOLS = frozenset({FINALIZE_TOOL, PLAN_TOOL})


def domain_invoked(steps) -> list[str]:
    """从 steps 抽取「领域工具」调用序列，剔除终态(respond)/提案(plan)工具。

    任务列表式 ReAct 下，模型每轮都必须产出工具调用（终态用 respond、需确认用
    plan），但这些不是「业务领域工具」，不应计入 routing/guardrail/tooluse/workflow
    等层对领域工具调用的断言（否则闲聊零工具用例会因 respond 而误判为「有工具」）。
    """
    out: list[str] = []
    for s in steps:
        if s.tool_call is None:
            continue
        name = s.tool_call.tool_name
        if name in NON_DOMAIN_TOOLS:
            continue
        out.append(name)
    return out
