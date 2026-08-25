from __future__ import annotations

import asyncio
import logging

from harness.config import settings
from harness.llm.base import AbstractLLMClient
from harness.llm.openai_compatible import OpenAICompatibleClient

logger = logging.getLogger("harness.llm.factory")

# 小模型旁路调用（事实抽取/重排）共享信号量：限制并发，
# 防止瞬时打满小模型限流触发 429 风暴。旁路调用统一 async with 持有。
cheap_semaphore = asyncio.Semaphore(3)


class LLMFactory:
    """统一 OpenAI v1 兼容接入：OpenAI / 智谱 / DeepSeek / 通义 / vLLM 自建等，
    仅需配置 OPENAI_API_URL / OPENAI_API_KEY / OPENAI_MODEL 三项。"""

    @staticmethod
    def create() -> AbstractLLMClient:
        logger.info(
            "创建 LLM 客户端: model=%s base=%s",
            settings.openai_model,
            settings.openai_api_url,
        )
        return OpenAICompatibleClient(
            base_url=settings.openai_api_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )

    @staticmethod
    def create_cheap() -> AbstractLLMClient | None:
        """小模型客户端（旁路低风险调用专用）；未配置 OPENAI_SMALL_MODEL 时返回 None。

        URL/KEY 留空则继承主配置（默认同一 OpenAI 兼容网关、仅换模型名）。
        """
        if not settings.openai_small_model:
            return None
        logger.info(
            "创建小模型客户端: model=%s base=%s",
            settings.openai_small_model,
            settings.openai_small_api_url or settings.openai_api_url,
        )
        return OpenAICompatibleClient(
            base_url=settings.openai_small_api_url or settings.openai_api_url,
            api_key=settings.openai_small_api_key or settings.openai_api_key,
            model=settings.openai_small_model,
        )
