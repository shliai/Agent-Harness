"""D5 任务流程评测层（端到端多轮）

按 golden layer=workflow 用例的多轮 turns 依次驱动同一会话（共享 session_id，
依赖工作记忆/长期记忆保持跨轮状态），逐轮断言：

- expect_tools：该轮必须被调用的工具（子集匹配，容忍额外调用）
- expect_order：该轮 invoked 序列须以期望工具为子序列（相对顺序一致，允许插入）
- 任务完成率：用例内所有轮全部通过才计完成；终止正确率 = 无多余重复调用

数据流：golden layer=workflow 用例 → 逐轮 agent.run() → 从 steps 提取工具序列 → 比对。
"""
from __future__ import annotations

from typing import Any


def _invoked_sequence(steps: list[Any]) -> list[str]:
    return [s.tool_call.tool_name for s in steps if s.tool_call is not None]


def _order_ok(invoked: list[str], expected: list[str]) -> bool:
    """expected 须以子序列形式出现在 invoked 中（顺序一致，允许中间插入其他工具）"""
    idx = 0
    for t in invoked:
        if idx < len(expected) and t == expected[idx]:
            idx += 1
    return idx == len(expected)


async def eval_workflow(cases: list[dict]) -> dict:
    from harness.web.api import _build_agent

    agent = _build_agent()
    results = []
    for case in [c for c in cases if c["layer"] == "workflow"]:
        session_id = f"eval-wf-{case['id']}"
        turn_rows = []
        all_ok = True
        for turn_no, turn in enumerate(case["turns"], 1):
            try:
                result = await agent.run(turn["query"], session_id=session_id)
                invoked = _invoked_sequence(result.steps)
                error = result.error if not result.success else None
            except Exception as e:
                invoked, error = [], str(e)

            expected = turn.get("expect_tools") or []
            expect_order = turn.get("expect_order", False)
            hit_ok = set(expected).issubset(invoked) if expected else True
            order_ok = _order_ok(invoked, expected) if expect_order else True
            ok = hit_ok and order_ok and error is None

            all_ok &= ok
            turn_rows.append({
                "turn": turn_no, "query": turn["query"],
                "expected": expected, "invoked": invoked,
                "hit_ok": hit_ok, "order_ok": order_ok,
                "error": error, "ok": ok,
            })

        # 终止正确率：期望工具被重复调用视为流程失控（如同一查询反复检索）
        expected_tools = {t for turn in case["turns"] for t in (turn.get("expect_tools") or [])}
        flat_invoked = [t for row in turn_rows for t in row["invoked"]]
        redundant = [t for t in flat_invoked if flat_invoked.count(t) > 1 and t == "knowledge_retrieval"]
        terminate_ok = not redundant and all_ok

        results.append({
            "id": case["id"], "turn_count": len(case["turns"]),
            "turns": turn_rows, "redundant_retrieval": len(redundant),
            "terminate_ok": terminate_ok, "pass": all_ok and terminate_ok,
        })

    return summarize_workflow("workflow(任务流程)", results)


def summarize_workflow(layer: str, results: list[dict]) -> dict:
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
