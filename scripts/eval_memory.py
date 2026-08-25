"""长期记忆 + 工作记忆评测层

两部分：
1. eval_memory()   长期记忆检索质量：在临时 ChromaDB collection 中灌入带已知事实的
                   历史对话，验证检索按语义命中正确记忆；含 2 条负例（不相关查询
                   不得把无关记忆顶进 top3）与 user 隔离断言。
2. eval_wm_flow()  工作记忆流（L0 确定性，零 LLM）：直接驱动 WorkingMemory
                   .update_from_input 按 golden layer=memory_flow 用例逐步断言
                   预算写入/覆盖/临时上限不覆盖/订单与物流号去重。
"""
from __future__ import annotations

import time

SEED_DOCS = [
    ("m1", "用户: 我预算5000左右想买台拍照好的手机\n助手: 为您推荐了小米17 Pro，徕卡影像很适合拍照"),
    ("m2", "用户: 帮我查一下订单2026080100001\n助手: 您的订单已发货，顺丰承运"),
    ("m3", "用户: 我是索尼粉，只买索尼的耳机\n助手: 好的，为您优先推荐索尼WH系列"),
    ("m4", "用户: 家里猫老抓沙发，有没有耐磨的沙发垫\n助手: 推荐了加厚帆布猫抓布沙发垫"),
    ("m5", "用户: 下周去西藏徒步需要块手表\n助手: 推荐Garmin fenix 9，多频定位适合高原徒步"),
    ("m6", "用户: 今天天气真不错，适合出去走走\n助手: 是的，记得带伞防午后阵雨"),  # 负例-无关
    ("m7", "用户: 请问你们几点下班\n助手: 客服工作时间为每天9点到21点"),                 # 负例-无关
    ("m8", "用户: 收货地址是北京市朝阳区望京SOHO\n助手: 已为您记录默认收货地址"),
]

# (描述, query, 期望命中 doc id | 期望缺席 absent ids, 可选的 where 过滤)
POSITIVE_CASES = [
    ("预算语义命中", "之前说过的买手机预算是多少来着", {"expect": "m1"}),
    ("订单语义命中", "我上次查的那个订单状态怎么样", {"expect": "m2"}),
    ("品牌偏好命中", "记得我只信任哪个耳机的牌子吗", {"expect": "m3"}),
    ("场景语义命中", "去高原徒步该带哪块表", {"expect": "m5"}),
    ("地址语义命中", "我之前留的收货地址是哪里", {"expect": "m8"}),
]
NEGATIVE_CASES = [
    # 不相关查询不应把无关记忆顶进 top3
    ("无关查询不命中-预算", "今天天气怎么样要不要带伞", {"absent": ["m1", "m3"]}),
    ("无关查询不命中-投诉", "我想投诉你们的客服服务态度", {"absent": ["m3", "m5"]}),
]
ISOLATION_CASES = [
    # user 隔离：换个用户 id 过滤后，不应返回他用户的历史记忆
    ("用户隔离-预算", "之前说过的买手机预算是多少来着",
     {"where": {"user_id": "u1"}, "absent": ["m1"]}),
]


async def eval_memory() -> dict:
    """独立于 golden_set：内置种子对话 + 固定查询"""
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from harness.config import settings
    from harness.memory.embeddings import get_embed_fn

    results = []
    try:
        client = chromadb.EphemeralClient(
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        try:
            client.delete_collection("eval_lt")
        except Exception:
            pass
        coll = client.create_collection(
            "eval_lt", embedding_function=get_embed_fn(),
            metadata={"hnsw:space": "cosine"},
        )
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        coll.add(
            ids=[d[0] for d in SEED_DOCS],
            documents=[d[1] for d in SEED_DOCS],
            metadatas=[{"user_id": f"u{i % 2}", "ts": ts} for i, _ in enumerate(SEED_DOCS)],
        )

        def _query(query: str, where: dict | None = None) -> list[str]:
            kw: dict = {"query_texts": [query], "n_results": 3}
            if where:
                kw["where"] = where
            return coll.query(**kw)["ids"][0]

        # 正例：期望命中必须出现在 top3
        for label, query, cfg in POSITIVE_CASES:
            top_ids = _query(query, cfg.get("where"))
            expect_id = cfg["expect"]
            hit = expect_id in top_ids
            mrr = (1.0 / (top_ids.index(expect_id) + 1)) if hit else 0.0
            results.append({
                "id": f"M{len(results)+1:02d}", "kind": f"正例-{label}", "query": query,
                "expect": expect_id, "top3": top_ids,
                "recall": hit, "mrr": round(mrr, 2), "pass": hit,
            })

        # 负例：缺席 doc 不得出现在 top3
        for label, query, cfg in NEGATIVE_CASES:
            top_ids = _query(query)
            intrude = [a for a in cfg["absent"] if a in top_ids]
            results.append({
                "id": f"M{len(results)+1:02d}", "kind": f"负例-{label}", "query": query,
                "absent": cfg["absent"], "top3": top_ids,
                "intruded": intrude, "pass": not intrude,
            })

        # 用户隔离：where 过滤后他用户记忆不得串入
        for label, query, cfg in ISOLATION_CASES:
            top_ids = _query(query, cfg["where"])
            leak = [a for a in cfg["absent"] if a in top_ids]
            results.append({
                "id": f"M{len(results)+1:02d}", "kind": f"隔离-{label}", "query": query,
                "where": cfg["where"], "absent": cfg["absent"], "top3": top_ids,
                "leaked": leak, "pass": not leak,
            })
    except Exception as e:
        results.append({"id": "M00", "kind": f"基础设施异常: {e}", "pass": False})

    return summarize_memory(results)


async def eval_wm_flow(cases: list[dict]) -> dict:
    """工作记忆流：L0 确定性，直接驱动 WorkingMemory 规则抽取"""
    from harness.memory.working_memory import WorkingMemory

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


def summarize_memory(results: list[dict], layer: str = "memory(长期记忆检索)") -> dict:
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
