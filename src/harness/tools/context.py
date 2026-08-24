from __future__ import annotations

import logging
import time
from contextvars import ContextVar

logger = logging.getLogger("harness.tools.context")

# 请求级上下文：由 ReActLoop 每次执行前设置，工具内部读取。
# 让"订单归属校验 / 我的订单 / 工单归属"等业务逻辑无需改工具签名即可拿到身份。
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="demo_user")
current_session_id: ContextVar[str] = ContextVar("current_session_id", default="")
current_budget: ContextVar[float | None] = ContextVar("current_budget", default=None)
# 流式调用时 LLM 客户端把真实 usage 写进该 dict；ReAct 循环每轮读取后清零
llm_usage_sink: ContextVar[dict | None] = ContextVar("llm_usage_sink", default=None)

DEFAULT_USER = "demo_user"


# ── 枚举风控：同会话对同一查询型工具连续未命中即熔断 ──────

_miss_state: dict[str, dict] = {}


class EnumerationGuard:
    """防止遍历式探测（拖库）：同 key 连续 miss 达到阈值后熔断一段时间

    - 命中即清零（正常使用不受影响）
    - 熔断期内直接拒绝，返回引导转人工
    - 进程内实现；多实例部署时换 Redis 计数器即可
    """

    def __init__(self, max_misses: int = 8, window_seconds: int = 900, cooldown_seconds: int = 1800) -> None:
        self.max_misses = max_misses
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds

    def _bucket(self, key: str) -> dict:
        now = time.time()
        b = _miss_state.get(key)
        if b is None or now - b["start"] > self.window_seconds:
            b = {"misses": 0, "blocked_until": 0.0, "start": now}
            _miss_state[key] = b
        return b

    def check(self, key: str) -> str | None:
        """调用前检查：被熔断返回提示文案，否则 None"""
        b = self._bucket(key)
        if time.time() < b["blocked_until"]:
            remain = int(b["blocked_until"] - time.time())
            return (
                f"检测到短时间内大量无效查询，为保障数据安全已暂停该查询服务（约 {remain} 秒后恢复）。"
                "如有紧急需要请联系人工客服。"
            )
        return None

    def record_miss(self, key: str) -> None:
        b = self._bucket(key)
        b["misses"] += 1
        if b["misses"] >= self.max_misses:
            b["blocked_until"] = time.time() + self.cooldown_seconds
            logger.warning("枚举风控触发: %s (连续 %d 次未命中)", key, b["misses"])

    def record_hit(self, key: str) -> None:
        b = _miss_state.get(key)
        if b:
            b["misses"] = 0


# 查询类工具共享的风控实例
query_guard = EnumerationGuard()


def guard_key(tool_name: str) -> str:
    return f"{tool_name}:{current_session_id.get() or 'anonymous'}"
