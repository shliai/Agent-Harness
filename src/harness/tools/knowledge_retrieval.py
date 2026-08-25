from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from harness.config import settings
from harness.domain.exceptions import ToolExecutionError
from harness.llm import reranker
from harness.memory.embeddings import get_embed_fn
from harness.tools.query_enricher import expand as expand_query
from harness.tools.base import BaseTool, ToolSpec

logger = logging.getLogger("harness.tools.knowledge_retrieval")

COLLECTION_NAME = "ecommerce_knowledge"

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

# RRF 常数（业界标准值）：抑制排名靠后文档的贡献，避免分数被头部文档垄断
RRF_K = 60


class _EmptyKnowledgeBase(Exception):
    """知识库没有任何商品（count=0）"""


# ── 中文分词 ─────────────────────────────────────────────
# 优先使用 jieba（若安装）；否则回退到自包含的「ASCII 词 + CJK 二元组」分词器，
# 保证 BM25 关键字通道对中文查询真实生效。

try:  # pragma: no cover - 取决于运行环境是否安装 jieba
    import jieba  # type: ignore[import-untyped]

    _jieba = jieba.Tokenizer()
    _jieba.initialize()

    def tokenize(text: str) -> list[str]:
        return [t.strip().lower() for t in _jieba.cut(text) if t.strip()]

except ImportError:
    logger.info("未安装 jieba，使用内置分词器（ASCII 词 + CJK 二元组）")

    _TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]")

    def tokenize(text: str) -> list[str]:
        tokens: list[str] = []
        cjk_buf: list[str] = []
        for tok in _TOKEN_RE.findall(text):
            if "\u4e00" <= tok <= "\u9fff":
                cjk_buf.append(tok)
                continue
            tokens.extend(_flush_cjk(cjk_buf))
            tokens.append(tok.lower())
        tokens.extend(_flush_cjk(cjk_buf))
        return tokens

    def _flush_cjk(buf: list[str]) -> list[str]:
        """连续汉字切成二元组；单字原样保留"""
        if len(buf) <= 1:
            out = list(buf)
            buf.clear()
            return out
        bigrams = [buf[i] + buf[i + 1] for i in range(len(buf) - 1)]
        buf.clear()
        return bigrams


