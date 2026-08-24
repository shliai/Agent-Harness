from __future__ import annotations

import logging

from harness.config import settings
from harness.llm.base import AbstractLLMClient
from harness.llm.openai_compatible import OpenAICompatibleClient

logger = logging.getLogger("harness.llm.factory")


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
