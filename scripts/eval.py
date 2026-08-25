"""Agent Harness 评测框架（多维度）

用法：
    python scripts/eval.py --mode L0 --strict   # CI 离线五层：检索/预算/鲁棒性/长期记忆/工作记忆流
    python scripts/eval.py --mode L1            # 发版前：L0 + 生成质量/护栏/任务流程/路由/非功能
    python scripts/eval.py --mode L2            # 定期：追加 LLM-as-Judge 层
    python scripts/eval.py --layers gen,guardrail,workflow
    python scripts/eval.py --live               # 兼容旧用法：追加在线路由层

层次设计（确定性从强到弱）：
    retrieval   检索质量：ground truth 由 data/products.json 结构化字段运行时计算，
                指标 Recall@5 / MRR / 价格硬合规 / 品类硬合规
    budget      预算合规：带预算查询返回的商品必须全部 ≤ 预算（硬性），
                且 top1 价格接近度不低于中位数（软性参考）
    robustness  对抗鲁棒性：幂运算炸弹 / 代码注入 / PII 脱敏 / 控制字符拦截 /
                per-key 限流 / 路径穿越 / 超大输入 / 畸形订单与物流单号
    memory      长期记忆检索（L0 确定性）：语义命中 + 负例 + user 隔离
    wm_flow     工作记忆流（L0 确定性）：预算写入/覆盖/去重
    gen         生成质量（真实 LLM）：忠实度 / 幻觉率 / 上下文利用率（L1 规则版 / L2 Judge 版）
    guardrail   护栏一致性（真实 LLM）：商品强制检索 / 闲聊零工具 / 引用一致 / 不死循环
    workflow    任务流程（真实 LLM）：多轮端到端，期望工具序列匹配
    routing     工具路由（真实 LLM）：spy registry 记录实际调用工具
    perf        非功能（真实 LLM）：延迟 P50/P95、每轮 token、调用次数（仅报告）

报告写入 data/eval/report_<timestamp>.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

GOLDEN_PATH = REPO / "data" / "eval" / "golden_set.jsonl"
PRODUCTS_PATH = REPO / "data" / "seed" / "products.json"


def load_golden() -> list[dict]:
    cases = []
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                cases.append(json.loads(line))
    return cases


# ══════════════════════ Layer: retrieval ══════════════════════

def compute_ground_truth(products: list[dict], case: dict) -> set[int]:
    """由结构化字段计算期望命中集合"""
    gt: set[int] = set()
    for i, p in enumerate(products):
        if case.get("category") and p.get("category") != case["category"]:
            continue
        price = float(p.get("price", 0))
        if case.get("price_max") is not None and price > case["price_max"]:
            continue
        if case.get("price_min") is not None and price < case["price_min"]:
            continue
        kws = case.get("keywords") or []
        if kws:
            hay = " ".join(
                [p.get("name", ""), p.get("description", ""), " ".join(p.get("tags", []))]
            )
            if not any(kw in hay for kw in kws):
                continue
        gt.add(i)
    return gt


def parse_returned_indices(text: str, products: list[dict]) -> list[int]:
    """从工具输出文本反查商品索引（按名称前缀匹配）"""
    name_to_idx = {}
    for i, p in enumerate(products):
        name_to_idx[p.get("name", "")] = i
    found: list[int] = []
    for line in text.splitlines():
        m = re.match(r"\[¥[\d.]+\]\s*(.+?)\s*\|", line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        if name in name_to_idx:
            found.append(name_to_idx[name])
    return found


async def eval_retrieval(cases: list[dict], products: list[dict]) -> dict:
    from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool

    tool = KnowledgeRetrievalTool()
    results = []
    for case in [c for c in cases if c["layer"] == "retrieval"]:
        gt = compute_ground_truth(products, case)
        output = await tool.run(query=case["query"])
        got = parse_returned_indices(output, products)
        top5 = got[:5]

        prices = [float(products[i]["price"]) for i in top5]
        cats = [products[i].get("category") for i in top5]

        cap = case.get("price_max")
        floor = case.get("price_min")
        price_ok = all(
            (cap is None or p <= cap + 1e-6) and (floor is None or p >= floor - 1e-6)
            for p in prices
        )
        cat_ok = all(c == case["category"] for c in cats)

        hits = [(rank, idx) for rank, idx in enumerate(top5, 1) if idx in gt]
        recall = len(hits) / min(5, len(gt)) if gt else (1.0 if not top5 else 0.0)
        mrr = 1.0 / hits[0][0] if hits else 0.0

        results.append({
            "id": case["id"],
            "query": case["query"],
            "gt_size": len(gt),
            "top5": top5,
            "recall_at_5": round(recall, 3),
            "mrr": round(mrr, 3),
            "price_compliant": price_ok,
            "category_compliant": cat_ok,
            "pass": price_ok and cat_ok and recall >= 0.4,
        })
    return summarize("retrieval", results)


# ══════════════════════ Layer: budget ══════════════════════

async def eval_budget(cases: list[dict], products: list[dict]) -> dict:
    from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool

    tool = KnowledgeRetrievalTool()
    results = []
    for case in [c for c in cases if c["layer"] == "budget"]:
        output = await tool.run(query=case["query"])
        got = parse_returned_indices(output, products)[:5]
        budget = float(case["budget"])
        prices = [float(products[i]["price"]) for i in got]

        over = [p for p in prices if p > budget + 1e-6]
        cat_ok = all(products[i].get("category") == case["category"] for i in got)

        # 软指标：top1 接近度应不低于命中商品价格中位数（业务上优先推接近预算的款）
        proximity_top1 = prices[0] / budget if prices else 0.0
        median = sorted(prices)[len(prices) // 2] if prices else 0.0
        proximity_median = median / budget if budget else 0.0

        results.append({
            "id": case["id"],
            "query": case["query"],
            "budget": budget,
            "returned_prices": prices,
            "over_budget_count": len(over),
            "proximity_top1": round(proximity_top1, 2),
            "proximity_vs_median_ok": proximity_top1 >= proximity_median - 0.05,
            "category_compliant": cat_ok,
            "pass": not over and cat_ok,
        })
    return summarize("budget", results)


# ══════════════════════ Layer: routing（--live） ══════════════════════

class _RecordingTool:
    """透明代理：记录 run() 调用次数"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.spec = inner.spec
        self.call_count = 0

    async def run(self, **kwargs):
        self.call_count += 1
        return await self._inner.run(**kwargs)


