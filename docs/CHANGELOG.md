# Agent Harness 更新日志

## [v0.1.0] - 2026-07-01

### 初始版本发布

- 基础 ReAct 循环引擎实现（`src/harness/core/loop.py`）
- 工具系统完整实现（5 个工具：knowledge_retrieval, order_query, logistics_query, calculator, subtask_dispatch）
- 记忆系统实现（ShortTermMemory + ConversationHistory + SessionManager）
- 安全护栏实现（InputValidator, OutputFilter, RateLimiter, AuditLogger）
- Web API 完整实现（FastAPI, 9 个端点, SSE 流式响应）
- 前端聊天界面（暗色主题, 3 栏布局, ReAct 步骤可视化）
- 测试覆盖：37 个（29 个单元测试 + 8 个集成测试）
- 构建系统：hatchling, Python >= 3.11