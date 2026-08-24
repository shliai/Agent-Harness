"""针对 v0.3.0 修复与优化的回归测试"""

from __future__ import annotations



import asyncio

import time

from unittest.mock import MagicMock, patch



import pytest



from harness.domain.exceptions import InputValidationError, RateLimitError

from harness.guardrails.audit_logger import AuditLogger

from harness.guardrails.base import GuardrailPipeline

from harness.guardrails.input_validator import InputValidator

from harness.guardrails.output_filter import OutputFilter

from harness.guardrails.rate_limiter import RateLimiter

from harness.llm.base import LLMReply


class StreamFromChat:
    """为只实现了 chat_async 的脚本 LLM 补齐流式接口（整段作为单个 delta）"""

    async def stream_chat_async(self, messages, temperature=None):
        reply = await self.chat_async(messages, temperature=temperature)
        yield reply.content







# ── Calculator：AST 安全求值 ──────────────────────────────



class TestCalculatorHardening:

    @pytest.mark.asyncio

    async def test_pow_bomb_rejected_fast(self) -> None:

        """9**99999999 这类幂炸弹必须被秒拒，不能卡死事件循环"""

        from harness.tools.calculator import CalculatorTool



        tool = CalculatorTool()

        start = time.perf_counter()

        result = await tool.run(expression="9**99999999")

        elapsed = time.perf_counter() - start

        assert elapsed < 2.0

        assert "拒绝" in result or "溢出" in result or "范围" in result



    @pytest.mark.asyncio

    async def test_normal_math_still_works(self) -> None:

        from harness.tools.calculator import CalculatorTool



        tool = CalculatorTool()

        assert await tool.run(expression="2 + 3 * 4") == "14"

        assert await tool.run(expression="(1500 + 2500) * 0.85") == "3400"

        assert await tool.run(expression="2**10") == "1024"

        assert await tool.run(expression="7 % 3") == "1"

        assert await tool.run(expression="-5 + 10") == "5"



    @pytest.mark.asyncio

    async def test_no_names_or_calls(self) -> None:

        from harness.tools.calculator import CalculatorTool



        tool = CalculatorTool()

        for bad in ["__import__('os')", "abs(-1)", "'a' + 'b'"]:

            result = await tool.run(expression=bad)

            assert "拒绝" in result or "不支持" in result or "错误" in result





# ── RateLimiter：按 key 隔离 ──────────────────────────────



class TestRateLimiterPerKey:

    def test_keys_isolated(self) -> None:

        limiter = RateLimiter(max_requests=2, window_seconds=60)

        ctx_a = {"type": "input", "content": "hi", "session_id": "session-a"}

        ctx_b = {"type": "input", "content": "hi", "session_id": "session-b"}



        limiter.check(ctx_a)

        limiter.check(ctx_a)

        with pytest.raises(RateLimitError):

            limiter.check(ctx_a)  # a 已满

        limiter.check(ctx_b)  # b 不受影响



    def test_anonymous_fallback(self) -> None:

        limiter = RateLimiter(max_requests=1, window_seconds=60)

        ctx = {"type": "input", "content": "hi"}

        limiter.check(ctx)

        with pytest.raises(RateLimitError):

            limiter.check(ctx)





# ── OutputFilter：身份证 X 结尾 ───────────────────────────



class TestOutputFilterIdCardX:

    def test_id_card_with_x_suffix_masked(self) -> None:

        f = OutputFilter()

        text = "身份证是11010119900307447X请查收"

        masked = f.check({"type": "output", "content": text})

        assert "11010119900307447X" not in masked

        assert "***" in masked



    def test_plain_id_card_masked(self) -> None:

        f = OutputFilter()

        masked = f.check({"type": "output", "content": "号码110101199003074470结束"})

        assert "110101199003074470" not in masked



    def test_order_id_not_masked(self) -> None:

        f = OutputFilter()

        masked = f.check({"type": "output", "content": "订单20240601001已发货"})

        assert "20240601001" in masked  # 11 位订单号不应误伤





# ── AuditLogger：blocked 事件留痕 ─────────────────────────



