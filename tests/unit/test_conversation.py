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

    def test_append_only_no_eviction(self) -> None:
        """只追加语义：超过 window_size 不自动淘汰（淘汰由显式压缩负责）"""
        memory = ShortTermMemory(window_size=2)
        memory.add(AgentMessage(role=ChatRole.user, content="msg1"))
        memory.add(AgentMessage(role=ChatRole.user, content="msg2"))
        memory.add(AgentMessage(role=ChatRole.user, content="msg3"))

        context = memory.get_context()
        assert len(context) == 3
        assert [m.content for m in context] == ["msg1", "msg2", "msg3"]

    def test_split_and_trim(self) -> None:
        """压缩接口：split_for_compression 切分 + trim_to 裁剪"""
        memory = ShortTermMemory(window_size=10)
        for i in range(6):
            memory.add(AgentMessage(role=ChatRole.user, content=f"m{i}"))

        old, recent = memory.split_for_compression(keep_recent=4)
        assert [m.content for m in old] == ["m0", "m1"]
        assert [m.content for m in recent] == ["m2", "m3", "m4", "m5"]

        memory.trim_to(recent)
        assert len(memory.get_context()) == 4
        assert memory.get_context()[0].content == "m2"

    def test_clear(self) -> None:
        memory = ShortTermMemory(window_size=10)
        memory.add(AgentMessage(role=ChatRole.user, content="你好"))
        memory.clear()
        assert len(memory.get_context()) == 0
