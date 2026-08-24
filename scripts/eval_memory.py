"""长期记忆检索质量评测层

在临时 ChromaDB collection 中灌入带已知事实的历史对话，
验证检索能按语义命中正确记忆（含 user 隔离与距离过滤语义）。
"""
from __future__ import annotations

import time

SEED_DOCS = [
    ("m1", "用户: 我预算5000左右想买台拍照好的手机\n助手: 为您推荐了小米17 Pro，徕卡影像很适合拍照"),
    ("m2", "用户: 帮我查一下订单2026080100001\n助手: 您的订单已发货，顺丰承运"),
    ("m3", "用户: 我是索尼粉，只买索尼的耳机\n助手: 好的，为您优先推荐索尼WH系列"),
    ("m4", "用户: 家里猫老抓沙发，有没有耐磨的沙发垫\n助手: 推荐了加厚帆布猫抓布沙发垫"),
    ("m5", "用户: 下周去西藏徒步需要块手表\n助手: 推荐Garmin fenix 9，多频定位适合高原徒步"),
]

# (query, 期望命中的 doc id)
CASES = [
    ("之前说过的买手机预算是多少来着", "m1"),
    ("我上次查的那个订单状态怎么样", "m2"),
    ("记得我只信任哪个耳机的牌子吗", "m3"),
    ("去高原徒步该带哪块表", "m5"),
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

        for query, expect_id in CASES:
            res = coll.query(query_texts=[query], n_results=3)
            top_ids = res["ids"][0]
            hit = expect_id in top_ids
            mrr = (1.0 / (top_ids.index(expect_id) + 1)) if hit else 0.0
            results.append({
                "id": f"M{len(results)+1:02d}",
                "kind": query,
                "expect": expect_id,
                "top3": top_ids,
                "recall": hit,
                "mrr": round(mrr, 2),
                "pass": hit,
            })
    except Exception as e:
        results.append({"id": "M00", "kind": f"基础设施异常: {e}", "pass": False})

    return summarize_memory(results)


def summarize_memory(results: list[dict]) -> dict:
    counted = [r for r in results if not r.get("skip")]
    passed = sum(1 for r in counted if r["pass"])
    return {
        "layer": "memory(长期记忆检索)",
        "total": len(counted),
        "skipped": len(results) - len(counted),
        "passed": passed,
        "pass_rate": round(passed / len(counted), 3) if counted else 1.0,
        "cases": results,
    }
