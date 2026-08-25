from __future__ import annotations

import pytest



class TestSettings:
    def test_default_log_level(self) -> None:
        from harness.config import Settings
        s = Settings()
        assert s.log_level == "INFO"

    def test_invalid_log_level(self) -> None:
        from harness.config import Settings
        with pytest.raises(ValueError):
            Settings(log_level="invalid")

    def test_default_values(self) -> None:
        from harness.config import Settings
        s = Settings()
        # 以下值均被 .env 显式覆盖（MAX_ITERATIONS=20 / RETRIEVAL_TOP_K=20），尊重 .env
        assert s.max_iterations == 20
        assert s.short_term_window == 100
        assert s.retrieval_top_k == 20