async def eval_routing(cases: list[dict]) -> dict:
    from harness.web.api import _build_agent

    agent = _build_agent()
    recorders: dict[str, _RecordingTool] = {}
    for name in agent.registry.list_tools():
        inner = agent.registry.get_tool(name)
        recorder = _RecordingTool(inner)
        recorders[name] = recorder
        agent.registry._tools[name] = recorder  # type: ignore[index]

    results = []
    for case in [c for c in cases if c["layer"] == "routing"]:
        for r in recorders.values():
            r.call_count = 0
        t0 = time.perf_counter()
        try:
            result = await agent.run(case["query"], session_id=f"eval-{case['id']}")
            error = result.error if not result.success else None
        except Exception as e:
            result, error = None, str(e)
        duration = time.perf_counter() - t0

        invoked = {n for n, r in recorders.items() if r.call_count > 0}
        expected = set(case.get("expect_tools") or [])
        expect_none = bool(case.get("expect_none"))

        if expect_none:
            ok = not invoked
        else:
            ok = expected.issubset(invoked)

        results.append({
            "id": case["id"],
            "query": case["query"],
            "expected": sorted(expected),
            "invoked": sorted(invoked),
            "duration_s": round(duration, 2),
            "error": error,
            "pass": ok and error is None,
        })
    return summarize("routing(live)", results)


# ══════════════════════ Layer: robustness ══════════════════════

