from __future__ import annotations

import logging

from harness.config import settings
from harness.domain.exceptions import ConfigError
from harness.llm.base import AbstractLLMClient

logger = logging.getLogger("harness.llm.factory")


class LLMFactory:
    @staticmethod
    def create() -> AbstractLLMClient:
        provider = settings.llm_provider
        logger.info("创建 LLM 客户端: provider=%s", provider)

        from harness.llm.openai_compatible import OpenAICompatibleClient

        if provider == "zhipu":
            return OpenAICompatibleClient(
                base_url=settings.zhipu_api_url,
                api_key=settings.zhipu_api_key,
                model=settings.zhipu_model,
            )

        if provider == "openai":
            return OpenAICompatibleClient(
                base_url=settings.openai_api_url,
                api_key=settings.openai_api_key,
                model=settings.openai_model,
            )

        raise ConfigError(f"不支持的 LLM 供应商: {provider}")
