from __future__ import annotations

import pytest

from harness.domain.exceptions import InputValidationError, RateLimitError
from harness.guardrails.base import GuardrailPipeline
from harness.guardrails.input_validator import InputValidator
from harness.guardrails.output_filter import OutputFilter
from harness.guardrails.rate_limiter import RateLimiter


class TestInputValidator:
    def test_empty_input(self) -> None:
        validator = InputValidator()
        with pytest.raises(InputValidationError, match="不能为空"):
            validator.check({"type": "input", "content": ""})

    def test_valid_input(self) -> None:
        validator = InputValidator()
        result = validator.check({"type": "input", "content": "你好"})
        assert result is None

    def test_skip_non_input(self) -> None:
        validator = InputValidator()
        result = validator.check({"type": "output", "content": ""})
        assert result is None


class TestOutputFilter:
    def test_filter_phone(self) -> None:
        filt = OutputFilter()
        result = filt.check({"type": "output", "content": "我的手机是13800138000"})
        assert "***" in result

    def test_filter_id_card(self) -> None:
        filt = OutputFilter()
        result = filt.check({"type": "output", "content": "身份证号110101199001011234"})
        assert "***" in result

    def test_skip_non_output(self) -> None:
        filt = OutputFilter()
        result = filt.check({"type": "input", "content": "13800138000"})
        assert result is None


class TestRateLimiter:
    def test_within_limit(self) -> None:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            result = limiter.check({"type": "input", "content": "test"})
            assert result is None

    def test_exceed_limit(self) -> None:
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.check({"type": "input", "content": "test"})
        with pytest.raises(RateLimitError):
            limiter.check({"type": "input", "content": "test"})


class TestGuardrailPipeline:
    def test_guardrail_pipeline(self) -> None:
        pipeline = GuardrailPipeline()
        pipeline.add(InputValidator())
        pipeline.add(OutputFilter())

        result = pipeline.check_input("你好")
        assert result == "你好"

        result = pipeline.check_output("手机13800138000")
        assert "***" in result
