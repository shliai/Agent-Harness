"""D3 护栏一致性评测层（回归守护）

对 golden layer=guardrail 用例跑真实 agent，验证四类断言：

1. 商品信号强制检索：推荐/比价/参数/预算/品牌 → knowledge_retrieval 必须被调用，
   且 first_tool=true 的用例要求其为首个工具调用
2. 闲聊零工具：非商品咨询不得触发任何工具
3. 引用一致性：answer 中引用的 [product_xxx] 必须 ∈ 检索返回集合（防幻觉引用）
4. 不重复检索：单轮商品问题不会对同一输入反复触发检索死循环
   （knowledge_retrieval 调用次数 ≤ 阈值）

数据流：golden layer=guardrail 用例 → agent.run() → 从 steps 提取工具调用序列与检索结果 → 断言。
"""
from __future__ import annotations

import re
from typing import Any

_PRODUCT_ID_RE = re.compile(r"\[(product_\d{3,})\]")
# 单轮内重复检索上限：一次强制检索 + 至多一次重查属正常，超过视为死循环
_MAX_SINGLE_TURN_RETRIEVAL = 2


def _invoked_sequence(steps: list[Any]) -> list[str]:
    return [s.tool_call.tool_name for s in steps if s.tool_call is not None]


async def eval_guardrail(cases: list[dict], products: list[dict] | None = None) -> dict:
    from harness.web.api import _build_agent

    agent = _build_agent()
    results = []
    for case in [c for c in cases if c["layer"] == "guardrail"]:
        query = case["query"]
        try:
            result = await agent.run(query, session_id=f"eval-ga-{case['id']}")
            answer = result.answer or ""
            error = result.error if not result.success else None
        except Exception as e:
            results.append({
                "id": case["id"], "query": query, "error": str(e),
                "invoked": [], "expect_tools_ok": False, "first_tool_ok": False,
                "ref_consistent": False, "no_dead_loop": True, "pass": False,
            })
            continue

        invoked = _invoked_sequence(result.steps)
        expected = set(case.get("expect_tools") or [])
        expect_none = bool(case.get("expect_none"))
        first_tool = case.get("first_tool")

        # 1. 商品信号 → 期望工具命中；闲聊 → 零工具
        if expect_none:
            expect_tools_ok = not invoked
        else:
            expect_tools_ok = expected.issubset(invoked)
        first_tool_ok = True
        if first_tool and invoked:
            first_tool_ok = invoked[0] in expected

        # 2. 引用一致性：answer 引用的 id 必须在检索返回集合内
        returned_ids = set()
        for s in result.steps:
            if s.tool_call is not None and s.tool_result is not None \
                    and s.tool_call.tool_name == "knowledge_retrieval":
                returned_ids |= set(_PRODUCT_ID_RE.findall(s.tool_result.output))
        answer_ids = set(_PRODUCT_ID_RE.findall(answer))
        ref_consistent = answer_ids.issubset(returned_ids) if returned_ids else (not answer_ids)

        # 3. 不重复检索（防死循环）
        retrieval_count = sum(1 for t in invoked if t == "knowledge_retrieval")
        no_dead_loop = retrieval_count <= _MAX_SINGLE_TURN_RETRIEVAL

        ok = (
            expect_tools_ok and first_tool_ok and ref_consistent
            and no_dead_loop and error is None
        )
        results.append({
            "id": case["id"], "query": query, "error": error,
            "invoked": invoked,
            "expect_tools_ok": expect_tools_ok, "first_tool_ok": first_tool_ok,
            "ref_consistent": ref_consistent, "no_dead_loop": no_dead_loop,
            "pass": ok,
        })

    return summarize_guardrail("guardrail(护栏一致性)", results)


def summarize_guardrail(layer: str, results: list[dict]) -> dict:
    counted = [r for r in results if not r.get("skip")]
    passed = sum(1 for r in counted if r["pass"])
    return {
        "layer": layer,
        "total": len(counted),
        "skipped": len(results) - len(counted),
        "passed": passed,
        "pass_rate": round(passed / len(counted), 3) if counted else 1.0,
        "cases": results,
    }
