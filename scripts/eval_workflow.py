"""D5 任务流程评测层（端到端多轮）

按 golden layer=workflow 用例的多轮 turns 依次驱动同一会话（共享 session_id，
依赖工作记忆/长期记忆保持跨轮状态），逐轮断言：

- expect_tools：该轮必须被调用的工具（子集匹配，容忍额外调用）
- expect_any：多种合规路径任选其一（如售后诉求可先确认订单再提交）
- expect_order：该轮 invoked 序列须以期望工具为子序列（相对顺序一致，允许插入）
- 任务完成率：用例内所有轮全部通过才计完成；终止正确率 = 单轮内同一工具
  调用不超过 2 次（跨轮重复检索属合理行为，不计失控）

数据流：golden layer=workflow 用例 → 逐轮 agent.run() → 从 steps 提取工具序列 → 比对。
"""
from __future__ import annotations

from typing import Any

# 单轮内同一工具调用上限（与 guardrail 层 _MAX_SINGLE_TURN_RETRIEVAL 对齐）
_MAX_PER_TURN_SAME_TOOL = 2


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
    from _eval_common import build_eval_agent, S

    agent = build_eval_agent()
    results = []
    for case in [c for c in cases if c["layer"] == "workflow"]:
        session_id = S(f"eval-wf-{case['id']}")
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
            # expect_any：多种执行路径均可接受的轮次（如售后前先确认订单），
            # 任一备选工具集满足子集匹配即算命中
            if "expect_any" in turn:
                hit_ok = any(
                    set(alt or []).issubset(set(invoked)) for alt in turn["expect_any"]
                )
            else:
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

        # 终止正确率：死循环判定只看「单轮内」——同一轮对同一工具调用 >2 次
        # （与 guardrail 层阈值一致）才算失控；跨轮重复检索是合理的多轮行为
        # （如第二轮重新查价格/库存），不计入
        redundant = []
        for row in turn_rows:
            per_tool: dict[str, int] = {}
            for t in row["invoked"]:
                per_tool[t] = per_tool.get(t, 0) + 1
            for tool_name, cnt in per_tool.items():
                if cnt > _MAX_PER_TURN_SAME_TOOL:
                    redundant.append(f"turn{row['turn']}:{tool_name}x{cnt}")
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
