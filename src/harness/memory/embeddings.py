from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("harness.memory.embeddings")

# 模型路径解析：本地目录优先；无则返回 HF 模型 ID 让 sentence_transformers 自动下载
LOCAL_MODEL_DIR = "models/bge-small-zh-v1.5"
HF_MODEL_ID = "BAAI/bge-small-zh-v1.5"

import os as _os

def _resolve_model_path() -> str:
    if _os.path.isdir(LOCAL_MODEL_DIR) and _os.path.exists(
        _os.path.join(LOCAL_MODEL_DIR, "config.json")
    ):
        return LOCAL_MODEL_DIR
    return HF_MODEL_ID

MODEL_PATH = _resolve_model_path()

# 全局单例：整个进程共享一个 BGE 嵌入模型实例
# 避免多个模块（KnowledgeRetrievalTool、LongTermMemory）各自加载模型
_embed_fn: Any | None = None
_loaded = False


def get_embed_fn() -> Any | None:
    """获取共享的 BGE 嵌入模型单例

    首次调用时加载模型（约 21 秒），后续调用直接返回缓存实例。
    加载失败返回 None，调用方应自行降级处理。
    """
    global _embed_fn, _loaded
    if _loaded:
        return _embed_fn
    _loaded = True
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        logger.info("正在加载 BGE 嵌入模型: %s", MODEL_PATH)
        _embed_fn = SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
        logger.info("BGE 嵌入模型加载完成")
    except Exception as e:
        logger.warning("BGE 嵌入模型加载失败: %s", e)
        _embed_fn = None
    return _embed_fn


def warmup() -> None:
    """启动时主动预热 BGE 模型，避免首次请求时阻塞

    在 main.py 启动时调用，把模型加载放到服务启动阶段，
    这样首次请求时模型已在内存，响应不再等待加载。
    """
    get_embed_fn()
