from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────
    llm_provider: Literal["zhipu", "openai"] = "zhipu"
    zhipu_api_key: str = Field(default="", alias="ZHIPU_API_KEY")
    zhipu_api_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4"
    zhipu_embedding_model: str = "embedding-2"

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_api_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # ── 生成参数 ─────────────────────────────────────
    temperature: float = 0.3  # 降低随机性，提高响应速度
    max_tokens: int = 2048   # 减少最大token数量

    # ── ReAct 循环 ───────────────────────────────────
    max_iterations: int = 6   # 减少最大迭代次数

    # ── 记忆 ─────────────────────────────────────────
    short_term_window: int = 20
    long_term_enabled: bool = False
    long_term_store_path: Path = Path("./data/memory_store")

    # ── Guardrails ──────────────────────────────────
    rate_limit_max_requests: int = 60
    rate_limit_window_seconds: int = 60

    # ── 工具重试 ────────────────────────────────────
    tool_max_retries: int = 1  # 减少重试次数

    # ── 知识库 ──────────────────────────────────────
    knowledge_store_path: Path = Path("./data/chroma_db")
    retrieval_top_k: int = 5
    hybrid_search_alpha: float = 0.5

    # ── 可观测性 ────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    tracing_enabled: bool = True

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level 必须是 {allowed} 之一，收到: {v}")
        return v.upper()

    @field_validator("knowledge_store_path", "long_term_store_path", mode="before")
    @classmethod
    def ensure_path(cls, v: str | Path) -> Path:
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
