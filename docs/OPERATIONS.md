# 运维手册（Operations）

> 版本：v0.7.0 · 面向部署/值班/SRE 视角的运行指南

## 1. 日志聚合

应用以 JSON 结构化日志输出到 **stdout/stderr**（`LOG_FORMAT=json`），容器化后由采集端接管：

```
容器 stdout → Docker logging driver / Filebeat sidecar → Loki | ELK
```

- 本地调试用 `LOG_FORMAT=console`
- 关键检索字段：`logger`（模块）、`session_id`、`level`；错误堆栈在 `exception` 字段
- 审计流水独立落盘 `data/audit_logs/*.jsonl`（含 PII 掩码），按 `AUDIT_ROTATE_MB` 轮转，
  建议 Filebeat 单独 tail 到保留策略更长的索引

## 2. 指标与告警

### 抓取端点

`GET /metrics/prometheus`（text/plain, Prometheus 0.0.4 格式）

| 指标 | 类型 | 说明 |
|---|---|---|
| harness_llm_calls_total | gauge | 累计 LLM 调用次数 |
| harness_tokens_total | gauge | 累计 token 消耗 |
| harness_duration_ms_total | gauge | 累计推理耗时 |
| harness_uptime_seconds | gauge | 进程存活时间 |
| harness_tool_calls_total{tool=...} | counter | 分工具调用次数 |

### Prometheus 抓取配置示例

```yaml
scrape_configs:
  - job_name: agent-harness
    metrics_path: /metrics/prometheus
    static_configs:
      - targets: ["harness:8000"]
```

### 告警规则建议（Alertmanager）

```yaml
- alert: HarnessDown
  expr: up{job="agent-harness"} == 0
  for: 2m
- alert: TokenBurnRateHigh
  expr: rate(harness_tokens_total[10m]) > 50000   # 按容量调整
  for: 5m
- alert: NoToolActivity
  expr: increase(harness_llm_calls_total[30m]) == 0 and harness_uptime_seconds > 1800
```

### 应用内预算告警

单会话 token 用量达 `TOKEN_BUDGET_PER_SESSION × TOKEN_BUDGET_ALERT_RATIO`（默认 80%）时
输出 `[ALERT][BUDGET]` WARNING 日志——告警管道对该关键字建立规则即可联动；
达到 100% 且 `TOKEN_BUDGET_HARD_STOP=true` 时会话被优雅终止。

## 3. 灰度发布

- 每个响应携带 `X-Release-Channel` 头（`RELEASE_CHANNEL` 配置，stable/canary）
- 推荐路径：
  1. 构建两个镜像 tag（stable / canary）
  2. 反向代理按权重分流（Traefik/Nginx 示例：canary weight=10%）
  3. 观察 canary 的 token 增速、工具失败率、badcase 导出结果后再放量
- 应用层无状态，扩容 = 多副本；注意限流/风控计数器为进程内实现，
  多副本部署时需迁移至 Redis（接口已预留，见 `guardrails/rate_limiter.py`）

## 4. 密钥管理

当前支持两级解析（优先级从高到低）：

1. 环境变量 `OPENAI_API_KEY`
2. 文件挂载 `OPENAI_API_KEY_FILE`（Docker Swarm/K8s Secret 挂载为文件的标准形态）

规范：

- `.env` 已列入 `.gitignore`，严禁提交真实密钥
- K8s 生产示例：
  ```yaml
  env:
    - name: OPENAI_API_KEY_FILE
      value: /etc/secrets/openai_key
  containers:
    - volumeMounts:
        - name: secrets
          mountPath: /etc/secrets
          readOnly: true
  ```
- 轮换：更新 Secret 后滚动重启进程即可（客户端启动时读取一次）

## 5. 数据生命周期

| 数据 | 位置 | 保留策略 |
|---|---|---|
| 会话状态 | SQLite sessions/messages 表 | `SESSION_CLEANUP_HOURS`（默认 24h）自动清理 |
| 商品/订单/物流 | SQLite | 种子或上游同步为准；`init_db --reset --reindex` 可全量重建 |
| 长期记忆 | ChromaDB memory_store | 持久累积（生产需增加 TTL 与用户删除权实现） |
| 审计流水 | audit_logs/*.jsonl | 按 AUDIT_ROTATE_MB 轮转；合规留存期由组织策略定义 |

## 6. 故障排查速查

| 症状 | 排查入口 |
|---|---|
| 回复中出现「系统异常」 | stderr 日志搜 `Agent 执行异常` |
| 大量 429 | 会话并发锁 / 管理接口限流命中，检查调用方重试逻辑 |
| 检索返回「已放宽价格条件」 | 正常自校正行为；频繁出现说明目录覆盖不足 |
| `[ALERT][BUDGET]` 密集 | 单用户滥用或长会话未压缩，评估调低阈值 |
| 向量库计数与 DB 不符 | `python scripts/init_db.py --reindex` 对账重建 |
