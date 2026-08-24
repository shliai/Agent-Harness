"""审计日志轮转单元测试（v0.5.1）"""
from __future__ import annotations



class TestAuditRotation:
    def test_rotation_triggered_at_threshold(self, tmp_path, monkeypatch) -> None:
        from harness.config import settings
        from harness.guardrails.audit_logger import AuditLogger

        monkeypatch.setattr(settings, "audit_rotate_mb", 0)  # 0MB → 每次写入前必轮转
        al = AuditLogger(log_path=tmp_path)

        # 第一次写入建立基础文件
        al._write_record({"i": 0})
        base = tmp_path / f"audit_{al._write_record.__self__ and __import__('datetime').datetime.now().strftime('%Y-%m-%d')}.jsonl"
        assert base.exists()

        # 第二次写入触发轮转：原文件 → .1.jsonl，新内容写入主文件
        al._write_record({"i": 1})

        rotated = list(tmp_path.glob("audit_*.1.jsonl"))
        assert len(rotated) == 1, "应产生一个轮转文件"
        assert '{"i": 0}' in rotated[0].read_text(encoding="utf-8")
        assert '{"i": 1}' in base.read_text(encoding="utf-8")

    def test_no_rotation_below_threshold(self, tmp_path, monkeypatch) -> None:
        from harness.config import settings
        from harness.guardrails.audit_logger import AuditLogger

        monkeypatch.setattr(settings, "audit_rotate_mb", 16)
        al = AuditLogger(log_path=tmp_path)
        al._write_record({"small": True})
        import datetime as _dt

        base = tmp_path / f"audit_{_dt.datetime.now().strftime('%Y-%m-%d')}.jsonl"
        al._rotate_if_needed(base)
        assert list(tmp_path.glob("audit_*.1.jsonl")) == []