class TestAuditBlockedEvents:

    def test_blocked_input_recorded(self, tmp_path) -> None:

        pipeline = GuardrailPipeline()

        pipeline.add(InputValidator(max_length=10))

        audit = AuditLogger(log_path=tmp_path)

        pipeline.add(audit)



        with pytest.raises(InputValidationError):

            pipeline.check_input("x" * 100)



        files = list(tmp_path.glob("audit_*.jsonl"))

        assert len(files) == 1

        content = files[0].read_text(encoding="utf-8")

        assert '"result": "blocked"' in content



    def test_passed_input_recorded(self, tmp_path) -> None:

        pipeline = GuardrailPipeline()

        audit = AuditLogger(log_path=tmp_path)

        pipeline.add(audit)



        pipeline.check_input("正常输入")



        files = list(tmp_path.glob("audit_*.jsonl"))

        assert '"result": "passed"' in files[0].read_text(encoding="utf-8")





# ── LLM 客户端参数处理 ────────────────────────────────────



class TestLLMParamHandling:

    def test_temperature_zero_preserved(self) -> None:

        from harness.llm.openai_compatible import _resolve_temperature



        assert _resolve_temperature(0) == 0          # 显式 0 不被吞

        assert _resolve_temperature(0.3) == 0.3

        assert _resolve_temperature(None) is not None  # 回退 settings 默认值





# ── 知识检索：分词 / 预算单位 ──────────────────────────────



class TestTokenizerAndBM25:

    def test_chinese_tokenization_produces_terms(self) -> None:

        from harness.tools.knowledge_retrieval import BM25, tokenize



        docs = [

            tokenize("小米14 拍照手机 徕卡光学镜头 摄影强大"),

            tokenize("iPhone 15 Pro Max 旗舰拍照 A17 Pro 芯片"),

            tokenize("Redmi Note 13 Pro 性能足够 日常使用"),

        ]

        bm25 = BM25(docs)

        scores = [bm25.score(tokenize("拍照手机"), i) for i in range(len(docs))]

        # 至少一个文档命中关键字通道（修复前恒为 0）

        assert any(s > 0 for s in scores)



    def test_extract_filters_k_unit(self) -> None:

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool as K



        assert K._extract_filters("推荐个3k以内的手机")["price_max"] == 3000.0

        assert K._extract_filters("3千左右的耳机")["price_max"] == 3000.0



    def test_extract_filters_kuai_unit(self) -> None:

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool as K



        assert K._extract_filters("3000块的手机有什么推荐")["price_max"] == 3000.0



    def test_extract_filters_reversed_budget_word(self) -> None:

        """「2000预算」倒序表达也要能识别（评测 B02 回归）"""

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool as K



        assert K._extract_filters("我只有2000预算，买个耳机")["price_max"] == 2000.0

        assert K._extract_filters("预算只有2500的手机")["price_max"] == 2500.0



    def test_extract_filters_wan_units(self) -> None:
        """中文数量词：万 / 万元以上 / 1万5（评测 R13 驱动）"""
        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool as K

        assert K._extract_filters("万元以上的专业创作笔记本")["price_min"] == 10000.0
        assert K._extract_filters("1万5以内的游戏本")["price_max"] == 15000.0
        assert K._extract_filters("2万8以上的笔记本")["price_min"] == 28000.0

    def test_extract_filters_year_not_price(self) -> None:

        from harness.tools.knowledge_retrieval import KnowledgeRetrievalTool as K



        filters = K._extract_filters("2024年的新款手机有哪些")

        assert "price_max" not in filters and "price_min" not in filters





# ── 会话 ID 校验（防路径穿越） ────────────────────────────



class TestSessionIdValidation:

    def test_valid_ids_accepted(self) -> None:

        from harness.web.api import validate_session_id



        for sid in ["abc123", "test_sess", "a" * 64]:

            validate_session_id(sid)



    @pytest.mark.parametrize(

        "evil",

        [

            "../../etc/passwd",

            "..\\..\\windows\\win.json",

            "a/b",

            ".",

            "..",

            "$(rm -rf)",

            "a" * 65,

            "",

        ],

    )

    def test_traversal_ids_rejected(self, evil: str) -> None:

        from fastapi import HTTPException



        from harness.web.api import validate_session_id



        with pytest.raises(HTTPException):

            validate_session_id(evil)





# ── 子任务分发：多步执行控制流 ────────────────────────────



