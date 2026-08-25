"""D8 非功能评测层（延迟 / 成本）

对一组代表性查询跑真实 agent，统计：
- 端到端延迟：均值 / P50 / P95
- 成本：每轮 token 均值与中位数、LLM 调用次数（以步骤数近似）、工具调用次数

仅输出报告（不参与 pass/fail gate）；若所有样本均失败则报告 pass=False 提示。
"""
from __future__ import annotations

import time
from typing import Any

# 代表性负载：检索推荐 / 订单查询 / 闲聊 / 比价 / 计算
PERF_QUERIES = [
    ("budget-phone", "预算3000以内推荐一款拍照手机"),
    ("order-status", "查询订单20240601001的状态"),
    ("chitchat", "你好"),
    ("headphone", "2000元以内的降噪耳机哪个好"),
    ("calc", "帮我算一下(120+80)*3"),
]


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _tool_count(steps: list[Any]) -> int:
    return sum(1 for s in steps if s.tool_call is not None)


async def eval_perf() -> dict:
    from harness.web.api import _build_agent

    agent = _build_agent()
    samples = []
    for name, query in PERF_QUERIES:
        t0 = time.perf_counter()
        try:
            result = await agent.run(query, session_id=f"eval-perf-{name}")
            duration_ms = result.total_duration_ms or (time.perf_counter() - t0) * 1000
            tokens = result.total_tokens or 0
            llm_calls = len(result.steps) or 1
            tools = _tool_count(result.steps)
            error = result.error if not result.success else None
        except Exception as e:
            duration_ms = (time.perf_counter() - t0) * 1000
            tokens = llm_calls = tools = 0
            error = str(e)
        samples.append({
            "id": name, "query": query,
            "duration_ms": round(duration_ms, 1),
            "tokens": tokens, "llm_calls": llm_calls, "tools": tools,
            "error": error, "pass": error is None,
        })

    durations = sorted(s["duration_ms"] for s in samples)
    tokens_list = sorted(s["tokens"] for s in samples)
    llm_calls = sorted(s["llm_calls"] for s in samples)
    n = len(samples)
    failures = [s for s in samples if s["error"]]

    return {
        "layer": "perf(非功能·延迟/成本)",
        "total": n,
        "skipped": 0,
        "passed": n - len(failures),
        "pass_rate": round((n - len(failures)) / n, 3) if n else 1.0,
        "cases": samples,
        "report": {
            "duration_ms": {
                "avg": round(sum(s["duration_ms"] for s in samples) / n, 1) if n else 0.0,
                "p50": round(_percentile(durations, 0.5), 1),
                "p95": round(_percentile(durations, 0.95), 1),
            },
            "tokens_per_turn": {
                "avg": round(sum(s["tokens"] for s in samples) / n, 1) if n else 0.0,
                "median": round(_percentile(tokens_list, 0.5), 1),
            },
            "llm_calls_per_turn": {
                "avg": round(sum(s["llm_calls"] for s in samples) / n, 2) if n else 0.0,
                "p95": round(_percentile(llm_calls, 0.95), 2),
            },
        },
    }
