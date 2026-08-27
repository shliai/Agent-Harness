"""Agentic RAG 查询理解层：同义扩展(MQE-lite) + 工作记忆槽位注入"""
from __future__ import annotations

import re

# 领域同义词表：键为用户高频词，值为可替换表述（每词取首个替换生成变体）
SYNONYMS: dict[str, list[str]] = {
    "拍照": ["影像", "相机"],
    "游戏": ["电竞", "性能"],
    "小屏": ["小巧", "紧凑"],
    "运动": ["不入耳", "挂耳"],
    "轻薄": ["便携", "轻便"],
    "性价比": ["实惠"],
    "降噪": ["消噪"],
    "办公": ["商务", "生产力"],
    # R17/R19 弱项补充：细分品类词（BGE-small 对英文缩写/细分子类语义偏弱，
    # 依赖同义变体多路召回头戴/HiFi 类商品）
    "hifi": ["高保真", "音质"],
    "头戴": ["耳罩", "包耳"],
    "影音": ["观影", "追剧"],
    "手环": ["智能手环", "穿戴"],
}

_NUM_RE = re.compile(r"\d{3,}")


def has_price_cue(query: str) -> bool:
    """是否已包含价格线索（数字≥100 或 万/块/k 表述由上游归一化后体现）"""
    return bool(_NUM_RE.search(query))


def expand(query: str, budget_amount: float | None = None,
           category: str | None = None) -> list[str]:
    """生成检索变体列表（原查询在前，最多 4 条）

    规则：
    1. 预算槽位确定性注入——工作记忆中有预算且查询未含价格线索时，
        将「X 元以内」拼接到主查询（替代依赖 LLM 自觉的 prompt 方式）
    2. MQE-lite 同义扩展——对每个命中的同义词生成一个单替换变体，
        并对含 ≥2 个同义词的查询额外生成一个「全替换」组合变体，
        覆盖多属性表述（如「拍照游戏手机」→「影像电竞手机」）
    """
    main = query.strip()
    variants: list[str] = []

    if budget_amount and not has_price_cue(main):
        main = f"{main} {int(budget_amount)}元以内"
    variants.append(main)

    # 收集所有命中的同义词（大小写不敏感）
    subs_applied: list[tuple[str, str]] = []
    for word, subs in SYNONYMS.items():
        if re.search(re.escape(word), main, re.IGNORECASE):
            subs_applied.append((word, subs[0]))

    # 组合替换变体优先：一次替换全部同义词（多属性 query 的整体改写表述，如「拍照游戏手机」→「影像电竞手机」）
    if len(subs_applied) >= 2:
        combined = main
        for word, rep in subs_applied:
            combined = re.sub(re.escape(word), rep, combined, count=1, flags=re.IGNORECASE)
        variants.append(combined)

    # 单替换变体：每个同义词生成一个（提升单属性召回多样性）
    for word, rep in subs_applied:
        v = re.sub(re.escape(word), rep, main, count=1, flags=re.IGNORECASE)
        if v not in variants:
            variants.append(v)

    if category and category not in main and len(variants) < 5:
        variants.append(f"{main} {category}")

    return variants[:5]
