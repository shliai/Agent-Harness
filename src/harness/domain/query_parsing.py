"""商品查询文本解析（纯函数，无业务依赖）

从 knowledge_retrieval 抽出，供 tools / memory 共用：
memory.working_memory 只需要品类识别，不应为此反向依赖 tools 层。
依赖方向：tools/memory → domain（单向）。
"""
from __future__ import annotations

import re
from typing import Any

KNOWN_CATEGORIES = {
    "手机": "手机",
    "笔记本": "笔记本",
    "电脑": "笔记本",
    "耳机": "耳机",
    "平板": "平板",
    "穿戴": "穿戴",
    "手表": "穿戴",
    "手环": "穿戴",
}

# 中文数量词归一化：
# 1) "1万5" / "2万8" → 15000 / 28000（口语）
# 2) "1万"/"2.5万" → 数字展开
# 3) 裸 "万元以上/以内" → 10000 元（隐含 1 万）——必须先于数字展开，
#    否则 "1万以内" 会在裸替换后被拼成 "110000元以内"（bug）
_MONEY_NORMALIZERS = (
    (re.compile(r"(\d+(?:\.\d+)?)万(\d)(?![\d])"),
     lambda m: str(round(float(m.group(1)) * 10000 + int(m.group(2)) * 1000))),
    (re.compile(r"(\d+(?:\.\d+)?)\s*万"),
     lambda m: str(round(float(m.group(1)) * 10000))),
    (re.compile(r"万元?以上"), "10000元以上"),
    (re.compile(r"万元?[以内下]+"), "10000元以内"),
)


def normalize_money_text(text: str) -> str:
    for pattern, repl in _MONEY_NORMALIZERS:
        text = pattern.sub(repl, text)
    return text


def extract_category(text: str) -> str | None:
    """品类识别（确定性）：命中已知品类词即返回归一化品类名"""
    for keyword, cat in KNOWN_CATEGORIES.items():
        if keyword in text:
            return cat
    return None


def extract_filters(raw_query: str) -> dict[str, Any]:
    """从用户查询抽取价格区间与品类过滤条件"""
    query = normalize_money_text(raw_query)
    filters: dict[str, Any] = {}

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*以[下内]", query)
    if m:
        filters["price_max"] = float(m.group(1))

    m = re.search(r"(\d+(?:\.\d+)?)\s*[~-到至]\s*(\d+(?:\.\d+)?)\s*(?:元|块)", query)
    if m:
        filters["price_min"] = float(m.group(1))
        filters["price_max"] = float(m.group(2))

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*以上", query)
    if m:
        filters["price_min"] = float(m.group(1))

    # k/千 单位：3k / 3千 → 3000
    if "price_max" not in filters and "price_min" not in filters:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[kK千]", query)
        if m:
            value = float(m.group(1)) * 1000
            if 100 <= value <= 999999:
                filters["price_max"] = value

    # 精确预算表达：3999的手机 / 预算3999 / 3000块的 / 2000预算
    # 理解为"预算 X 元"，按价格上限 X 处理（允许检索到 ≤X 的商品）
    if "price_max" not in filters and "price_min" not in filters:
        m = re.search(
            r"(?:预算[^\d]{0,4}(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块)?\s*的|(\d+(?:\.\d+)?)\s*预算)",
            query,
        )
        if m:
            budget = float(next(g for g in m.groups() if g))
            # 只对合理的 3C 预算生效（100-99999 元），避免误匹配型号数字
            if 100 <= budget <= 99999:
                filters["price_max"] = budget

    category = extract_category(query)
    if category:
        filters["category"] = category

    return filters
