from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from harness.domain.models import ChatRole
from harness.core.loop import ReActLoop
from harness.guardrails.base import GuardrailPipeline
from harness.memory.conversation_history import ConversationHistory
from harness.memory.learning import LearningRecord, LearningStore
from harness.observability.metrics import MetricsCollector
from harness.observability.tracer import Tracer
from tests.conftest import MockLLMClient


class _FakeSettings:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_store(tmp: Path, **overrides: Any) -> LearningStore:
    cfg = {
        "learning_enabled": True,
        "learning_store_path": str(tmp),
        "learning_ttl_days": 365,
        "learning_max_items": 50,
        "learning_confidence_threshold": 0.0,
    }
    cfg.update(overrides)
    with patch("harness.memory.learning.settings", _FakeSettings(**cfg)):
        return LearningStore()


class TestLearningStoreDisabled:
    def test_disabled_noop(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, learning_enabled=False)
        store.add(LearningRecord(
            type="preference", key="brand", value="品牌=索尼", ts="2026-01-01T00:00:00"))
        assert store.load() == []
        assert store.count() == 0

    def test_disabled_render_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, learning_enabled=False)
        assert store.render_for_prompt() == ""


class TestLearningStoreEnabled:
    def test_add_and_load(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(LearningRecord(
            type="preference", key="brand", value="品牌=索尼", ts="2026-01-01T00:00:00"))
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].value == "品牌=索尼"
        block = store.render_for_prompt(loaded)
        assert "用户长期画像" in block
        assert "偏好：品牌=索尼" in block

    def test_correction_overrides_preference(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(LearningRecord(
            type="preference", key="brand", value="品牌=索尼", ts="2026-01-01T00:00:00"))
        store.add(LearningRecord(
            type="correction", key="brand", value="品牌=华为", ts="2026-01-02T00:00:00"))
        loaded = store.load()
        keys = {(r.type, r.key) for r in loaded}
        assert ("preference", "brand") not in keys
        assert ("correction", "brand") in keys
        assert any(r.value == "品牌=华为" for r in loaded)

    def test_merge_replace_same_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add(LearningRecord(
            type="preference", key="budget", value="预算上限=3000元", ts="2026-01-01T00:00:00"))
        store.add(LearningRecord(
            type="preference", key="budget", value="预算上限=5000元", ts="2026-01-02T00:00:00"))
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].value == "预算上限=5000元"

    def test_ttl_expiry(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, learning_ttl_days=1)
        store.add(LearningRecord(
            type="preference", key="brand", value="品牌=索尼", ts="2000-01-01T00:00:00"))
        assert store.load() == []

    def test_max_items_cap(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, learning_max_items=3)
        for i in range(5):
            store.add(LearningRecord(
                type="preference", key=f"k{i}", value=f"v{i}",
                ts=f"2026-01-0{i + 1}T00:00:00"))
        assert len(store.load()) == 3


def _build_loop(llm: MockLLMClient, store: LearningStore) -> ReActLoop:
    return ReActLoop(
        llm=llm,
        registry=MagicMock(),
        guardrails=GuardrailPipeline(),
        tracer=Tracer(enabled=False),
        metrics=MetricsCollector(),
        conversation_history=MagicMock(spec=ConversationHistory),
        max_iterations=3,
        learning_store=store,
    )


class TestLoopLearning:
    @pytest.mark.asyncio
    async def test_learning_injected_and_captured(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # 预置一条学习，验证其被注入系统提示词
        store.add(LearningRecord(
            type="preference", key="brand", value="品牌=索尼", ts="2026-01-01T00:00:00"))
        llm = MockLLMClient(response="好的，为您推荐索尼耳机")
        loop = _build_loop(llm, store)
        loop.conversation_history.aload_state.return_value = None

        result = await loop.execute("我喜欢索尼的耳机", session_id="learn-sid")
        await asyncio.sleep(0.05)

        injected = any(
            m.role == ChatRole.system and "用户长期画像" in m.content and "品牌=索尼" in m.content
            for call in llm.all_calls for m in call
        )
        assert injected, "系统提示词应注入长期学习画像"
        assert any(r.type == "preference" and r.key == "brand" for r in store.load())
        assert result.success is True

    @pytest.mark.asyncio
    async def test_disabled_no_injection(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, learning_enabled=False)
        llm = MockLLMClient(response="你好")
        loop = _build_loop(llm, store)
        loop.conversation_history.aload_state.return_value = None

        await loop.execute("你好", session_id="learn-off")
        injected = any(
            m.role == ChatRole.system and "用户长期画像" in m.content
            for call in llm.all_calls for m in call
        )
        assert not injected
