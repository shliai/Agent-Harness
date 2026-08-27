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
    """生成检索变体列表（原查询在前，最多 3 条）

    规则：
    1. 预算槽位确定性注入——工作记忆中有预算且查询未含价格线索时，
       将「X 元以内」拼接到主查询（替代依赖 LLM 自觉的 prompt 方式）
    2. MQE-lite 同义扩展——对每个可替换词生成一个变体查询
    """
    main = query.strip()
    variants: list[str] = []

    if budget_amount and not has_price_cue(main):
        main = f"{main} {int(budget_amount)}元以内"
    variants.append(main)

    for word, subs in SYNONYMS.items():
        # 大小写不敏感匹配与替换（用户可能写 HiFi/HIFI）
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        if pattern.search(main) and len(variants) < 3:
            v = pattern.sub(subs[0], main, count=1)
            if v not in variants:
                variants.append(v)

    if category and category not in main and len(variants) < 3:
        variants.append(f"{main} {category}")

    return variants
