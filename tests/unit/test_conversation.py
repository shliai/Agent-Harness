from __future__ import annotations

from harness.domain.models import AgentMessage, ChatRole
from harness.memory.short_term import ShortTermMemory


class TestShortTermMemory:
    def test_add_and_get_context(self) -> None:
        memory = ShortTermMemory(window_size=10)
        msg = AgentMessage(role=ChatRole.user, content="你好")
        memory.add(msg)

        context = memory.get_context()
        assert len(context) == 1
        assert context[0].content == "你好"

    def test_sliding_window(self) -> None:
        memory = ShortTermMemory(window_size=2)
        memory.add(AgentMessage(role=ChatRole.user, content="msg1"))
        memory.add(AgentMessage(role=ChatRole.user, content="msg2"))
        memory.add(AgentMessage(role=ChatRole.user, content="msg3"))

        context = memory.get_context()
        assert len(context) == 2
        assert context[0].content == "msg2"
        assert context[1].content == "msg3"

    def test_clear(self) -> None:
        memory = ShortTermMemory(window_size=10)
        memory.add(AgentMessage(role=ChatRole.user, content="你好"))
        memory.clear()
        assert len(memory.get_context()) == 0
