> 历史快照说明：本文档为评测规划的历史归档版本，内容为当时规划与实现对照。最新评测体系与结论见 `docs/EVALUATION.md`，实时报告见 `data/eval/report_*.json`。

# Agent 完整评测规划（v2）

> 目标：从「单层检索/路由评测」升级为**覆盖生成质量、护栏一致性、记忆、任务流程、鲁棒性与非功能的完整评测体系**，让每次改动都能被自动化守护。
>
> 状态：本文档为规划与实现对照；对应代码位于 `scripts/eval*.py`，用例位于 `data/eval/golden_set.jsonl`，结果报告位于 `data/eval/report_*.json`。

---

## 一、运行架构：三档模式

统一入口 `python scripts/eval.py`，按 `--mode` 选择运行档位：

| 档位 | 特征 | 真实 LLM | 门槛 | 场景 |
|---|---|---|---|---|
| **L0 离线确定性** | 纯规则 / 解析 / 确定性模块打分 | 无 | 硬性 100%（合规类） | CI / 每次改动 |
| **L1 在线确定性** | 真实 agent + 确定性打分 | 主模型+小模型 | 软性 ≥ 0.8 | 发版前 / 手动 |
| **L2 LLM-as-Judge** | 真实 agent + judge 打分 | 全部 | 参考（不 gate） | 定期评测 |

`--mode` 与层的映射：

| 层 | 模块 | 指标 | L0 | L1 | L2 |
|---|---|---|---|---|---|
| D1 检索质量 | eval.py::eval_retrieval | Recall@5 / MRR / 价格品类硬合规 | ✓ | ✓ | ✓ |
| D1 预算合规 | eval.py::eval_budget | 超预算数=0 / 接近度 | ✓ | ✓ | ✓ |
| D2 生成质量（规则） | eval_gen.py::eval_gen | Faithfulness / Hallucination / ContextUtil | – | ✓ | ✓ |
| D2 生成质量（Judge） | eval_gen.py::eval_gen(judge=True) | AnswerRelevance / Completeness | – | – | ✓ |
| D3 护栏一致性 | eval_guardrail.py | 强制检索触发 / 引用一致性 | – | ✓ | ✓ |
| D4 长期记忆 | eval_memory.py::eval_memory | 语义命中 / MRR（含负例） | ✓ | ✓ | ✓ |
| D4 工作记忆流 | eval_memory.py::eval_wm_flow | 槽位写入/更新/清除 | ✓ | ✓ | ✓ |
| D5 任务流程 | eval_workflow.py | 任务完成率 / 工具序列匹配 | – | ✓ | ✓ |
| D6 工具路由 | eval.py::eval_routing | 期望工具命中率 | – | ✓ | ✓ |
| D7 鲁棒性 | eval.py::eval_robustness | 攻击向量全拒绝 | ✓ | ✓ | ✓ |
| D8 非功能 | eval_perf.py | P50/P95 延迟 · 每轮 token · 调用次数 | – | ✓ | ✓ |

CLI：
```bash
python scripts/eval.py --mode L0 --strict      # CI：离线四层，全绿才退出 0
python scripts/eval.py --mode L1               # 发版前：完整离线+在线确定性
python scripts/eval.py --mode L2               # 定期：追加 LLM-as-Judge 层
python scripts/eval.py --layers gen,guardrail  # 精确挑选任意层
```

---

## 二、维度与用例设计

### D1 检索质量 + 预算合规（已有 → 扩充）

- 用例：retrieval 13 → **30+**；budget 4 → **8**
- 扩充方向：口语变体（`5k` / `万五` / `3千块`）、单位归一化、多条件组合、无结果提示、宽松重查触发、空知识库、预算口语（`预算1500` / `1500预算` / `1万以内`）
- 指标：Recall@5、MRR@5、价格/品类硬合规、超预算数=0、top1 接近度
- 门槛：Recall@5 ≥ 0.8；**价格/品类/预算硬合规 100%**（L0 硬性）

### D2 生成质量（新增，直击幻觉）

| 指标 | 定义 | 打分方式 | 档位 |
|---|---|---|---|
| Faithfulness 忠实度 | 回答中每个 `product_xxx` 引用均能在检索 `tool_result` 中追溯到 | 规则解析（输出文本反向匹配） | L1 |
| Hallucination Rate | 回答中出现但不在检索结果中的商品引用比例（id 级 + 名称级） | 交叉校验 | L1 |
| Context Utilization | 回答引用的唯一 id 数 ÷ 检索返回 top-K | 计数 | L1 |
| Answer Relevance | 是否切题 | 小模型 LLM-as-Judge 1-5 | L2 |
| Completeness | GT top-3 商品是否被提及 | 规则 + Judge | L1/L2 |

数据流：golden `layer=gen` 用例 → `agent.run()` → 从 `traces/steps` 提取 `knowledge_retrieval` 的 `tool_result` 与最终 `answer` → 解析比对。

### D3 护栏一致性（新增，回归守护）

