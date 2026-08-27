"""长期记忆（学习机制）+ 工作记忆流 评测层

两部分：
1. eval_memory()   长期记忆（学习机制）确定性评测（零 LLM、无向量）：
                    直接驱动 WorkingMemory → learning_signals() → LearningStore，
                    断言 JSON 落盘、预算/偏好/约束/纠正导出、纠正覆盖同 key 偏好、
                    合并按 (type,key) 去重、render_for_prompt 全量召回、单用户单文件。
                    对应产品侧 harness.memory.learning.LearningStore（v0.7.7 起，
                    确定性、单用户、无 ChromaDB/向量）。
2. eval_wm_flow()  工作记忆流（L0 确定性，零 LLM）：直接驱动 WorkingMemory
                    .update_from_input 按 golden layer=memory_flow 用例逐步断言
                    预算写入/覆盖/临时上限不覆盖/订单与物流号去重。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from harness.memory.learning import LearningRecord, LearningStore
from harness.memory.working_memory import WorkingMemory


def _signals_to_records(signals: list[tuple[str, str, str]], evidence: str = "") -> list[LearningRecord]:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    return [
        LearningRecord(type=t, key=k, value=v, evidence=evidence, ts=ts)
        for t, k, v in signals
    ]


def _new_store() -> LearningStore:
    """临时目录 + 强制启用（评测只看机制本身，不受 settings.learning_enabled 影响）"""
    d = tempfile.mkdtemp(prefix="eval_learning_")
    store = LearningStore(store_path=Path(d))
    store.enabled = True
    return store


async def eval_memory() -> dict:
    """学习机制确定性评测：WorkingMemory → learning_signals → LearningStore 全链路断言。"""
    results: list[dict] = []

    # 1) 正向：预算 + 偏好 + 约束 导出并能被 render_for_prompt 召回
    try:
        store = _new_store()
        wm = WorkingMemory()
        wm.update_from_input("我喜欢索尼耳机，预算3000，对镍过敏", turn=1)
        for r in _signals_to_records(wm.learning_signals(), "我喜欢索尼耳机，预算3000，对镍过敏"):
            store.add(r)
        loaded = store.load()
        prompt = store.render_for_prompt()
        prompt_ok = (
            "预算上限=3000" in prompt
            and "品牌=索尼" in prompt
            and "对镍过敏" in prompt
        )
        ok = (
            store.count() >= 3
            and any(r.type == "constraint" for r in loaded)
            and prompt_ok
        )
        results.append({
            "id": "LM01", "kind": "正向导出+召回",
            "records": store.count(), "prompt_ok": prompt_ok, "pass": ok,
        })
    except Exception as e:
        results.append({"id": "LM01", "kind": f"异常: {e}", "pass": False})

    # 2) 纠正覆盖同 key 偏好（华为 → 苹果）：写纠正后不得残留品牌=华为 偏好
    #    注：学习记录 key 为中文「品牌」，非英文 brand
    try:
        store = _new_store()
        wm = WorkingMemory()
        wm.update_from_input("我只要华为牌手机", turn=1)  # 「只买…牌」触发偏好
        for r in _signals_to_records(wm.learning_signals()):
            store.add(r)
        wm.update_from_input("不是华为，是苹果", turn=2)  # 纠正覆盖
        for r in _signals_to_records(wm.learning_signals()):
            store.add(r)
        loaded = store.load()
        has_huawei_pref = any(
            r.type == "preference" and r.key == "品牌" for r in loaded
        )
        has_apple_corr = any(
            r.type == "correction" and r.key == "品牌" and "苹果" in r.value
            for r in loaded
        )
        ok = (not has_huawei_pref) and has_apple_corr
        results.append({
            "id": "LM02", "kind": "纠正覆盖同key偏好",
            "has_huawei_pref": has_huawei_pref, "has_apple_corr": has_apple_corr,
            "pass": ok,
        })
    except Exception as e:
        results.append({"id": "LM02", "kind": f"异常: {e}", "pass": False})

    # 3) 合并去重：同 (type,key) 覆盖而非翻倍（预算 3000 → 5000）
    try:
        store = _new_store()
        wm = WorkingMemory()
        wm.update_from_input("预算3000买手机", turn=1)
        for r in _signals_to_records(wm.learning_signals()):
            store.add(r)
        wm.update_from_input("预算改5000", turn=2)
        for r in _signals_to_records(wm.learning_signals()):
            store.add(r)
        budget_recs = [r for r in store.load() if r.key == "budget"]
        ok = len(budget_recs) == 1 and any("5000" in r.value for r in budget_recs)
        results.append({
            "id": "LM03", "kind": "合并去重",
            "budget_count": len(budget_recs),
            "value": budget_recs[0].value if budget_recs else "",
            "pass": ok,
        })
    except Exception as e:
        results.append({"id": "LM03", "kind": f"异常: {e}", "pass": False})

    # 4) 单用户：所有记录落同一 JSON，无 user_id 维度
    try:
        store = _new_store()
        wm = WorkingMemory()
        wm.update_from_input("我喜欢小米手机", turn=1)
        for r in _signals_to_records(wm.learning_signals()):
            store.add(r)
        ok = store.file_path.name == "learning.json" and store.count() >= 1
        results.append({
            "id": "LM04", "kind": "单用户单文件",
            "file": store.file_path.name, "pass": ok,
        })
    except Exception as e:
        results.append({"id": "LM04", "kind": f"异常: {e}", "pass": False})

    return summarize_memory(results, layer="memory(学习机制·确定性)")


async def eval_wm_flow(cases: list[dict]) -> dict:
    """工作记忆流：L0 确定性，直接驱动 WorkingMemory 规则抽取"""
    results = []
    for case in [c for c in cases if c["layer"] == "memory_flow"]:
        wm = WorkingMemory()
        checks = []
        for turn, step in enumerate(case["steps"], 1):
            wm.update_from_input(step["input"], turn)

            if step.get("assert_budget") is not None:
                ok = wm.budget_amount == step["assert_budget"]
                checks.append(("预算", step["assert_budget"], wm.budget_amount, ok))
            if step.get("assert_category") is not None:
                ok = wm.budget_category == step["assert_category"]
                checks.append(("品类", step["assert_category"], wm.budget_category, ok))
            if step.get("assert_orders"):
                missing = [o for o in step["assert_orders"] if o not in wm.order_ids]
                checks.append(("订单号", step["assert_orders"], wm.order_ids, not missing))
            if step.get("assert_tracking"):
                missing = [t for t in step["assert_tracking"] if t not in wm.tracking_nos]
                checks.append(("物流号", step["assert_tracking"], wm.tracking_nos, not missing))
            if step.get("assert_dedupe"):
                dup = (
                    len(wm.order_ids) != len(set(wm.order_ids))
                    or len(wm.tracking_nos) != len(set(wm.tracking_nos))
                )
                checks.append(("去重", "无重复实体", {"orders": wm.order_ids, "tracking": wm.tracking_nos}, not dup))

        ok = all(c[3] for c in checks)
        results.append({
            "id": case["id"],
            "checks": [
                {"name": c[0], "expect": c[1], "got": c[2], "ok": c[3]} for c in checks
            ],
            "pass": ok,
        })
    return summarize_memory(results, layer="wm_flow(工作记忆流)")


def summarize_memory(results: list[dict], layer: str = "memory(学习机制·确定性)") -> dict:
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