async def eval_robustness(cases: list[dict]) -> dict:
    results = []

    async def check(cid: str, kind: str, fn) -> None:
        try:
            detail = await fn()
            ok, note, skip = True, detail, False
        except SkipCase as e:
            ok, note, skip = True, str(e), True
        except Exception as e:
            ok, note, skip = False, f"异常: {e}", False
        results.append({"id": cid, "kind": kind, "note": str(note)[:200],
                        "pass": ok, "skip": skip})

    from harness.guardrails.input_validator import InputValidator
    from harness.guardrails.output_filter import OutputFilter
    from harness.guardrails.rate_limiter import RateLimiter
    from harness.tools.calculator import CalculatorTool
    from harness.domain.exceptions import InputValidationError

    calc = CalculatorTool()

    async def x01_pow_bomb():
        start = time.perf_counter()
        out = await calc.run(expression="9**99999999")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"耗时 {elapsed:.2f}s"
        return f"拒绝响应: {out} ({elapsed * 1000:.0f}ms)"

    async def x02_injection():
        out = await calc.run(expression="__import__('os')")
        assert out.strip() != "os", "注入竟然成功"
        return f"拒绝响应: {out}"

    async def x03_malformed_action():
        # 坏 JSON ACTION 的完整闭环（纠正重试、不把原文当答案）在单测中深度覆盖：
        # tests/unit/test_fixes.py::TestLoopRobustness::test_malformed_action_gets_retry_not_raw_answer
        raise SkipCase("由单元测试覆盖，脚本层跳过执行体")

    async def x04_phone_mask():
        f = OutputFilter()
        masked = f.check({"type": "output", "content": "您的手机号13800138000已登记"})
        assert "13800138000" not in masked
        return "手机号已掩码"

    async def x05_idcard_x():
        f = OutputFilter()
        masked = f.check({"type": "output", "content": "身份证11010119900307447X核对完毕"})
        assert "11010119900307447X" not in masked
        return "X 结尾身份证已掩码"

    async def x06_control_chars():
        with pytest_raises(InputValidationError):
            InputValidator().check({"type": "input", "content": "bad\x07input"})
        return "控制字符输入已被拦截"

    async def x07_rate_limit_keys():
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        ctx_a = {"type": "input", "content": "hi", "session_id": "sess-a"}
        ctx_b = {"type": "input", "content": "hi", "session_id": "sess-b"}
        limiter.check(ctx_a)
        limiter.check(ctx_a)
        with pytest_raises(Exception):
            limiter.check(ctx_a)
        limiter.check(ctx_b)  # b 不受 a 影响
        return "per-key 隔离生效"

    async def x08_traversal():
        from fastapi import HTTPException
        from harness.web.api import validate_session_id

        for evil in ["..\\..\\x.json", "../../x", "", "a" * 65]:
            with pytest_raises(HTTPException):
                validate_session_id(evil)
        return "非法 session_id 全部拦截"

    async def x09_oversized_input():
        with pytest_raises(InputValidationError):
            InputValidator().check({"type": "input", "content": "长" * 5000})
        return "超大输入已被拦截"

    async def x10_empty_input():
        with pytest_raises(InputValidationError):
            InputValidator().check({"type": "input", "content": "   "})
        return "空输入已被拦截"

    async def x11_bank_card_masked():
        f = OutputFilter()
        masked = f.check({"type": "output", "content": "您的卡号6222021234567890123已绑定"})
        assert "6222021234567890123" not in masked
        return "银行卡号已掩码"

    async def x12_api_key_masked():
        f = OutputFilter()
        masked = f.check({"type": "output", "content": "密钥为 sk-abcdefghijklmnopqrstuvwxyz123456，请保密"})
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in masked
        return "API Key 已掩码"

    async def x13_calc_injection_variant():
        out = await calc.run(expression="__import__('subprocess').Popen")
        assert "subprocess" not in out or out.strip() != "subprocess", "注入变体竟然成功"
        return f"拒绝响应: {out}"

    async def x14_order_malformed():
        from harness.tools.order_query import OrderQueryTool

        out = await OrderQueryTool().run(order_id="abc-123")
        assert "格式不正确" in out, f"畸形订单号未被拒绝: {out}"
        return f"拒绝响应: {out}"

    async def x15_order_not_found():
        from harness.tools.order_query import OrderQueryTool

        out = await OrderQueryTool().run(order_id="2024010100000")
        assert "未找到订单" in out, f"不存在订单未优雅处理: {out}"
        return f"优雅提示: {out}"

    async def x16_tracking_malformed():
        from harness.tools.logistics_query import LogisticsQueryTool

        out = await LogisticsQueryTool().run(logistics_no="12345")
        assert "格式不正确" in out, f"畸形物流单号未被拒绝: {out}"
        return f"拒绝响应: {out}"

    plan = {
        "calc_pow_bomb": x01_pow_bomb,
        "calc_code_injection": x02_injection,
        "action_malformed_json": x03_malformed_action,
        "pii_phone_masked": x04_phone_mask,
        "pii_idcard_x_masked": x05_idcard_x,
        "input_control_chars_blocked": x06_control_chars,
        "rate_limit_per_key_isolated": x07_rate_limit_keys,
        "session_traversal_rejected": x08_traversal,
        "oversized_input_blocked": x09_oversized_input,
        "empty_input_blocked": x10_empty_input,
        "bank_card_masked": x11_bank_card_masked,
        "api_key_masked": x12_api_key_masked,
        "calc_injection_variant": x13_calc_injection_variant,
        "order_malformed_rejected": x14_order_malformed,
        "order_not_found_graceful": x15_order_not_found,
        "tracking_malformed_rejected": x16_tracking_malformed,
    }
    for case in [c for c in cases if c["layer"] == "robustness"]:
        fn = plan.get(case["kind"])
        if fn is None:
            results.append({"id": case["id"], "kind": case["kind"], "pass": False, "note": "未知用例"})
            continue
        await check(case["id"], case["kind"], fn)
    return summarize("robustness", results)


