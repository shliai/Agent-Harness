# Agent Harness

基于 ReAct 模式的电商智能客服 Agent 运行时——自研循环引擎、四层记忆体系、七道安全护栏与五层评测框架，不依赖 LangChain 等框架。

## 快速开始

### 前置要求

- Python 3.11+
- 任一 OpenAI v1 兼容 API Key（OpenAI / 智谱 / DeepSeek / 通义 / 本地 vLLM 等）
- Node.js（仅前端语法检查，非必需）

### 三步启动

```bash
# ① 克隆并安装
git clone https://github.com/shliai/Agent-Harness.git
cd Agent-Harness
pip install -e .

# ② 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 API Key：
#   OPENAI_API_KEY=sk-xxx
#   OPENAI_API_URL=https://api.moonshot.cn/v1   # 或 OpenAI/智谱/DeepSeek 等 v1 兼容网关
#   OPENAI_MODEL=kimi-k2-0905-preview           # 默认 256k 窗口模型（与压缩阈值对齐）
#   换模型时同步修改 CONTEXT_WINDOW_TOKENS = 该模型真实上下文窗口
#   （可选）OPENAI_SMALL_MODEL=deepseek-chat    # 事实抽取/重排走小模型，省成本

# ③ 初始化数据 + 启动（BGE 嵌入模型已内置本地，离线加载）
python scripts/init_db.py --reindex
python -m harness.main --port 8000
```

打开 `http://localhost:8000` 即可使用。

> **BGE 模型**：内置在 `models/bge-small-zh-v1.5/`，默认离线加载（`HF_HUB_OFFLINE=true`），无需联网。
> 如需在线下载/更新模型，在 `.env` 中设 `HF_HUB_OFFLINE=false`；网络受限时可用 `HF_ENDPOINT=https://hf-mirror.com` 国内镜像。

### Docker 部署

```bash
cp .env.example .env    # 编辑填入 API Key
docker compose up -d
```

---

## 你能得到什么

发送一条消息，Agent 会：

1. **理解意图**：判断需要调用哪些工具（检索商品 / 查订单 / 查物流 / 查政策 / 申请售后…）
2. **执行工具**：带归属校验、枚举风控、参数白名单的确定性代码
3. **流式回答**：token 级增量推送，思考过程实时可见
4. **记住上下文**：预算约束、提到的订单号跨轮持续生效
5. **商品防幻觉**：涉及商品的提问强制走知识库检索（`knowledge_retrieval`），禁止凭记忆编造型号/价格/参数

### 示例对话

```
你：预算3000以内，帮我推荐一款拍照好的手机
慧：（检索商品库 → 过滤价格品类 → 按预算接近度排序）
    为您推荐以下几款…… [product_009]

你：查一下订单20240601001的状态
慧：（SQLite 归属校验 → 查询本人订单）
    您的订单信息如下……

你：激活过的耳机还能退吗
慧：（强制查政策库 → 引用条款编号）
    根据平台七天无理由退货政策 [POL-REFUND-01] ……
```

---

## 功能清单

| 模块 | 能力 |
|---|---|
| 循环引擎 | 自研 ReAct · **原生 function calling** · token 流式 · 失败修正重试 · 中断落盘 |
| 商品检索 | 向量+BM25 RRF 融合 · 中文分词 · 价格/品类/库存过滤 · 预算接近度加权 |
| 订单管理 | 归属校验 · 列表查询 · 枚举风控熔断 |
| 售后服务 | 退货换货申请（状态机）· 退款测算（券不退+满减扣回）· 进度查询 |
| 政策问答 | 结构化条款库 · 强制引用编号 · 未命中引导转人工 |
| 转人工 | 工单创建 · 排队确认话术 |
| 安全护栏 | 输入校验 · 注入拦截 · 系统提示词防泄露 · PII 掩码 · 承诺合规 · 会话限频 · 审计轮转 |
| 记忆体系 | 短期窗口(只追加+token触发压缩) · 冻结章节(LSM式压缩归档,只存硬实体+小模型摘要) · 工作记忆(规则槽位) · 轮末折叠式滚动摘要(cheap_llm,≤300字) · 学习机制(轮末读工作记忆确定性信号→JSON→注入系统提示词，默认关闭) |

完整功能矩阵见 [docs/FEATURES.md](docs/FEATURES.md)。

## 项目结构

```
src/harness/
├── core/           ReAct 循环引擎
├── llm/            OpenAI v1 兼容客户端
├── tools/          10 个工具
├── memory/         四层记忆
├── guardrails/     七道安全护栏
├── storage/        SQLite + 向量同步
├── observability/  日志/指标/追踪
├── domain/         Pydantic 数据模型
├── web/            FastAPI + 前端
└── config.py       配置中心

scripts/            init_db / eval / export_badcase
data/seed/          400 商品 + 260 订单 + 200 物流种子数据
data/policies.json  10 条官方政策条款
data/eval/          108 条评测用例（14 层）
tests/              173 个测试用例
docs/               10 份文档
```

## 测试与评测

```bash
pytest tests/ -v                    # 173 个测试
python scripts/eval.py              # L0 离线确定性（55/55 PASS，CI 闸门）
python scripts/eval.py --mode L1    # 完整评测：L0 + 在线十层（含参数正确性/容错行为/安全对齐/跨会话隔离）
python scripts/eval.py --mode L1 --runs 3   # 在线层跑 3 次：输出复现率与 flaky 用例
```

详细结果见 [docs/EVALUATION.md](docs/EVALUATION.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [用户指南](docs/USER_GUIDE.md) | 操作手册、示例提问、常见问题 |
| [功能清单](docs/FEATURES.md) | 全部能力矩阵与实现要点 |
| [架构设计](docs/ARCHITECTURE.md) | 分层架构、模块职责、数据流 |
| [实现细节](docs/IMPLEMENTATION.md) | 前后端全部功能的算法与设计决策 |
| [设计决策](docs/DESIGN_DECISIONS.md) | 为什么这样做、放弃了什么替代方案 |
| [接口文档](docs/API.md) | REST/SSE 端点规范 |
| [运维手册](docs/OPERATIONS.md) | 日志聚合、指标告警、灰度发布、密钥管理 |
| [评测报告](docs/EVALUATION.md) | 五层评测方法论与详细结果 |
| [更新日志](docs/CHANGELOG.md) | v0.1.0 → v0.8.0 演进史 |

## 技术栈

Python 3.11+ · FastAPI · httpx(async) · SQLite(WAL) · ChromaDB · Sentence-Transformers(BGE) · Pydantic v2 · Docker · GitHub Actions

## 许可证

MIT License
