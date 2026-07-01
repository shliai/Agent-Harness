class HarnessError(Exception):
    """Agent Harness 基础异常"""


class ConfigError(HarnessError):
    """配置错误"""


class LLMError(HarnessError):
    """LLM 调用异常"""


class ToolError(HarnessError):
    """工具调用基础异常"""


class ToolNotFoundError(ToolError):
    """工具未注册"""


class ToolExecutionError(ToolError):
    """工具执行失败"""


class GuardrailError(HarnessError):
    """安全护栏拦截"""


class InputValidationError(GuardrailError):
    """输入校验未通过"""


class RateLimitError(GuardrailError):
    """速率限制"""


class AgentLoopError(HarnessError):
    """Agent 循环异常"""


class MaxIterationsExceeded(AgentLoopError):
    """超过最大迭代次数"""
