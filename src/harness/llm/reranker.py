"""LLM as Reranker：RRF 粗排后用 LLM 对 top-N 做相关性精排

设计：
- 懒加载复用 LLMFactory 客户端（进程级单例）
- 失败/超时/解析失败一律回退 RRF 原序（永不阻塞主流程）
- 开关 settings.rerank_enabled，top-N 由 settings.rerank_top_n 控制
"""
from __future__ import annotations

import asyncio
import logging
import re

from harness.config import settings

logger = logging.getLogger("harness.llm.reranker")

_client = None


def _get_client():
    global _client
    if _client is None:
        from harness.llm.factory import LLMFactory

        _client = LLMFactory.create()
    return _client


def _parse_order(text: str, valid_ids: list[str]) -> list[str] | None:
    """从 LLM 输出中解析 id 排序列表；容错任意包裹符"""
    ids = re.findall(r"product_\d+", text)
    if not ids:
        return None
    seen, ordered = set(), []
    for i in ids:
        if i in valid_ids and i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered or None


async def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """对候选（已含 id/metadata/document）做 LLM 精排；失败时原样返回"""
    if not settings.rerank_enabled or len(candidates) < 6:
        return candidates

    top = candidates[: settings.rerank_top_n]
    lines = [
        f"{c['id']} | {c['metadata'].get('brand', '')}{c['metadata'].get('category', '')} "
        f"¥{c['metadata'].get('price', 0)} | {c['document'][:80]}"
        for c in top
    ]
    prompt = (
        "你是电商检索重排器。根据用户需求，对以下商品按相关度从高到低排序。\n"
        f"用户需求：{query}\n\n商品列表：\n" + "\n".join(lines) +
        "\n\n只输出按相关度排序的商品 id（形如 product_123），空格分隔，不要其他内容。"
    )
    try:
        from harness.domain.models import AgentMessage, ChatRole

        async with asyncio.timeout(12):
            reply = await _get_client().chat_async(
                [AgentMessage(role=ChatRole.system, content="只输出商品id序列。"),
                 AgentMessage(role=ChatRole.user, content=prompt)],
                temperature=0.0,
            )
        ordered_ids = _parse_order(reply.content, [c["id"] for c in top])
        if not ordered_ids:
            logger.warning("Rerank 输出无法解析，回退 RRF 序")
            return candidates

        by_id = {c["id"]: c for c in top}
        placed = [by_id[i] for i in ordered_ids]
        missing = [c for c in top if c["id"] not in set(ordered_ids)]
        rest = candidates[len(top):]
        reranked = placed + missing + rest
        logger.info("LLM 重排完成: top1=%s", reranked[0]["id"])
        return reranked
    except Exception as e:
        logger.warning("Rerank 失败回退 RRF 序: %s", e)
        return candidates
