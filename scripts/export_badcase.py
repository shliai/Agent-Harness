"""badcase 回流：从审计日志与调用链中挖掘可疑对话，生成待评审用例草稿

来源信号：
    - 审计日志 blocked 事件（输入被拦截 / 限流触发）
    - 追踪记录里工具失败（tool_result.success=False）的会话
输出：data/eval/badcase_<date>.jsonl，每行一个候选 case，
字段含 layer 建议（人工评审后并入 golden_set.jsonl）。

用法：python scripts/export_badcase.py [--limit 50]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

AUDIT_DIR = REPO / "data" / "audit_logs"
OUT_DIR = REPO / "data" / "eval"


def collect_blocked(limit: int) -> list[dict]:
    out = []
    for f in sorted(AUDIT_DIR.glob("audit_*.jsonl"), reverse=True):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("result") == "blocked":
                out.append({
                    "source": "audit",
                    "reason": rec.get("reason", rec.get("type", "")),
                    "query": rec.get("content_preview", ""),
                    "session_id": rec.get("session_id"),
                    "ts": rec.get("timestamp"),
                    "suggest_layer": "robustness",
                })
                if len(out) >= limit:
                    return out
    return out


def collect_failed_tools(limit: int) -> list[dict]:
    from harness.storage.db import get_conn

    out = []
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT session_id, traces_json, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception:
        return out

    for r in rows:
        try:
            traces = json.loads(r["traces_json"] or "[]")
        except Exception:
            continue
        for t in traces:
            failed = [s for s in t.get("steps", [])
                      if s.get("tool_result") and s["tool_result"].get("success") is False]
            if not failed:
                continue
            tool_names = [s["tool_call"]["tool_name"] for s in failed if s.get("tool_call")]
            out.append({
                "source": "trace",
                "reason": f"工具失败: {','.join(tool_names)}",
                "query": t.get("user", "")[:80],
                "session_id": r["session_id"],
                "ts": t.get("ts"),
                "steps_preview": len(t.get("steps", [])),
                "suggest_layer": "routing",
            })
            if len(out) >= limit:
                return out
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 badcase 候选")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    cases = collect_blocked(args.limit) + collect_failed_tools(args.limit)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"badcase_{time.strftime('%Y%m%d')}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"导出 {len(cases)} 条 badcase 候选 -> {out_path}")
    print("人工评审后可将有效用例追加进 data/eval/golden_set.jsonl 对应 layer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