| 用例 | 断言 |
|---|---|
| 商品信号（推荐/比价/参数/预算/品牌） | `knowledge_retrieval` 必须被调用，且为首个工具 |
| 闲聊 / 非商品咨询 | 零工具调用 |
| 引用一致性 | answer 引用的商品 id 必须 ∈ 检索返回集合 |
| 不重复检索 | 商品问题不会对同一输入反复触发检索死循环 |

### D4 记忆层（已有 → 扩充）

- **长期记忆**：种子 5 → 8（含 2 条负例：不相关查询不应命中 top3）+ user 隔离断言
- **工作记忆流**（L0 确定性，直接驱动 `WorkingMemory.update_from_input`）：
  - 预算写入：`预算3000` / `3999的手机` / `3k` → `budget_amount=3000/3999/3000`
  - 预算覆盖：后轮新预算替换旧预算
  - 临时上限不覆盖：已有长期预算时 `5000以下` 不覆盖
  - 订单号/物流号追加与去重
  - 槽位清空：`reset_for_new_cycle()` 后仅保留会话级计数器
- 短期指代消解（"那款/它"）依赖 LLM，标注为人工抽查（不在自动 gate 内）

### D5 任务流程（新增，端到端）

- 用例 10~12 条多轮任务流：查订单→售后申请→转人工；预算推荐→比价→选定；不记得订单号→列表查询
- 判定：Spy Registry 记录**调用序列**，与 `expect_tools`（子集）及 `expect_order`（若指定顺序）匹配
- 指标：任务完成率、期望工具命中率、终止正确率（无多余重复调用）

### D6 工具路由（已有 → 补齐 --live）

- 用例 10 → 15：子任务分发、参数化查询、多条件组合
- 指标：期望工具命中率（含 `optional_tools` 容忍）、闲聊零工具率
- 门槛 ≥ 0.85（容忍 LLM 随机性）

### D7 鲁棒性 / 安全（已有 → 扩充）

- 攻击集 8 → 16：prompt 注入、越权会话访问、库存超卖参数、超大输入、SSE 断连、订单号伪造
- 门槛 100%（L0 硬性）

### D8 非功能（新增）

- **延迟**：复用现有耗时埋点，报告端到端 P50/P95、每工具耗时
- **成本**：每轮 token、LLM 调用次数（`AgentResult.total_tokens` + `agent.metrics`）
- 报告写入 eval 报告，不参与 pass/fail gate

---

## 三、数据与基建

### 3.1 golden_set.jsonl 扩展 schema

```jsonl
{"id":"R14","layer":"retrieval","query":"...","price_max":3000,"category":"手机","keywords":["拍照"]}
{"id":"G01","layer":"gen","query":"预算3000以内推荐一款拍照手机","expect_faithful":true,"expect_ids":["product_1"]}
{"id":"GA01","layer":"guardrail","query":"小米17 Pro 多少钱","expect_tools":["knowledge_retrieval"]}
{"id":"GA10","layer":"guardrail","query":"你好","expect_none":true}
{"id":"W01","layer":"workflow","query":"帮我查一下订单20240601003，我要退货","expect_tools":["order_query","after_sale_apply"],"expect_order":true}
```

字段约定：
- `expect_tools`：期望必须被调用的工具集合
- `expect_order`：为 true 时要求调用顺序与 `expect_tools` 一致（前缀匹配）
- `expect_none`：要求零工具
- `expect_ids`：回答中必须引用的商品 id（可空）
- `layer=gen` 用例复用 `expect_ids` 作为忠实度锚点

### 3.2 报告与回流

- 报告：`data/eval/report_<ts>.json`（统一结构），并渲染为 Markdown 追加/比对
- badcase 回流：`scripts/export_badcase.py` 已有；gen/guardrail/workflow 失败用例自动追加候选，人工评审后并入 golden_set

### 3.3 CI 接入

- test job 追加 `python scripts/eval.py --mode L0 --strict`（离线四层，无 LLM/网络依赖，CI 可稳定复现）
- L1/L2 因消耗真实 tokens，仅手动/定时触发，不入 CI

---

## 四、落地分期与现状

| 阶段 | 内容 | 档位 | 状态 |
|---|---|---|---|
| P0 | D2 规则版 + D3 护栏一致性 + golden 扩充 | L0/L1 | 本文档落地时实现 |
| P1 | D4 记忆对话流 + D1 检索扩量 + D8 延迟/成本报告 | L0/L1 | 同上 |
| P2 | D5 任务流程 + D6 路由全量 | L1 | 同上 |
| P3 | D2 Judge 版 + D7 扩充 + CI 接入 | L2 | 同上 |

> 说明：本规划落地时一次性实现 P0-P3 全部代码与用例（D7 扩充中 LLM 无关项随 golden 扩充，D2 Judge 版与 D5 在配置有效 key 后跑 `--mode L1/L2` 生效）。

---

## 五、运行与复现

```bash
# L0 离线四层（CI 同款）
python scripts/eval.py --mode L0 --strict

# 完整 L1（真实 LLM，需 .env 有效 key）
python scripts/eval.py --mode L1

# L2 追加 Judge 层
python scripts/eval.py --mode L2

# 查看最新报告
cat data/eval/report_*.json | python -m json.tool
```
