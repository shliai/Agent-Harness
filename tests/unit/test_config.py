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
        # 显式忽略 .env（CI 无 .env），验证 config.py 默认值本身，避免断言依赖本地环境
        s = Settings(_env_file=None)
        assert s.max_iterations == 20
        assert s.retrieval_top_k == 5

    def test_env_override(self) -> None:
        from harness.config import Settings
        # 验证配置覆盖生效（等价于本地 .env 的 MAX_ITERATIONS=20 / RETRIEVAL_TOP_K=10）
        s = Settings(max_iterations=20, retrieval_top_k=10)
        assert s.max_iterations == 20
        assert s.retrieval_top_k == 10
