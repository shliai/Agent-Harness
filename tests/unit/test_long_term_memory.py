from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.memory.long_term import LongTermMemory


class TestLongTermMemoryDisabled:
    """禁用状态下的行为：零开销，不加载 ChromaDB"""

    def test_disabled_by_default(self) -> None:
        """配置 long_term_enabled=False 时，enabled 应为 False"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = False
            mock_settings.long_term_store_path = "./data/memory_store_test"
            mem = LongTermMemory()
            assert mem.enabled is False
            assert mem.collection is None

    @pytest.mark.asyncio
    async def test_disabled_add_is_noop(self) -> None:
        """禁用状态下 add() 应静默返回，不抛异常"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = False
            mem = LongTermMemory()
            await mem.add("你好", "你好！有什么可以帮助您的吗？", "session-1")

    @pytest.mark.asyncio
    async def test_disabled_search_returns_empty(self) -> None:
        """禁用状态下 search() 应返回空列表"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = False
            mem = LongTermMemory()
            assert await mem.search("任意查询") == []

    def test_disabled_count_is_zero(self) -> None:
        """禁用状态下 count() 应返回 0"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = False
            mem = LongTermMemory()
            assert mem.count() == 0


class TestLongTermMemoryEnabled:
    """启用状态下的核心行为：写入、检索、计数"""

    @pytest.fixture
    def mock_collection(self) -> MagicMock:
        """Mock ChromaDB Collection，避免加载真实 BGE 模型"""
        coll = MagicMock()
        coll.count.return_value = 0
        return coll

    @pytest.fixture
    def enabled_memory(self, mock_collection: MagicMock) -> LongTermMemory:
        """构造一个启用状态、collection 被 mock 的 LongTermMemory"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = True
            mock_settings.long_term_store_path = "./data/memory_store_test"
            mock_settings.long_term_top_k = 3
            with patch("harness.memory.long_term.get_embed_fn", return_value=None):
                with patch.object(
                    LongTermMemory,
                    "_get_or_create_collection",
                    return_value=mock_collection,
                ):
                    mem = LongTermMemory()
                    mem.enabled = True
                    mem.collection = mock_collection
                    return mem

    def test_enabled_state(self, enabled_memory: LongTermMemory) -> None:
        """启用后 enabled 为 True，collection 不为 None"""
        assert enabled_memory.enabled is True
        assert enabled_memory.collection is not None

    @pytest.mark.asyncio
    async def test_add_writes_to_collection(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """add() 应调用 collection.add() 一次，写入文档和元数据"""
        await enabled_memory.add(
            user_input="查询订单 20240601001",
            assistant_answer="您的订单已发货，预计明日送达",
            session_id="session-abc",
        )
        mock_collection.add.assert_called_once()
        call_kwargs = mock_collection.add.call_args.kwargs
        assert "ids" in call_kwargs
        assert "documents" in call_kwargs
        assert "metadatas" in call_kwargs
        doc = call_kwargs["documents"][0]
        assert "查询订单 20240601001" in doc
        assert "您的订单已发货" in doc
        meta = call_kwargs["metadatas"][0]
        assert meta["session_id"] == "session-abc"

    @pytest.mark.asyncio
    async def test_add_empty_input_skipped(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """空 user_input 或空 assistant_answer 不应写入"""
        await enabled_memory.add("", "回答", "session-1")
        await enabled_memory.add("问题", "", "session-1")
        await enabled_memory.add("   ", "回答", "session-1")
        mock_collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_failure_does_not_raise(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """collection.add 抛异常时不应传播，应静默降级"""
        mock_collection.add.side_effect = RuntimeError("ChromaDB 写入失败")
        await enabled_memory.add("问题", "回答", "session-1")

    @pytest.mark.asyncio
    async def test_search_returns_top_k(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """search() 应返回检索结果，按 distance 升序"""
        mock_collection.count.return_value = 5
        mock_collection.query.return_value = {
            "documents": [["doc1", "doc2", "doc3"]],
            "metadatas": [[{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}]],
            "distances": [[0.1, 0.2, 0.3]],
        }
        results = await enabled_memory.search("查询订单")
        assert len(results) == 3
        assert results[0]["document"] == "doc1"
        assert results[0]["metadata"]["session_id"] == "s1"
        assert results[0]["distance"] == 0.1

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_empty(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """空查询应返回空列表，不调用 collection"""
        assert await enabled_memory.search("") == []
        assert await enabled_memory.search("   ") == []
        mock_collection.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_empty_store_returns_empty(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """记忆库为空（count=0）时应返回空列表"""
        mock_collection.count.return_value = 0
        assert await enabled_memory.search("任意查询") == []
        mock_collection.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_failure_returns_empty(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """query 抛异常时应返回空列表，不传播"""
        mock_collection.count.return_value = 5
        mock_collection.query.side_effect = RuntimeError("查询失败")
        assert await enabled_memory.search("任意查询") == []

    @pytest.mark.asyncio
    async def test_search_top_k_clamped_by_count(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """n_results 应为 min(top_k, count)，避免请求超过实际条数"""
        mock_collection.count.return_value = 2
        mock_collection.query.return_value = {
            "documents": [["doc1", "doc2"]],
            "metadatas": [[{}, {}]],
            "distances": [[0.1, 0.2]],
        }
        await enabled_memory.search("查询", top_k=10)
        call_kwargs = mock_collection.query.call_args.kwargs
        assert call_kwargs["n_results"] == 2

    def test_count_returns_collection_count(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """count() 应返回 collection.count() 的值"""
        mock_collection.count.return_value = 42
        assert enabled_memory.count() == 42

    def test_count_failure_returns_zero(
        self, enabled_memory: LongTermMemory, mock_collection: MagicMock
    ) -> None:
        """count 抛异常时应返回 0"""
        mock_collection.count.side_effect = RuntimeError("计数失败")
        assert enabled_memory.count() == 0


class TestLongTermMemoryInitFailure:
    """初始化失败时的安全降级"""

    def test_init_failure_disables_memory(self) -> None:
        """ChromaDB 初始化抛异常时，应降级为 enabled=False，不传播异常"""
        with patch("harness.memory.long_term.settings") as mock_settings:
            mock_settings.long_term_enabled = True
            mock_settings.long_term_store_path = "./data/memory_store_test"
            with patch("harness.memory.long_term.chromadb.PersistentClient") as mock_client:
                mock_client.side_effect = RuntimeError("ChromaDB 不可用")
                mem = LongTermMemory()
                assert mem.enabled is False
                assert mem.collection is None


class TestReActLoopIntegration:
    """ReActLoop 与长期记忆的集成：注入与写入"""

    @pytest.mark.asyncio
    async def test_loop_injects_recall_into_prompt(self) -> None:
        """启用时，ReActLoop 应将检索结果注入 system prompt"""
        from harness.core.loop import ReActLoop
        from harness.guardrails.base import GuardrailPipeline
        from harness.memory.conversation_history import ConversationHistory
        from harness.observability.metrics import MetricsCollector
        from harness.observability.tracer import Tracer
        from tests.conftest import MockLLMClient

        llm = MockLLMClient(response="您好，我已经记得您的需求了。")
        long_term = MagicMock()
        long_term.enabled = True
        long_term.search = AsyncMock(return_value=[
            {
                "document": "用户: 之前买过手机\n助手: 推荐了小米14",
                "metadata": {"session_id": "old-session"},
                "distance": 0.1,
            }
        ])
        long_term.add = AsyncMock()

        loop = ReActLoop(
            llm=llm,
            registry=MagicMock(),
            guardrails=GuardrailPipeline(),
            tracer=Tracer(enabled=False),
            metrics=MetricsCollector(),
            conversation_history=MagicMock(spec=ConversationHistory),
            max_iterations=3,
            long_term_memory=long_term,
        )
        loop.conversation_history.aload_state.return_value = None  # type: ignore[attr-defined]

        result = await loop.execute("我想买手机", session_id="test-sid")

        # 应检索过长期记忆（带 user_id 隔离 + 排除当前会话）
        long_term.search.assert_called_once_with(
            "我想买手机", user_id="demo_user", exclude_session_id="test-sid"
        )
        # 注入到「状态尾注」消息（对话末尾）应包含「相关历史记忆」
        # 注意：轮末还有一次事实抽取调用，需取主调用（排除抽取器提示词）
        from harness.domain.models import ChatRole
        from harness.core.loop import ReActLoop

        main_calls = [c for c in llm.all_calls
                      if c and not c[0].content.startswith(ReActLoop.FACT_SYSTEM_PROMPT)]
        assert main_calls, "应存在主对话调用"
        note_msgs = [m for m in main_calls[-1]
                     if m.role == ChatRole.system and m.content.startswith("## 当前任务状态")]
        assert note_msgs, "应存在状态尾注消息"
        assert "相关历史记忆" in note_msgs[0].content
        assert "之前买过手机" in note_msgs[0].content
        # 完成后应触发写入长期记忆（async create_task）
        # 等待后台任务完成
        await asyncio.sleep(0.05)
        long_term.add.assert_called_once()
        call_args = long_term.add.call_args
        # create_task 调用的是 long_term.add(...), 参数在 call_args.args 或 kwargs
        assert call_args.kwargs.get("user_input") == "我想买手机" or \
               (call_args.args and "我想买手机" in call_args.args)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_loop_skips_recall_when_disabled(self) -> None:
        """禁用时，ReActLoop 不应调用长期记忆的 search/add"""
        from harness.core.loop import ReActLoop
        from harness.guardrails.base import GuardrailPipeline
        from harness.memory.conversation_history import ConversationHistory
        from harness.observability.metrics import MetricsCollector
        from harness.observability.tracer import Tracer
        from tests.conftest import MockLLMClient

        llm = MockLLMClient(response="直接回答")
        long_term = MagicMock()
        long_term.enabled = False
        long_term.search = AsyncMock(return_value=[])
        long_term.add = AsyncMock()

        loop = ReActLoop(
            llm=llm,
            registry=MagicMock(),
            guardrails=GuardrailPipeline(),
            tracer=Tracer(enabled=False),
            metrics=MetricsCollector(),
            conversation_history=MagicMock(spec=ConversationHistory),
            max_iterations=3,
            long_term_memory=long_term,
        )
        loop.conversation_history.aload_state.return_value = None  # type: ignore[attr-defined]

        await loop.execute("你好", session_id="test-sid")

        long_term.search.assert_not_called()
        long_term.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_skips_add_on_failure(self) -> None:
        """ReAct 循环失败（如 MaxIterations 超限）时不应写入长期记忆"""
        from harness.core.loop import ReActLoop
        from harness.guardrails.base import GuardrailPipeline
        from harness.memory.conversation_history import ConversationHistory
        from harness.observability.metrics import MetricsCollector
        from harness.observability.tracer import Tracer
        from tests.conftest import MockLLMClient

        llm = MockLLMClient(response='THOUGHT: 需要工具\nACTION: {"tool":"nonexistent","arguments":{}}')
        long_term = MagicMock()
        long_term.enabled = True
        long_term.search = AsyncMock(return_value=[])
        long_term.add = AsyncMock()

        registry = MagicMock()
        from harness.domain.exceptions import ToolNotFoundError
        registry.get_tool.side_effect = ToolNotFoundError("不存在")
        registry.get_tool_descriptions.return_value = ""
        registry.list_tools.return_value = []

        loop = ReActLoop(
            llm=llm,
            registry=registry,
            guardrails=GuardrailPipeline(),
            tracer=Tracer(enabled=False),
            metrics=MetricsCollector(),
            conversation_history=MagicMock(spec=ConversationHistory),
            max_iterations=2,
            long_term_memory=long_term,
        )
        loop.conversation_history.aload_state.return_value = None  # type: ignore[attr-defined]

        result = await loop.execute("复杂问题", session_id="fail-sid")

        await asyncio.sleep(0.05)
        long_term.add.assert_not_called()
        assert result.success is False
