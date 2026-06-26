"""app.config 启动硬校验单元测试。

测试覆盖：
- sqlite URL 拒绝（各环境）
- development: 必须 bz_stock、不得 _test
- test: 必须 _test
- production: 不得 _test
- _validate_database_url 函数直接调用
- get_settings() 集成校验（启动硬校验入口）

运行方式（避开 conftest.py 的 DB 依赖，本测试为纯单元测试）：
    docker exec trading-backend python -m pytest tests/test_config_validation.py -v --noconftest

约束：
- 不依赖 conftest.py 的 DB fixture（纯单元测试）
- 不实际连接数据库
- get_settings() 测试用 monkeypatch 设置环境变量 + cache_clear()
"""
import pytest

from app.config import (
    InvalidDatabaseURLError,
    _validate_database_url,
    get_settings,
)


# ---------------------------------------------------------------------------
# _validate_database_url 直接调用测试
# ---------------------------------------------------------------------------


class TestValidateDatabaseUrlDirect:
    """直接调用 _validate_database_url 的单元测试。"""

    def test_sqlite_rejected_in_development(self):
        """sqlite URL 在开发环境必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="sqlite"):
            _validate_database_url("sqlite:///./test.db", "development")

    def test_sqlite_rejected_in_test(self):
        """sqlite URL 在测试环境必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="sqlite"):
            _validate_database_url("sqlite+aiosqlite:///:memory:", "test")

    def test_sqlite_rejected_in_production(self):
        """sqlite URL 在生产环境必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="sqlite"):
            _validate_database_url("sqlite:///./prod.db", "production")

    def test_sqlite_case_insensitive_rejected(self):
        """sqlite 大小写混合也必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="sqlite"):
            _validate_database_url("SQLITE:///./test.db", "development")

    def test_dev_bz_stock_passes(self):
        """开发环境 + bz_stock 库通过。"""
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/bz_stock", "development"
        )

    def test_dev_test_db_rejected(self):
        """开发环境连测试库（含 _test）必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="不得连测试库"):
            _validate_database_url(
                "postgresql+psycopg://u:p@h:5432/bz_stock_test", "development"
            )

    def test_dev_non_bz_stock_rejected(self):
        """开发环境非 bz_stock 库必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="必须含 bz_stock"):
            _validate_database_url(
                "postgresql+psycopg://u:p@h:5432/other_db", "development"
            )

    def test_test_env_with_test_suffix_passes(self):
        """测试环境 + _test 后缀库通过。"""
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/bz_stock_test", "test"
        )

    def test_test_env_without_test_suffix_rejected(self):
        """测试环境非 _test 库必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="必须含 _test"):
            _validate_database_url(
                "postgresql+psycopg://u:p@h:5432/bz_stock", "test"
            )

    def test_production_test_db_rejected(self):
        """生产环境连测试库必须拒绝。"""
        with pytest.raises(InvalidDatabaseURLError, match="不得连测试库"):
            _validate_database_url(
                "postgresql+psycopg://u:p@h:5432/bz_stock_test", "production"
            )

    def test_production_formal_db_passes(self):
        """生产环境 + 正式库通过。"""
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/bz_stock", "production"
        )

    def test_unknown_env_no_extra_check(self):
        """未知环境（非 dev/test/prod）只校验 sqlite，不校验库名。"""
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/any_db", "staging"
        )


# ---------------------------------------------------------------------------
# get_settings() 集成校验测试（启动硬校验入口）
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_settings_cache():
    """每个测试前后清理 get_settings 的 lru_cache，避免环境变量污染。

    get_settings() 用 lru_cache 缓存单例，测试用 monkeypatch 改环境变量后
    必须清缓存才能重新实例化。测试后再清一次，避免影响其他测试。
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestGetSettingsValidation:
    """通过 get_settings() 验证启动硬校验（校验入口在 get_settings 中）。"""

    def test_sqlite_url_blocks_startup(self, monkeypatch, reset_settings_cache):
        """sqlite URL 必须阻止 get_settings() 返回（启动失败）。"""
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
        monkeypatch.setenv("APP_ENV", "development")
        with pytest.raises(InvalidDatabaseURLError, match="sqlite"):
            get_settings()

    def test_dev_with_test_db_blocks_startup(
        self, monkeypatch, reset_settings_cache
    ):
        """开发环境连测试库必须阻止启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock_test"
        )
        monkeypatch.setenv("APP_ENV", "development")
        with pytest.raises(InvalidDatabaseURLError, match="不得连测试库"):
            get_settings()

    def test_production_with_test_db_blocks_startup(
        self, monkeypatch, reset_settings_cache
    ):
        """生产环境连测试库必须阻止启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock_test"
        )
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(InvalidDatabaseURLError, match="不得连测试库"):
            get_settings()

    def test_test_env_without_test_suffix_blocks_startup(
        self, monkeypatch, reset_settings_cache
    ):
        """测试环境非 _test 库必须阻止启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock"
        )
        monkeypatch.setenv("APP_ENV", "test")
        with pytest.raises(InvalidDatabaseURLError, match="必须含 _test"):
            get_settings()

    def test_dev_bz_stock_starts_successfully(
        self, monkeypatch, reset_settings_cache
    ):
        """开发环境 + bz_stock 库正常启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock"
        )
        monkeypatch.setenv("APP_ENV", "development")
        s = get_settings()
        assert "bz_stock" in s.database_url
        assert "_test" not in s.database_url

    def test_test_env_with_test_suffix_starts_successfully(
        self, monkeypatch, reset_settings_cache
    ):
        """测试环境 + _test 库正常启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock_test"
        )
        monkeypatch.setenv("APP_ENV", "test")
        s = get_settings()
        assert "_test" in s.database_url

    def test_production_formal_db_starts_successfully(
        self, monkeypatch, reset_settings_cache
    ):
        """生产环境 + 正式库正常启动。"""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/bz_stock"
        )
        monkeypatch.setenv("APP_ENV", "production")
        s = get_settings()
        assert "_test" not in s.database_url


# ---------------------------------------------------------------------------
# 模块自测入口（与 app/config.py __main__ 对齐，便于单独验证）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 手动运行：python -m pytest tests/test_config_validation.py -v --noconftest
    import sys

    sys.exit(pytest.main([__file__, "-v", "--noconftest"]))
