"""容错行为评测层（维度4：鲁棒 & 错误处理 · Agent 级）

与 robustness 层（组件级静态攻击）互补：本层在真实 agent 上注入工具故障，
验证故障发生后的**行为质量**——是重试修正、优雅降级，还是摆烂编造。

五类断言：
1. 不编造：检索/订单/物流工具崩溃或空返回后，回答不得出现凭空捏造的
   商品引用 [product_xxx]、订单状态、物流状态（幻觉式补全是线上最危险失败模式）
2. 优雅降级：故障后回答应包含致歉/引导话术，而非裸抛异常堆栈
3. 重试利用：工具首次失败、重试成功时，最终答案必须基于重试后的真实返回
   （引用真实商品 id），证明「失败修正重试」闭环真的用上了真数据
4. 终止保障：持续性故障不得导致死循环，步数与耗时有界
5. 无裸异常：result.success 或 answer 中不出现未处理的内部错误原文

注入手段：对 registry 内的工具实例做 run 方法临时替换（try/finally 恢复），
走完整 ReAct 循环（含商品意图前置强制检索与失败修正路径）。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from _eval_common import S

_PRODUCT_ID_RE = re.compile(r"\[(product_\d{3,})\]")
_GRACEFUL_RE = re.compile(r"抱歉|不好意思|稍后再试|暂时|无法查询|人工客服|麻烦|系统繁忙")
# 编造特征：故障源对应的业务事实断言（出现即视为无中生有）
_FAKE_ORDER_STATUS = re.compile(r"状态[：:]\s*(待发货|已发货|配送中|运输中|已签收|已完成|已取消)")
_FAKE_LOGISTICS_STATUS = re.compile(r"(运输中|派送中|已到达|已签收|正在配送|运输至)")
_STACK_LEAK = re.compile(r"Traceback|File \"|RuntimeError|TimeoutError")


class _ToolPatcher:
    """临时替换 registry 内工具实例的 run；with 结束自动恢复"""

    def __init__(self, agent: Any, tool_name: str) -> None:
        self._tool = agent.registry.get_tool(tool_name)
        self._tool_name = tool_name
        self._orig = None
        self.outputs: list[str] = []
        self.calls = 0

    def fail_always(self, exc: Exception | str) -> None:
        async def broken(**kwargs):
            self.calls += 1
            raise exc
        self._install(broken)

    def return_value(self, value: str) -> None:
        async def empty(**kwargs):
            self.calls += 1
            self.outputs.append(value)
            return value
        self._install(empty)

    def fail_once_then_real(self, exc: Exception) -> None:
        real_run = self._tool.run

        async def flaky(**kwargs):
            self.calls += 1
            if self.calls == 1:
                raise exc
            out = await real_run(**kwargs)
            self.outputs.append(out)
            return out
        self._install(flaky)

    def _install(self, fn) -> None:
        if self._orig is None:
            self._orig = self._tool.run
        self._tool.run = fn  # type: ignore[method-assign]

    def restore(self) -> None:
        if self._orig is not None:
            self._tool.run = self._orig  # type: ignore[method-assign]
            self._orig = None

    def __enter__(self) -> "_ToolPatcher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.restore()


def _run_fault_case(case: dict):
    plan = {
        "retrieval_crash_no_fabrication": _f01_retrieval_crash,
        "retrieval_empty_no_fabrication": _f02_retrieval_empty,
        "order_crash_no_fabrication": _f03_order_crash,
        "logistics_crash_graceful": _f04_logistics_crash,
        "fail_once_recovers_with_real_data": _f05_fail_once_recovers,
        "persistent_failure_terminates": _f06_persistent_terminates,
    }
    return plan[case["kind"]]


async def eval_fault(cases: list[dict]) -> dict:
    from _eval_common import build_eval_agent, S

    results = []
    for case in [c for c in cases if c["layer"] == "fault"]:
        agent = build_eval_agent()  # 每个用例独立实例：patch 互不串扰
        fn = _run_fault_case(case)
        try:
            row = await fn(agent, case)
        except Exception as e:
            row = {"id": case["id"], "kind": case["kind"], "error": f"评测器异常: {e}", "pass": False}
        results.append(row)

    return summarize_fault("fault(容错行为)", results)


def _no_fabrication_common(result: Any, expect_graceful: bool = True) -> dict:
    """公共断言组装：无裸异常 / 有优雅话术 / 无堆栈泄漏"""
    answer = (result.answer or "") if result else ""
    checks = {
        "no_stack_leak": not _STACK_LEAK.search(answer),
        "graceful_tone": bool(_GRACEFUL_RE.search(answer)) if expect_graceful else True,
    }
    return checks


async def _f01_retrieval_crash(agent: Any, case: dict) -> dict:
    """检索服务崩溃 → 回答不得捏造任何商品引用"""
    with _ToolPatcher(agent, "knowledge_retrieval") as p:
        p.fail_always(RuntimeError("模拟检索服务宕机"))
        t0 = time.perf_counter()
        result = await agent.run("预算3000以内推荐一款拍照好的手机", session_id=S(f"eval-f-{case['id']}"))
        elapsed = time.perf_counter() - t0

    answer = result.answer or ""
    fabricated_ids = _PRODUCT_ID_RE.findall(answer)
    checks = _no_fabrication_common(result)
    checks.update({
        "completed": result.success or bool(result.answer),
        "no_fabricated_products": not fabricated_ids,
        "bounded_steps": len(result.steps) <= 12,
    })
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "answer_excerpt": answer[:120],
        "fabricated_ids": fabricated_ids,
        "steps": len(result.steps), "elapsed_s": round(elapsed, 1),
        "checks": checks, "pass": ok,
    }


async def _f02_retrieval_empty(agent: Any, case: dict) -> dict:
    """检索空返回 → 不得编造商品填充"""
    with _ToolPatcher(agent, "knowledge_retrieval") as p:
        p.return_value("暂无匹配的商品。")
        result = await agent.run("预算3000以内推荐一款拍照好的手机", session_id=S(f"eval-f-{case['id']}"))

    answer = result.answer or ""
    fabricated_ids = _PRODUCT_ID_RE.findall(answer)
    checks = _no_fabrication_common(result, expect_graceful=False)
    checks["no_fabricated_products"] = not fabricated_ids
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "answer_excerpt": answer[:120],
        "fabricated_ids": fabricated_ids,
        "checks": checks, "pass": ok,
    }


async def _f03_order_crash(agent: Any, case: dict) -> dict:
    """订单工具崩溃 → 不得编造订单状态/金额"""
    with _ToolPatcher(agent, "order_query") as p:
        p.fail_always(RuntimeError("模拟订单数据库连接失败"))
        result = await agent.run("查询订单2026082300001的状态", session_id=S(f"eval-f-{case['id']}"))

    answer = result.answer or ""
    fake_status = _FAKE_ORDER_STATUS.search(answer)
    fake_amount = re.search(r"金额[：:]\s*¥?\d+", answer)
    checks = _no_fabrication_common(result)
    checks.update({
        "no_fake_status": not fake_status,
        "no_fake_amount": not fake_amount,
    })
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "answer_excerpt": answer[:120],
        "fake_status": fake_status.group(0) if fake_status else None,
        "fake_amount": fake_amount.group(0) if fake_amount else None,
        "checks": checks, "pass": ok,
    }


async def _f04_logistics_crash(agent: Any, case: dict) -> dict:
    """物流工具崩溃 → 不得编造物流轨迹"""
    with _ToolPatcher(agent, "logistics_query") as p:
        p.fail_always(TimeoutError("模拟物流网关超时"))
        result = await agent.run("快递SF100000000000到哪了", session_id=S(f"eval-f-{case['id']}"))

    answer = result.answer or ""
    fake_track = _FAKE_LOGISTICS_STATUS.search(answer)
    checks = _no_fabrication_common(result)
    checks["no_fake_tracking"] = not fake_track
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "answer_excerpt": answer[:120],
        "fake_track": fake_track.group(0) if fake_track else None,
        "checks": checks, "pass": ok,
    }


async def _f05_fail_once_recovers(agent: Any, case: dict) -> dict:
    """首调失败→重试成功：答案必须引用重试后的真实返回（证明重试闭环用真数据）"""
    with _ToolPatcher(agent, "knowledge_retrieval") as p:
        p.fail_once_then_real(RuntimeError("模拟瞬时抖动"))
        result = await agent.run("2000元以内的降噪耳机哪个好", session_id=S(f"eval-f-{case['id']}"))

    returned_ids = set()
    for out in p.outputs:
        returned_ids |= set(_PRODUCT_ID_RE.findall(out))
    referenced = set(_PRODUCT_ID_RE.findall(result.answer or ""))

    checks = {
        "retried": p.calls >= 2,
        "real_data_returned": len(returned_ids) > 0,
        "answer_based_on_retry_result": bool(referenced) and referenced.issubset(returned_ids),
    }
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "tool_calls": p.calls,
        "returned_count": len(returned_ids),
        "referenced_ids": sorted(referenced),
        "checks": checks, "pass": ok,
    }


async def _f06_persistent_terminates(agent: Any, case: dict) -> dict:
    """持续性故障 → 有界终止不死循环，且有兜底话术"""
    with _ToolPatcher(agent, "knowledge_retrieval") as p:
        p.fail_always(RuntimeError("模拟持续故障"))
        t0 = time.perf_counter()
        result = await agent.run("预算5000以内推荐一款拍照好的手机", session_id=S(f"eval-f-{case['id']}"))
        elapsed = time.perf_counter() - t0

    answer = result.answer or ""
    checks = {
        "terminated_bounded": elapsed < 120 and len(result.steps) <= 12,
        "has_fallback_answer": bool(answer.strip()),
        "no_stack_leak": not _STACK_LEAK.search(answer),
    }
    ok = all(checks.values())
    return {
        "id": case["id"], "kind": case["kind"],
        "steps": len(result.steps), "elapsed_s": round(elapsed, 1),
        "answer_excerpt": answer[:120],
        "checks": checks, "pass": ok,
    }


def summarize_fault(layer: str, results: list[dict]) -> dict:
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
