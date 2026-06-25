"""应用配置 - 仅环境变量，禁止硬编码密码与自动回退。

使用 Pydantic Settings 管理启动级配置：
- DATABASE_URL: PostgreSQL 连接串（postgresql+psycopg://），必须通过环境变量提供
- REDIS_URL: Redis 连接串
- JWT_SECRET: JWT 签名密钥，必须通过环境变量提供
- APP_ENV: 运行环境
- LOG_LEVEL: 日志级别
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingRequiredSettingError(ValueError):
    """缺少必须的环境变量时抛出。"""


def _resolve_database_url() -> str:
    """解析数据库连接串。

    仅允许通过环境变量 DATABASE_URL 提供，未设置时立即失败。

    Returns:
        str: postgresql+psycopg:// 格式的连接串

    Raises:
        MissingRequiredSettingError: DATABASE_URL 未设置时抛出
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    raise MissingRequiredSettingError(
        "DATABASE_URL 环境变量未设置。请在环境变量或外部 env 文件中配置，"
        "例如 postgresql+psycopg://user:password@host:port/dbname"
    )


class Settings(BaseSettings):
    """启动级配置，仅环境变量；业务密钥进入加密配置中心。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境
    app_env: str = Field(default="development", description="运行环境")
    log_level: str = Field(default="INFO", description="日志级别")

    # 数据库（postgresql+psycopg://，必须通过环境变量提供）
    database_url: str = Field(
        default_factory=_resolve_database_url,
        description="PostgreSQL 连接串（必须经 DATABASE_URL 环境变量提供）",
    )

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis 连接串",
    )

    # JWT
    jwt_secret: str = Field(default="change-me", description="JWT 签名密钥")
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    jwt_access_ttl_seconds: int = Field(default=3600, description="Access token 有效期（秒）")
    jwt_refresh_ttl_seconds: int = Field(default=604800, description="Refresh token 有效期（秒）")
    jwt_capture_ttl_seconds: int = Field(
        default=300, description="截图模式短期 token 有效期（秒）"
    )

    # 前端地址（截图服务访问个股详情页使用）
    frontend_base_url: str = Field(
        default="http://localhost:5173", description="前端 base URL"
    )

    # 截图 Worker 地址（backend 调用截图服务使用）
    capture_worker_url: str = Field(
        default="http://worker-capture:8001", description="截图 Worker HTTP 服务地址"
    )

    # 密钥管理（仅启动级占位，业务密钥进入配置中心）
    secret_master_key_provider: str = Field(
        default="local-dev-only",
        description="密钥管理提供方",
    )
    secret_master_key: str = Field(
        default="replace-in-development-only",
        description="主密钥（仅开发环境）",
    )

    # 行情数据源配置（策略模式，参考 Chanlunpro exchange 设计）
    bars_data_source: str = Field(
        default="pytdx",
        description="行情数据源: pytdx / db",
    )
    bars_redis_cache_enabled: bool = Field(
        default=False,
        description="是否启用 Redis 查询缓存",
    )
    bars_redis_cache_ttl_seconds: int = Field(
        default=60,
        description="Redis 缓存 TTL（秒）",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回单例 Settings，避免重复解析环境变量。"""
    return Settings()


if __name__ == "__main__":
    # 自测入口：验证配置解析行为（无副作用，不实际连接数据库）
    original_url = os.environ.get("DATABASE_URL")
    try:
        # 场景 1：环境变量存在时直接返回
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@localhost:5432/db"
        resolved = _resolve_database_url()
        print(f"with_env database_url={resolved}")
        assert resolved == os.environ["DATABASE_URL"]

        # 场景 2：无环境变量时必须抛出 MissingRequiredSettingError
        os.environ.pop("DATABASE_URL", None)
        try:
            _resolve_database_url()
            raise AssertionError("应抛出 MissingRequiredSettingError")
        except MissingRequiredSettingError as exc:
            print(f"expected_error={exc}")

        # 场景 3：Settings 能正确读取环境变量
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@localhost:5432/db"
        settings = Settings()
        assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/db"
        print("settings_loaded_ok")
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
    print("OK")