class _ScriptedSubtaskLLM:

    """脚本化 LLM：第 1 轮调工具 → 第 2 轮看到观察后直接回答"""



    def __init__(self) -> None:

        self.calls = 0

        self.second_round_messages: list | None = None



    async def chat_async(self, messages, temperature=None):

        from harness.domain.models import ChatRole



        self.calls += 1

        if self.calls == 1:

            return LLMReply(

                content='THOUGHT: 先查订单\nACTION: {"tool": "order_query", '

                        '"arguments": {"order_id": "20240601001"}}'

            )

        self.second_round_messages = messages

        roles = [m.role for m in messages]

        assert ChatRole.tool in roles, "第二轮前应能看到工具观察结果"

        return LLMReply(content="THOUGHT: 已查明\n您的订单已发货，顺丰承运。")





class TestSubtaskControlFlow:

    @pytest.mark.asyncio

    async def test_multi_step_subtask_completes(self) -> None:

        """回归测试：子任务执行工具后必须继续第二轮总结，结果不能为空"""

        from harness.core.registry import Registry

        from harness.tools.order_query import OrderQueryTool

        from harness.tools.subtask_dispatch import SubTaskDispatchTool



        reg = Registry()

        reg.register_tool(OrderQueryTool())

        llm = _ScriptedSubtaskLLM()

        tool = SubTaskDispatchTool(llm=llm, registry=reg)



        result = await tool.run(

            tasks=[{"id": "t1", "description": "查订单20240601001并总结", "tools": ["order_query"]}]

        )

        parsed = __import__("json").loads(result)

        assert llm.calls >= 2, "工具执行后 LLM 应获得观察机会"

        assert "已发货" in parsed["t1"]



    @pytest.mark.asyncio

    async def test_recursive_dispatch_blocked(self) -> None:

        """子任务中调用 subtask_dispatch 必须被拦截且不死循环"""

        from harness.core.registry import Registry

        from harness.tools.subtask_dispatch import SubTaskDispatchTool



        class RecursiveLLM:

            def __init__(self) -> None:

                self.calls = 0



            async def chat_async(self, messages, temperature=None):

                self.calls += 1

                return LLMReply(

                    content='THOUGHT: 分发\nACTION: {"tool": "subtask_dispatch", '

                            '"arguments": {"tasks": []}}'

                )



        reg = Registry()

        reg.register_tool(SubTaskDispatchTool(llm=RecursiveLLM(), registry=reg))

        llm = RecursiveLLM()

        tool = SubTaskDispatchTool(llm=llm, registry=reg)



        start = time.perf_counter()

        result = await tool.run(

            tasks=[{"id": "t1", "description": "试试递归", "tools": ["subtask_dispatch"]}]

        )

        assert time.perf_counter() - start < 5

        assert "未能在有限步骤内完成" in result  # 迭代耗尽兜底





# ── ReActLoop：坏 ACTION 重试 & 脱敏前移 ──────────────────



class TestLoopRobustness:

    def _build_loop(self, llm, registry):

        from harness.core.loop import ReActLoop

        from harness.memory.conversation_history import ConversationHistory

        from harness.observability.metrics import MetricsCollector

        from harness.observability.tracer import Tracer

        from harness.guardrails.base import GuardrailPipeline

        from harness.guardrails.output_filter import OutputFilter



        guardrails = GuardrailPipeline()

        guardrails.add(OutputFilter())

        return ReActLoop(

            llm=llm,

            registry=registry,

            guardrails=guardrails,

            tracer=Tracer(enabled=False),

            metrics=MetricsCollector(),

            conversation_history=MagicMock(spec=ConversationHistory),

            max_iterations=5,

        )



    @pytest.mark.asyncio

    async def test_malformed_action_gets_retry_not_raw_answer(self) -> None:

        """ACTION JSON 写错时，应要求重试而不是把原文当答案给用户"""

        from harness.core.registry import Registry

        from tests.conftest import MockTool



        class ScriptedLLM(StreamFromChat):

            def __init__(self) -> None:

                self.calls = 0



            async def chat_async(self, messages, temperature=None):

                self.calls += 1

                if self.calls == 1:

                    return LLMReply(content='THOUGHT: 查询\nACTION: {tool: mock_tool}')  # 坏 JSON

                if self.calls == 2:

                    return LLMReply(

                        content='THOUGHT: 修正\nACTION: {"tool": "mock_tool", "arguments": {"input": "ok"}}'

                    )

                return LLMReply(content="这是最终回答")



        registry = Registry()

        registry.register_tool(MockTool(response="工具结果"))

        llm = ScriptedLLM()

        loop = self._build_loop(llm, registry)

        loop.conversation_history.aload_state.return_value = None



        result = await loop.execute("测试")

        assert result.success

        assert result.answer == "这是最终回答"

        assert llm.calls >= 3  # 坏 ACTION → 纠正重试 → 正确调用 → 最终回答



    @pytest.mark.asyncio

    async def test_final_answer_masked_before_memory(self) -> None:

        """最终回答先脱敏再写入记忆/历史，敏感信息不得回流上下文"""

        from harness.core.registry import Registry



        captured = {}



        class PIILLM(StreamFromChat):

            async def chat_async(self, messages, temperature=None):

                captured["messages"] = messages

                return LLMReply(content="您的手机号13800138000已登记")



        loop = self._build_loop(PIILLM(), Registry())

        loop.conversation_history.aload_state.return_value = None



        saved = {}

        async def fake_save_state(sid, msgs, summary=None, working_memory=None, traces=None, user_id=None):

            saved["msgs"] = msgs



        loop.conversation_history.asave_state = fake_save_state



        result = await loop.execute("登记手机号")

        assert result.success, f"应成功执行: {result.error}"

        assert "13800138000" not in result.answer

        # 存入历史的 assistant 消息也应是脱敏后的

        assistant_msgs = [

            m.content for m in saved["msgs"]

            if getattr(m, "role", None) is not None and m.role.value == "assistant"

        ]

        assert all("13800138000" not in c for c in assistant_msgs)





