from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from harness.config import settings

logger = logging.getLogger("harness.memory.learning")

# 类型优先级：约束 > 纠正 > 偏好（注入与淘汰时据此排序）
_PRIORITY = {"constraint": 0, "correction": 1, "preference": 2}

# 用户信号抽取（偏好/约束/纠正）统一在 WorkingMemory 中确定性完成；
# 长期记忆轮末直接读取工作记忆，故此处仅再导出，避免二次抽取。


class LearningRecord(BaseModel):
    """单条学习记录（结构化、确定性捕获，非自由文本）"""

    type: str  # preference | correction | constraint
    key: str  # 合并键（如 budget / brand / category / allergy）
    value: str  # 人类可读的已学习内容
    confidence: float = 1.0
    evidence: str = ""
    ts: str = ""


class LearningStore:
    """单用户学习记忆：JSON 文件存储，结构化键控，无向量检索。

    - 捕获：仅来自确定性信号（偏好/约束/纠正），不调 LLM 自由文本抽取
    - 来源：轮末直接读取 WorkingMemory.learning_signals()（单点抽取）
    - 合并：按 (type, key)；纠正写入时覆盖同 key 偏好（纠正权威 > 初始偏好）
    - 召回：load() 全量读入 → render_for_prompt() 注入系统提示词（单用户数据量小）
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self.enabled = settings.learning_enabled
        self.dir = Path(store_path or settings.learning_store_path)
        self.file_path = self.dir / "learning.json"
        self.ttl_days = settings.learning_ttl_days
        self.max_items = settings.learning_max_items
        self.confidence_threshold = settings.learning_confidence_threshold
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("学习记忆目录创建失败: %s", e)

    # ── 持久化 ─────────────────────────────────────────
    def _read(self) -> list[dict[str, Any]]:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("学习记忆读取失败，按空处理: %s", e)
            return []

    def _write(self, records: list[LearningRecord]) -> None:
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(
                    [r.model_dump() for r in records],
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            logger.warning("学习记忆写入失败: %s", e)

    # ── 读取 / 筛选 / 渲染 ──────────────────────────────
    def load(self) -> list[LearningRecord]:
        if not self.enabled:
            return []
        now = datetime.now()
        kept: list[LearningRecord] = []
        for d in self._read():
            try:
                rec = LearningRecord(**d)
            except Exception:
                continue
            if rec.confidence < self.confidence_threshold:
                continue
            if rec.ts:
                try:
                    age = (now - datetime.fromisoformat(rec.ts)).days
                except Exception:
                    age = 0
                if age > self.ttl_days:
                    continue
            kept.append(rec)
        kept.sort(key=lambda r: (_PRIORITY.get(r.type, 9), r.ts))
        return kept

    def render_for_prompt(self, records: list[LearningRecord] | None = None) -> str:
        recs = records if records is not None else self.load()
        if not recs:
            return ""
        labels = {"constraint": "约束", "correction": "纠正", "preference": "偏好"}
        lines = ["## 用户长期画像（已学习，请遵循）"]
        for r in recs:
            lines.append(f"· {labels.get(r.type, r.type)}：{r.value}")
        return "\n".join(lines)

    def count(self) -> int:
        return len(self.load())

    # ── 写入 / 合并 ────────────────────────────────────
    def add(self, record: LearningRecord) -> None:
        if not self.enabled:
            return
        with self._lock:
            records: list[LearningRecord] = []
            for d in self._read():
                try:
                    records.append(LearningRecord(**d))
                except Exception:
                    continue
            # 纠正权威 > 偏好：写入纠正时删除同 key 的偏好
            if record.type == "correction":
                records = [
                    r for r in records
                    if not (r.type == "preference" and r.key == record.key)
                ]
            merged = False
            for i, r in enumerate(records):
                if r.type == record.type and r.key == record.key:
                    records[i] = record
                    merged = True
                    break
            if not merged:
                records.append(record)
            if self.max_items and len(records) > self.max_items:
                records.sort(key=lambda r: (_PRIORITY.get(r.type, 9), r.ts))
                records = records[-self.max_items:]
            self._write(records)