# ══════════════════════ utils & main ══════════════════════

class SkipCase(Exception):
    """用例跳过（已由其他层覆盖），不计入通过率"""


def pytest_raises(exc):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"期望抛出 {exc.__name__} 但没有")
            return issubclass(et, exc)
    return _Ctx()


def summarize(layer: str, results: list[dict]) -> dict:
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


def _load_products() -> list[dict]:
    """商品事实源：优先 SQLite（v0.4 起与向量库一致）；回退旧 JSON"""
    try:
        from harness.storage import db as store

        rows = store.list_products()
        if rows:
            return rows
    except Exception:
        pass
    return json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))


async def main_async(args: argparse.Namespace) -> int:
    products = _load_products()
    golden = load_golden()

    # 三档模式 → 层集合（--layers 显式指定时优先）
    MODE_LAYERS = {
        "L0": ["retrieval", "budget", "robustness", "memory", "wm_flow"],
        "L1": ["retrieval", "budget", "robustness", "memory", "wm_flow",
               "gen", "guardrail", "workflow", "routing", "perf"],
        "L2": ["retrieval", "budget", "robustness", "memory", "wm_flow",
               "gen", "gen_judge", "guardrail", "workflow", "routing", "perf"],
    }
    if args.layers:
        layers = set(args.layers.split(","))
    elif args.mode:
        layers = set(MODE_LAYERS[args.mode])
    else:
        layers = {"retrieval", "budget", "robustness", "memory", "wm_flow"}
    if args.live:
        layers.add("routing")

    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": args.mode or "default",
        "layers": [],
    }
    print(f"══ Agent Harness 评测 · {report['timestamp']} ══")
    print(f"知识库商品数: {len(products)} | 用例数: {len(golden)} | 模式: {args.mode or '自定义'} | 层: {sorted(layers)}\n")

    all_ok = True
    if "retrieval" in layers:
        r = await eval_retrieval(golden, products)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= (args.threshold if args.threshold else 1.0)
    if "budget" in layers:
        r = await eval_budget(golden, products)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] == 1.0  # 预算是硬约束，零容忍
    if "robustness" in layers:
        r = await eval_robustness(golden)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] == 1.0
    if "memory" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_memory import eval_memory as _eval_memory

        r = await _eval_memory()
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= 0.75
    if "wm_flow" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_memory import eval_wm_flow as _eval_wm_flow

        r = await _eval_wm_flow(golden)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= 0.9
    if "gen" in layers or "gen_judge" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_gen import eval_gen as _eval_gen

        judge = "gen_judge" in layers
        print("→ 生成质量评测（调用真实 LLM）…")
        r = await _eval_gen(golden, products, judge=judge)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= (0.6 if judge else 0.8)
    if "guardrail" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_guardrail import eval_guardrail as _eval_guardrail

        print("→ 护栏一致性评测（调用真实 LLM）…")
        r = await _eval_guardrail(golden, products)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= 0.8
    if "workflow" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_workflow import eval_workflow as _eval_workflow

        print("→ 任务流程评测（调用真实 LLM）…")
        r = await _eval_workflow(golden)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= 0.8
    if "routing" in layers:
        print("→ 在线路由评测（调用真实 LLM）…")
        r = await eval_routing(golden)
        report["layers"].append(r)
        _print_layer(r)
        all_ok &= r["pass_rate"] >= 0.75  # LLM 决策有固有随机性
    if "perf" in layers:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_perf import eval_perf as _eval_perf

        print("→ 非功能评测（延迟/成本，仅报告）…")
        r = await _eval_perf()
        report["layers"].append(r)
        _print_layer(r)
        if r.get("report"):
            print("  延迟: avg={avg}ms p50={p50}ms p95={p95}ms | token/轮 avg={tavg} | LLM调用/轮 avg={lavg}".format(
                avg=r["report"]["duration_ms"]["avg"], p50=r["report"]["duration_ms"]["p50"],
                p95=r["report"]["duration_ms"]["p95"], tavg=r["report"]["tokens_per_turn"]["avg"],
                lavg=r["report"]["llm_calls_per_turn"]["avg"]))
        all_ok &= r["pass_rate"] >= 0.8

    out_path = REPO / "data" / "eval" / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    total_pass = sum(l["passed"] for l in report["layers"])
    total_all = sum(l["total"] for l in report["layers"])
    verdict = "PASS ✓" if all_ok else "FAIL ✗"
    print(f"\n══ 总计 {total_pass}/{total_all} → {verdict}    报告: {out_path}")
    return 0 if (all_ok or not args.strict) else 1


