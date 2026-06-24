"""应用配置 - 从环境变量读取。

使用 Pydantic Settings 管理启动级配置：
- DATABASE_URL: PostgreSQL 连接串（postgresql+psycopg://）
- REDIS_URL: Redis 连接串
- JWT_SECRET: JWT 签名密钥
- APP_ENV: 运行环境
- LOG_LEVEL: 日志级别

数据库连接选择策略（按优先级）：
1. 环境变量 DATABASE_URL 优先
2. 本地 PostgreSQL 可用则使用本地连接
3. 无可用连接时抛出 ValueError，禁止回退到硬编码远程数据库
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_local_postgres_available(host: str = "127.0.0.1", port: int = 5432, timeout: float = 0.5) -> bool:
    """探测本地 PostgreSQL 端口是否可连通。

    Args:
        host: 探测主机，默认 127.0.0.1
        port: 探测端口，默认 5432
        timeout: 探测超时（秒），默认 0.5s

    Returns:
        bool: 本地 PostgreSQL 是否可用
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_database_url() -> str:
    """解析数据库连接串。

    优先级：
    1. 环境变量 DATABASE_URL（必须是 psycopg 驱动格式 postgresql+psycopg://）
    2. 本地 PostgreSQL 可用则使用本地连接
    3. 无可用连接时抛出 ValueError

    Returns:
        str: postgresql+psycopg:// 格式的连接串

    Raises:
        ValueError: 环境变量未设置且本地 PostgreSQL 不可用时抛出
    """
    # 1. 环境变量优先
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    # 2. 尝试本地连接
    if _is_local_postgres_available():
        return "postgresql+psycopg://bz:YOUR_PASSWORD@127.0.0.1:5432/bz_stock"

    # 3. 无可用连接时明确失败，禁止回退到硬编码远程数据库
    raise ValueError(
        "DATABASE_URL 环境变量未设置，且本地 PostgreSQL (127.0.0.1:5432) 不可用。"
        "请在环境变量中配置 DATABASE_URL，例如 postgresql+psycopg://user:password@host:port/dbname"
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

    # 数据库（postgresql+psycopg://，动态解析）
    database_url: str = Field(
        default_factory=_resolve_database_url,
        description="PostgreSQL 连接串（环境变量优先 → 本地；无可用连接时抛出错误）",
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
    # 自测入口：验证 database_url 解析逻辑（无副作用，不实际连接数据库）
    original_url = os.environ.get("DATABASE_URL")
    try:
        # 场景 1：环境变量存在时直接返回
        os.environ["DATABASE_URL"] = "postgresql+psycopg://user:pass@localhost:5432/db"
        resolved = _resolve_database_url()
        print(f"with_env database_url={resolved}")
        assert resolved == os.environ["DATABASE_URL"]

        # 场景 2：无环境变量且无本地 PG 时抛出 ValueError
        os.environ.pop("DATABASE_URL", None)
        # 强制本地探测失败，避免实际连接
        original_checker = _is_local_postgres_available
        _is_local_postgres_available = lambda **kwargs: False
        try:
            _resolve_database_url()
            raise AssertionError("应抛出 ValueError")
        except ValueError as e:
            print(f"expected_error={e}")
        finally:
            _is_local_postgres_available = original_checker
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
    print("OK")
