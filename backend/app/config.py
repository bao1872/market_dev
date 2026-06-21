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
3. 默认使用远程数据库
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
    3. 默认使用远程数据库

    Returns:
        str: postgresql+psycopg:// 格式的连接串
    """
    # 1. 环境变量优先
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    # 2. 尝试本地连接
    if _is_local_postgres_available():
        return "postgresql+psycopg://bz:es123456@127.0.0.1:5432/bz_stock"

    # 3. 默认使用远程
    return "postgresql+psycopg://bz:es123456@43.136.118.82:5432/bz_stock"


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
        description="PostgreSQL 连接串（环境变量优先 → 本地 → 远程）",
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
    # 自测入口：打印当前配置（无副作用）
    s = get_settings()
    print(f"app_env={s.app_env}")
    print(f"database_url={s.database_url}")
    print(f"redis_url={s.redis_url}")
    print(f"jwt_algorithm={s.jwt_algorithm}")
