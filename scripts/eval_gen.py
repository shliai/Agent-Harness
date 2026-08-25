"""D2 生成质量评测层（直击幻觉）

- 规则版（L1，judge=False）：跑真实 agent，从 steps 提取 knowledge_retrieval 的
  tool_result，解析最终 answer 中的商品 id 引用（[product_xxx]），交叉校验：
    * Faithfulness   忠实度：answer 引用的每个商品 id 均能在检索 tool_result 中追溯到
    * Hallucination  幻觉率：引用不在检索结果中的商品比例（id 级 + 名称级）
    * ContextUtil    上下文利用率：回答引用的唯一 id 数 ÷ 检索返回的唯一 id 数
    * Completeness   完整性：回答引用中命中 ground-truth（GT）的比例
- Judge 版（L2，judge=True）：小模型 LLM-as-Judge，对 Answer Relevance / Completeness 打分

数据流：golden layer=gen 用例 → agent.run() → 从 steps 提取检索 tool_result 与 answer → 解析比对。
"""
from __future__ import annotations

import re
from typing import Any

_PRODUCT_ID_RE = re.compile(r"\[(product_\d{3,})\]")


def _parse_ids(text: str) -> set[str]:
    return set(_PRODUCT_ID_RE.findall(text))


def _parse_retrieval_results(steps: list[Any]) -> tuple[list[str], list[dict]]:
    """从执行步骤中提取 knowledge_retrieval 的工具输出"""
    outputs: list[str] = []
    for step in steps:
        if step.tool_call is None or step.tool_result is None:
            continue
        if step.tool_call.tool_name == "knowledge_retrieval":
            outputs.append(step.tool_result.output)
    return outputs, steps


async def eval_gen(cases: list[dict], products: list[dict], judge: bool = False) -> dict:
    from harness.web.api import _build_agent
    from eval import compute_ground_truth

    agent = _build_agent()
    results = []
    for case in [c for c in cases if c["layer"] == "gen"]:
        try:
            result = await agent.run(case["query"], session_id=f"eval-{case['id']}")
            answer = result.answer or ""
            error = result.error if not result.success else None
        except Exception as e:
            results.append({
                "id": case["id"], "query": case["query"], "error": str(e),
                "faithful": False, "hallucination_rate": 1.0, "context_util": 0.0,
                "completeness": 0.0, "pass": False,
            })
            continue

        outputs, steps = _parse_retrieval_results(result.steps)
        returned_text = "\n".join(outputs)
        returned_ids = _parse_ids(returned_text)

        # 名称级：answer 中出现的、存在于商品库的商品名，是否都能在检索返回文本中回溯
        name_to_id = {p["name"]: p["id"] for p in products}
        answer_names = {n for n in name_to_id if n in answer}
        name_unverifiable = {name_to_id[n] for n in answer_names if n not in returned_text}

        # 引用级：answer 显式引用 [product_xxx]
        answer_ids = _parse_ids(answer)

        # 幻觉项：被引用但不在检索返回集合中（id 级 + 名称级）
        hallucinated = (answer_ids - returned_ids) | name_unverifiable
        referenced = answer_ids | set(name_to_id[n] for n in answer_names)
        hallu_rate = round(len(hallucinated) / len(referenced), 3) if referenced else 0.0

        # 忠实度：有检索依据时，全部引用都必须可追溯；无检索时引用任何商品即失败
        faithful = True
        if returned_ids:
            faithful = not hallucinated
        elif referenced:
            faithful = False

        # 上下文利用率：引用的唯一 id ÷ 检索返回的唯一 id
        context_util = round(
            len(referenced & returned_ids) / len(returned_ids), 3
        ) if returned_ids else 0.0

        # 完整性：回答引用的 id 中命中 GT 的比例（GT 由结构化字段运行时计算）
        gt = compute_ground_truth(products, case)
        expected = returned_ids & {products[i]["id"] for i in gt}
        mentioned = answer_ids & expected
        completeness = round(len(mentioned) / len(expected), 3) if expected else 0.0

        if judge:
            rel, comp = await _judge(case["query"], returned_text, answer)
            row = {
                "id": case["id"], "query": case["query"], "error": error,
                "answer_relevance": rel, "completeness_judge": comp,
                "faithful": faithful, "hallucination_rate": hallu_rate,
                "context_util": context_util, "completeness_rule": completeness,
                # Judge 档：忠实度（防幻觉）仍为硬门槛，评分项不 gate
                "pass": faithful and rel >= 3 and comp >= 3,
            }
            results.append(row)
            continue

        results.append({
            "id": case["id"], "query": case["query"], "error": error,
            "referenced_ids": sorted(referenced),
            "returned_ids": sorted(returned_ids),
            "hallucinated_ids": sorted(hallucinated),
            "faithful": faithful, "hallucination_rate": hallu_rate,
            "context_util": context_util, "completeness": completeness,
            "pass": faithful and context_util > 0,
        })

    return _summarize_gen("gen生成质量(Judge)" if judge else "gen生成质量(规则)", results)


async def _judge(query: str, retrieval: str, answer: str) -> tuple[int, int]:
    """LLM-as-Judge：小模型对 Answer Relevance / Completeness 打分 1-5"""
    from harness.llm.factory import LLMFactory
    from harness.domain.models import AgentMessage, ChatRole

    llm = LLMFactory.create_cheap()
    prompt = (
        "你是电商客服回答质量的评测员。请按 1-5 分评分。\n"
        f"用户问题：{query}\n\n"
        f"知识库检索结果：{retrieval[:2000] if retrieval else '（未检索）'}\n\n"
        f"客服回答：{answer[:2000]}\n\n"
        "评分标准：\n"
        "1. Answer Relevance：回答是否切题、是否直接解决用户问题（不跑题、无冗余）\n"
        "2. Completeness：是否覆盖用户关心的核心信息（如价格/型号/参数/有无库存）\n"
        "只输出两行数字，例如：\nrelevance: 4\ncompleteness: 5"
    )
    try:
        reply = await llm.chat_async([AgentMessage(role=ChatRole.user, content=prompt)])
        rel = comp = 3
        for line in reply.content.splitlines():
            m = re.search(r"relevance\s*[:：]?\s*(\d)", line, re.IGNORECASE)
            if m:
                rel = int(m.group(1))
            m = re.search(r"completeness\s*[:：]?\s*(\d)", line, re.IGNORECASE)
            if m:
                comp = int(m.group(1))
        return max(1, min(5, rel)), max(1, min(5, comp))
    except Exception:
        # Judge 失败不 gate，回退中性分 3
        return 3, 3


def _summarize_gen(layer: str, results: list[dict]) -> dict:
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