# ── 会话历史（SQLite 版）：事务与兼容行为 ──────────────────



class TestConversationStoreDB:

    def test_unknown_session_returns_none(self, tmp_path, monkeypatch) -> None:

        from harness.config import settings

        from harness.memory.conversation_history import ConversationHistory



        monkeypatch.setattr(settings, "db_path", tmp_path / "s.db")

        hist = ConversationHistory()

        assert hist.load_state("ghost") is None

        assert hist.load("ghost") is None



    def test_state_roundtrip_and_title_preserved(self, tmp_path, monkeypatch) -> None:

        from harness.config import settings

        from harness.domain.models import AgentMessage, ChatRole

        from harness.memory.conversation_history import ConversationHistory



        monkeypatch.setattr(settings, "db_path", tmp_path / "s.db")

        hist = ConversationHistory()

        hist.save_state("s1", [AgentMessage(role=ChatRole.user, content="hi")])

        raw = hist.load_state("s1")

        raw["title"] = "我的会话"

        hist.awrite_raw.__self__._write_raw_sync("s1", raw)



        # 再次保存不得丢失 title

        hist.save_state("s1", [AgentMessage(role=ChatRole.user, content="again")])

        state = hist.load_state("s1")

        assert state["title"] == "我的会话"

        assert len(state["messages"]) == 1 and state["messages"][0]["content"] == "again"



    def test_legacy_json_auto_migrated(self, tmp_path, monkeypatch) -> None:

        """旧版 JSON 会话文件应自动迁入 SQLite"""

        import json as _json



        from harness.config import settings

        from harness.memory import conversation_history as ch_mod

        from harness.memory.conversation_history import ConversationHistory



        monkeypatch.setattr(settings, "db_path", tmp_path / "s.db")

        legacy_dir = tmp_path / "conversations"

        legacy_dir.mkdir()

        legacy = {

            "session_id": "old1",

            "title": "",

            "summary": "",

            "working_memory": {},

            "messages": [{"role": "user", "content": "旧数据", "timestamp": "2026-01-01T00:00:00"}],

        }

        (legacy_dir / "old1.json").write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")



        monkeypatch.setattr(ch_mod, "LEGACY_DIR", legacy_dir)

        hist = ConversationHistory()

        loaded = hist.load("old1")

        assert loaded is not None and loaded[0].content == "旧数据"



# ── Tracer：容量上限 ─────────────────────────────────────



class TestTracerBounded:

    def test_log_capped(self) -> None:

        from harness.observability.tracer import Tracer



        tracer = Tracer(enabled=True, max_records=5)

        for i in range(20):

            tracer.record_step(i, f"thought-{i}", None, None, session_id=f"s{i % 2}")



        log = tracer.get_log()

        assert len(log) == 5  # 只保留最新 5 条

        assert log[-1]["step"] == 19



    def test_filter_by_session(self) -> None:

        from harness.observability.tracer import Tracer



        tracer = Tracer(enabled=True, max_records=100)

        tracer.record_step(0, "a", None, None, session_id="s1")

        tracer.record_step(1, "b", None, None, session_id="s2")

        assert all(r["session_id"] == "s1" for r in tracer.get_log(session_id="s1"))