class BM25:
    """BM25 打分器。语料必须先用 tokenize() 分词后再传入"""

    def __init__(self, tokenized_docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.corpus_size = len(tokenized_docs)
        total_len = sum(len(d) for d in tokenized_docs)
        self.avgdl = (total_len / self.corpus_size) if self.corpus_size else 1.0
        if self.avgdl <= 0:
            self.avgdl = 1.0
        self.doc_freqs: list[Counter] = [Counter(d) for d in tokenized_docs]
        self.doc_lens = [len(d) for d in tokenized_docs]
        self.idf: dict[str, float] = {}
        df: Counter = Counter()
        for doc_counter in self.doc_freqs:
            df.update(doc_counter.keys())
        for term, count in df.items():
            self.idf[term] = math.log(1 + (self.corpus_size - count + 0.5) / (count + 0.5))

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        score = 0.0
        doc_counter = self.doc_freqs[doc_index]
        doc_len = self.doc_lens[doc_index] or 1
        for term in set(query_tokens):
            idf = self.idf.get(term)
            if not idf:
                continue
            tf = doc_counter.get(term, 0)
            if tf == 0:
                continue
            score += idf * (tf * (self.k1 + 1)) / (
                tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
            )
        return score


class KnowledgeRetrievalTool(BaseTool):
    """知识库检索工具：ChromaDB 向量 + BM25 混合检索（RRF 融合）

    - 向量通道：BGE 语义相似度，价格/品类过滤下推到 where 条件
    - 关键字通道：BM25（中文分词后真实生效）
    - 融合：Reciprocal Rank Fusion 按排名融合，无分数尺度问题
    - 预算场景：预算接近度加权，越接近预算上限排序越靠前
    - 全异步对外：ChromaDB 同步调用放线程池，不阻塞事件循环
    """

    spec = ToolSpec(
        name="knowledge_retrieval",
        description="检索电商商品知识库，用于回答商品信息、价格查询、参数对比、商品推荐等业务问题",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的问题，如'5000元以下的拍照手机'",
                }
            },
            "required": ["query"],
        },
    )

    def __init__(self) -> None:
        self.collection: chromadb.Collection | None = None
        self._embed_fn = None
        try:
            self.client = chromadb.PersistentClient(
                path=str(settings.knowledge_store_path),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._embed_fn = get_embed_fn()
            self.collection = self._get_or_create_collection()
            logger.info("KnowledgeRetrievalTool 初始化完成 (ChromaDB + BGE + BM25/RRF)")
        except Exception as e:
            logger.warning("知识库初始化失败，降级运行: %s", e)

    def _get_or_create_collection(self) -> chromadb.Collection | None:
        ef = self._embed_fn or None
        try:
            return self.client.get_collection(COLLECTION_NAME, embedding_function=ef)
        except (ValueError, chromadb.errors.NotFoundError):
            return self.client.create_collection(COLLECTION_NAME, embedding_function=ef)
        except Exception as e:
            logger.warning("无法获取或创建 ChromaDB 集合: %s", e)
            return None

    # ── 过滤条件抽取 ───────────────────────────────────

    @staticmethod
    def _extract_filters(raw_query: str) -> dict[str, Any]:
        # 中文数量词归一化：
        # 1) "万元以上/以内" → 10000 元（隐含 1 万）
        # 2) "1万"/"2.5万" → 数字展开
        raw_query = re.sub(r"万元?以上", "10000元以上", raw_query)
        raw_query = re.sub(r"万元?[以内下]+", "10000元以内", raw_query)
        # 口语 "1万5" / "2万8" → 15000 / 28000
        raw_query = re.sub(
            r"(\d+(?:\.\d+)?)万(\d)(?![\d])",
            lambda m: str(round(float(m.group(1)) * 10000 + int(m.group(2)) * 1000)),
            raw_query,
        )
        query = re.sub(
            r"(\d+(?:\.\d+)?)\s*万",
            lambda m: str(round(float(m.group(1)) * 10000)),
            raw_query,
        )
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

        for keyword, cat in KNOWN_CATEGORIES.items():
            if keyword in query:
                filters["category"] = cat
                break

        return filters

    @staticmethod
    def _build_where(filters: dict[str, Any]) -> dict[str, Any]:
        # 恒定附带在售状态：下架/删除的商品即使向量残留也不会被召回
        conditions: list[dict] = [{"status": "在售"}]
        if filters.get("category"):
            conditions.append({"category": filters["category"]})
        price_max = filters.get("price_max")
        if price_max is not None:
            try:
                conditions.append({"price": {"$lte": float(price_max)}})
            except (ValueError, TypeError):
                pass
        price_min = filters.get("price_min")
        if price_min is not None:
            try:
                conditions.append({"price": {"$gte": float(price_min)}})
            except (ValueError, TypeError):
                pass
        return conditions[0] if len(conditions) == 1 else {"$and": conditions}

    @staticmethod
    def _format_product(document: str, metadata: dict) -> str:
        """直接输出入库时的自然语言商品文本（seed 写入的就是这种格式）"""
        price = metadata.get("price")
        category = metadata.get("category", "")
        brand = metadata.get("brand", "")
        prefix = f"[¥{price}] " if price is not None else ""
        cat_tag = f" ({brand} {category})".rstrip() if (brand or category) else ""
        text = document if len(document) <= 400 else document[:400] + "…"
        stock = metadata.get("stock")
        stock_txt = ""
        try:
            n = int(stock) if stock is not None else None
        except (TypeError, ValueError):
            n = None
        if n is not None:
            stock_txt = "（暂无库存）" if n <= 0 else (
                f"（库存紧张：仅 {n} 件）" if n <= 5 else f"（库存 {n}）"
            )
        return f"{prefix}{text}{stock_txt}{cat_tag}"

    # ── 主流程 ─────────────────────────────────────────

    async def run(self, **kwargs: Any) -> str:
        user_query = str(kwargs.get("query", ""))
        if not user_query.strip():
            return "请输入有效的问题"

        if self.collection is None:
            return "知识库未就绪，请联系管理员导入数据"

        try:
            try:
                candidates = await self._retrieve_candidates(user_query)
            except _EmptyKnowledgeBase:
                return "知识库为空，暂无商品信息"

            if not candidates:
                return self._no_result_message(user_query)

            # ── Agentic 检索：同义变体多路召回（合并去重后统一精排）──
            from harness.tools.context import current_budget

            budget = current_budget.get()
            variants = [v for v in expand_query(user_query, budget) if v != user_query][:2]
            seen = {c["document"] for c in candidates}
            for v in variants:
                try:
                    extra = await self._retrieve_candidates(v)
                except Exception as e:
                    logger.warning("变体召回失败(跳过) %s: %s", v, e)
                    continue
                for c in extra:
                    if c["document"] not in seen:
                        candidates.append(c)
                        seen.add(c["document"])

            ranked = await asyncio.to_thread(self._rank, candidates, user_query)

            # ── 自校正：向量最远距离超阈值视为召回不相关 → 放宽价格重查 ──
            floor = settings.retrieval_relevance_floor
            best_dist = min(c["distance"] for c in ranked)
            relaxed_note = ""
            if best_dist > floor:
                relaxed = await self._retrieve_relaxed(user_query)
                if relaxed:
                    merged = {c["document"]: c for c in relaxed}
                    for c in ranked:
                        merged.setdefault(c["document"], c)
                    ranked = list(merged.values())
                    ranked = await asyncio.to_thread(self._rank, ranked, user_query)
                    relaxed_note = "（已按需求放宽价格条件重新匹配）\n\n"

            # ── LLM 精排（可开关；失败自动回退 RRF 序）──
            ranked = await reranker.rerank(
                f"{user_query}" + (f"（预算{int(budget)}元）" if budget else ""), ranked
            )

            top = ranked[: settings.retrieval_top_k]

            lines = []
            for d in top:
                pid = d["metadata"].get("db_id", "")
                suffix = f"  [{pid}]" if pid else ""
                lines.append(self._format_product(d["document"], d["metadata"]) + suffix)

            note = f"（已扩展同义表述补充召回）\n\n" if variants and not relaxed_note else relaxed_note
            return (
                f"{note}商品检索结果（共 {len(top)} 条，已按综合相关度排序）：\n\n"
                + "\n\n".join(lines)
            )
        except ToolExecutionError:
            raise
        except _EmptyKnowledgeBase:
            return "知识库为空，暂无商品信息"
        except Exception as e:
            raise ToolExecutionError(f"商品检索失败: {e}") from e

    @staticmethod
    def _no_result_message(user_query: str) -> str:
        filters = KnowledgeRetrievalTool._extract_filters(user_query)
        pm = filters.get("price_max")
        hint = (
            f"当前价位（≤{int(pm)}元）暂无匹配商品。"
            if pm else "未找到匹配商品。"
        )
        return (
            hint + "建议：告知我可接受的价位区间或更换品类，我将为您推荐最接近的款式；"
            "也可以直接说「放宽预算再找一次」。"
        )

    async def _retrieve_candidates(self, user_query: str) -> list[dict]:
        """第一阶段：向量召回候选池（价格/品类过滤下推到 where，线程池执行阻塞调用）"""
        filters = self._extract_filters(user_query)
        where_clause = self._build_where(filters)

        def _do() -> tuple[list[dict], bool] | None:
            count = self.collection.count()
            if count == 0:
                return ([], True)  # 知识库为空标记
            n_results = min(count, max(settings.retrieval_candidates, settings.retrieval_top_k))
            result = self.collection.query(
                query_texts=[user_query], n_results=n_results, where=where_clause
            )
            docs = (result or {}).get("documents", [[]])[0]
            metas = (result or {}).get("metadatas", [[]])[0]
            dists = (result or {}).get("distances", [[]])[0]
            ids = (result or {}).get("ids", [[]])[0]
            return (
                [
                    {"id": cid, "document": doc, "metadata": meta or {}, "distance": dist}
                    for cid, doc, meta, dist in zip(ids, docs, metas, dists)
                ],
                False,
            )

        candidates, kb_empty = await asyncio.to_thread(_do)
        if kb_empty:
            raise _EmptyKnowledgeBase()
        return candidates

    async def _retrieve_relaxed(self, user_query: str) -> list[dict]:
        """自校正二次召回：去掉价格约束、保留品类与在售状态"""
        filters = self._extract_filters(user_query)
        filters.pop("price_max", None)
        filters.pop("price_min", None)
        where_clause = self._build_where(filters)

        def _do():
            count = self.collection.count()
            if count == 0:
                return []
            n_results = min(count, settings.retrieval_top_k * 2)
            res = self.collection.query(query_texts=[user_query], n_results=n_results,
                                        where=where_clause)
            docs = (res or {}).get("documents", [[]])[0]
            metas = (res or {}).get("metadatas", [[]])[0]
            dists = (res or {}).get("distances", [[]])[0]
            return [{"document": d, "metadata": m or {}, "distance": dist}
                    for d, m, dist in zip(docs, metas, dists)]

        return await asyncio.to_thread(_do)

    @staticmethod
    def _rank(candidates: list[dict], user_query: str) -> list[dict]:
        """第二阶段精排（CPU 密集，由调用方放线程池执行）"""
        query_tokens = tokenize(user_query)
        bm25 = BM25([tokenize(d["document"]) for d in candidates])

        n = len(candidates)
        vec_order = sorted(range(n), key=lambda i: candidates[i]["distance"])
        bm25_scores = [bm25.score(query_tokens, i) for i in range(n)]
        kw_order = sorted(range(n), key=lambda i: -bm25_scores[i])

        vec_rank_of = {idx: rank for rank, idx in enumerate(vec_order)}
        kw_rank_of = {idx: rank for rank, idx in enumerate(kw_order)}

        alpha = settings.hybrid_search_alpha
        for i, cand in enumerate(candidates):
            rrf_kw = alpha / (kw_rank_of[i] + RRF_K + 1)
            rrf_vec = (1 - alpha) / (vec_rank_of[i] + RRF_K + 1)
            cand["hybrid_score"] = rrf_vec + rrf_kw

        # 意图词命中数作为一级排序键（强意图必须优先满足），
        # 相关度分数退为二级键——避免大目录下贴预算但不匹配意图的商品挤占头部
        from harness.tools.query_enricher import SYNONYMS

        intent_words = [w for w in SYNONYMS if w in user_query]
        if intent_words:
            for cand in candidates:
                hay = cand["document"]
                cand["_hits"] = sum(1 for w in intent_words if w in hay)

        price_max = KnowledgeRetrievalTool._extract_filters(user_query).get("price_max")

        def sort_key(c: dict):
            hits = c.get("_hits", 0) if intent_words else 0
            try:
                price = float(c["metadata"].get("price", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
            proximity = min(price / price_max, 1.0) if (price_max and price_max > 0) else 0.0
            base = 0.6 * c["hybrid_score"] + 0.4 * proximity if price_max else c["hybrid_score"]
            return (hits, round(base, 6))

        ranked = sorted(candidates, key=sort_key, reverse=True)
        return ranked