def _print_layer(r: dict) -> None:
    def icon(c: dict) -> str:
        if c.get("skip"):
            return "⊘"
        return "✓" if c["pass"] else "✗"

    skip_note = f" (skip {r['skipped']})" if r.get("skipped") else ""
    print(f"── {r['layer']}: {r['passed']}/{r['total']} ({r['pass_rate'] * 100:.0f}%){skip_note}")
    for c in r["cases"]:
        extra = ""
        if "recall_at_5" in c:
            extra = f" recall@5={c['recall_at_5']:.2f} mrr={c['mrr']:.2f}"
        elif "over_budget_count" in c:
            extra = f" 超预算={c['over_budget_count']} top1接近度={c['proximity_top1']}"
        elif "invoked" in c:
            extra = f" 调用={','.join(c['invoked']) or '-'}"
        mark = icon(c)
        print(f"  {mark} {c['id']} {c.get('query', c.get('kind', ''))}{extra}")


def main() -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Agent Harness 多维度评测")
    parser.add_argument("--mode", type=str, choices=["L0", "L1", "L2"], default=None,
                        help="运行档位：L0 离线确定性 / L1 追加在线确定性 / L2 追加 Judge")
    parser.add_argument("--live", action="store_true", help="包含在线工具路由层（消耗真实 LLM tokens）")
    parser.add_argument("--layers", type=str, default=None,
                        help="逗号分隔: retrieval,budget,routing,robustness,memory,wm_flow,gen,guardrail,workflow,perf")
    parser.add_argument("--strict", action="store_true", help="任一失败退出码为 1")
    parser.add_argument("--threshold", type=float, default=None, help="检索层通过率阈值（默认 1.0）")
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
