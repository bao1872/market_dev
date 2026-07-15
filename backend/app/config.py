"""应用配置 - 结构与加载规则（实际值在 config.local.py / config.test.py / CONFIG_FILE）。

职责分离：
- 本文件（config.py）：Settings 结构定义、加载规则、启动硬校验。**入库**。
- config.local.py：开发环境实际值。**不入库**（含本地密码）。
- config.test.py：测试环境实际值。**不入库**。
- config.example.py：必需字段示例。**入库**。
- CONFIG_FILE 环境变量指向的外部文件（如 /etc/market-dev/config.production.py）：生产环境。**不入库**。

配置加载优先级（同字段，高到低）：
1. 环境变量（用于 docker 部署、Alembic 子进程、CI）
2. CONFIG_FILE 指向的 Python 配置文件
3. config.local.py（开发环境）/ config.test.py（测试环境）
4. Settings 字段默认值

环境选择（决定加载哪个 config.*.py）：
- CONFIG_MODULE 环境变量显式指定模块名（最高优先级）
- APP_ENV=test → 加载 app.config_test
- 其他 → 加载 app.config_local

启动硬校验（在 Settings 实例化时由 get_settings 触发）：
- 拒绝 sqlite URL（仅允许 PostgreSQL）
- development: DATABASE_URL 必须含 bz_stock 且不得连测试库（不含 _test）
- test: DATABASE_URL 必须含 _test 后缀
- production: DATABASE_URL 不得连测试库（不含 _test）
- production: JWT_SECRET 不得为默认值 change-me 或空
- production: SECRET_MASTER_KEY 不得为开发默认值或空

使用 Pydantic Settings 管理启动级配置：
- DATABASE_URL: PostgreSQL 连接串（postgresql+psycopg://）
- REDIS_URL: Redis 连接串
- JWT_SECRET: JWT 签名密钥
- SECRET_MASTER_KEY: 主密钥
- APP_ENV: 运行环境
- LOG_LEVEL: 日志级别
"""

from __future__ import annotations

import importlib.util
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MissingRequiredSettingError(ValueError):
    """缺少必须的配置项时抛出（如 DATABASE_URL 两个来源都未提供）。"""


class InvalidDatabaseURLError(ValueError):
    """DATABASE_URL 启动硬校验失败时抛出（sqlite / 环境与库名不匹配等）。"""


@lru_cache(maxsize=1)
def _load_py_config() -> dict[str, Any]:
    """从 Python 配置文件读取实际配置值（CONFIG_FILE 优先，否则按 APP_ENV 选 local/test）。

    文件选择：
    - CONFIG_FILE 环境变量存在 → 加载该路径
    - APP_ENV=test → config.test.py
    - 其他 → config.local.py

    注意：文件名含点（config.local.py），不能用 importlib.import_module
    （Python 模块名不允许含点），需用 importlib.util.spec_from_file_location 按路径加载。

    Returns:
        dict: 配置值字典（大写字段名 → 值）

    Raises:
        MissingRequiredSettingError: CONFIG_FILE 指向的文件不存在时抛出
    """
    config_dir = Path(__file__).parent
    config_file_env = os.environ.get("CONFIG_FILE")
    if config_file_env:
        config_file = Path(config_file_env)
        if not config_file.is_absolute():
            config_file = config_dir / config_file
        if not config_file.exists():
            raise MissingRequiredSettingError(
                f"CONFIG_FILE 指向的配置文件 {config_file} 不存在。"
            )
    else:
        app_env = os.environ.get("APP_ENV", "development").lower()
        if app_env == "test":
            config_file = config_dir / "config.test.py"
        else:
            config_file = config_dir / "config.local.py"
        # [配置加载] - 描述: 默认配置文件不存在时返回空字典，允许仅通过环境变量启动
        if not config_file.exists():
            return {}
    spec = importlib.util.spec_from_file_location("_py_config", config_file)
    if spec is None or spec.loader is None:
        raise MissingRequiredSettingError(f"无法加载配置文件 {config_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {attr: getattr(module, attr) for attr in dir(module) if attr.isupper()}


def _resolve_database_url() -> str:
    """解析数据库连接串。

    优先级：
    1. 环境变量 DATABASE_URL（docker 部署、Alembic 子进程、CI）
    2. CONFIG_FILE 指向的配置文件
    3. config.local.py / config.test.py

    Returns:
        str: postgresql+psycopg:// 格式的连接串

    Raises:
        MissingRequiredSettingError: 所有来源都未提供时抛出
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    file_url = _load_py_config().get("DATABASE_URL")
    if file_url:
        return file_url
    raise MissingRequiredSettingError(
        "DATABASE_URL 未设置。请通过环境变量、CONFIG_FILE 配置文件、"
        "config.local.py 或 config.test.py 提供，"
        "例如 postgresql+psycopg://user:password@host:port/dbname"
    )


def _safe_database_url(url: str) -> str:
    """返回脱敏后的数据库 URL（隐藏密码），用于日志与异常。

    Args:
        url: 原始数据库连接串

    Returns:
        str: 隐藏密码后的连接串；解析失败时返回原串
    """
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username or ''}:***@{parsed.hostname or ''}"
            if parsed.port:
                netloc += f":{parsed.port}"
            parsed = parsed._replace(netloc=netloc)
            return urlunparse(parsed)
    except Exception:
        pass
    return url


def _validate_database_url(url: str, app_env: str) -> None:
    """启动硬校验 DATABASE_URL 安全性。

    校验规则：
    - 拒绝 sqlite URL（仅允许 PostgreSQL）
    - development: 必须含 bz_stock 且不含 _test
    - test: 必须含 _test 后缀
    - production: 不得含 _test

    Raises:
        InvalidDatabaseURLError: 校验失败时抛出，阻止应用启动
    """
    safe_url = _safe_database_url(url)
    if "sqlite" in url.lower():
        raise InvalidDatabaseURLError(
            f"拒绝启动：DATABASE_URL 含 sqlite，仅允许 PostgreSQL。URL={safe_url}"
        )
    env = (app_env or "").lower()
    if env == "development":
        if "bz_stock" not in url:
            raise InvalidDatabaseURLError(
                f"开发环境 DATABASE_URL 必须含 bz_stock，实际={safe_url}"
            )
        if "_test" in url:
            raise InvalidDatabaseURLError(
                f"开发环境 DATABASE_URL 不得连测试库（含 _test），实际={safe_url}"
            )
    elif env == "test":
        if "_test" not in url:
            raise InvalidDatabaseURLError(
                f"测试环境 DATABASE_URL 必须含 _test 后缀，实际={safe_url}"
            )
    elif env == "production":
        if "_test" in url:
            raise InvalidDatabaseURLError(
                f"生产环境 DATABASE_URL 不得连测试库（含 _test），实际={safe_url}"
            )


def _validate_worker_urls(frontend_base_url: str, capture_worker_url: str, app_env: str) -> None:
    """启动硬校验截图相关地址：生产环境禁止默认 localhost 连接其他容器。

    校验规则：
    - 仅对需要截图的 worker 强制校验（backend / monitor_scheduler / after_close_orchestrator）
    - production: frontend_base_url 不得为默认 http://localhost:5173（容器间无法通过 localhost 互访）
    - production: capture_worker_url 不得指向 localhost
    - 失败时抛 ValueError 阻止启动（fail-fast，不吞异常）
    """
    env = (app_env or "").lower()
    if env != "production":
        return
    # [截图Worker校验] - 仅截图链路上的 worker 强制校验地址，其他 worker 不需要截图配置
    worker_type = (os.getenv("WORKER_TYPE") or "").lower()
    capture_required_workers = {"", "monitor_scheduler", "after_close_orchestrator"}
    if worker_type not in capture_required_workers:
        return
    if "localhost:5173" in frontend_base_url or "127.0.0.1:5173" in frontend_base_url:
        raise ValueError(
            "拒绝启动：生产环境 frontend_base_url 不得为默认 localhost:5173（容器间无法互访），"
            f"请设置 FRONTEND_BASE_URL=http://frontend。实际={frontend_base_url}"
        )
    if "localhost" in capture_worker_url or "127.0.0.1" in capture_worker_url:
        raise ValueError(
            "拒绝启动：生产环境 capture_worker_url 不得指向 localhost（容器间无法互访），"
            f"请设置 CAPTURE_WORKER_URL=http://worker-capture:8001。实际={capture_worker_url}"
        )


class _SecuritySettings(Protocol):
    """生产环境安全校验所需的最小配置协议（结构化类型）。

    允许 Settings 实例与 __main__ 自测中的 mock settings 类通过结构匹配传入
    _validate_security_settings，无需继承 Settings。
    """

    app_env: str
    jwt_secret: str
    secret_master_key: str


def _validate_security_settings(settings: _SecuritySettings) -> None:
    """启动硬校验生产环境密钥安全性。

    校验规则：
    - production: JWT_SECRET 不得为默认值 change-me 或空
    - production: SECRET_MASTER_KEY 不得为开发默认值或空

    Raises:
        MissingRequiredSettingError: 校验失败时抛出，阻止应用启动
    """
    env = (settings.app_env or "").lower()
    if env != "production":
        return
    if not settings.jwt_secret or settings.jwt_secret == "change-me":
        raise MissingRequiredSettingError(
            "拒绝启动：生产环境 JWT_SECRET 必须使用强密钥，"
            "不能为默认值 'change-me' 或空字符串。"
        )
    weak_master_keys = {
        "replace-in-development-only",
        "local-dev-only",
    }
    if not settings.secret_master_key or settings.secret_master_key in weak_master_keys:
        raise MissingRequiredSettingError(
            "拒绝启动：生产环境 SECRET_MASTER_KEY 必须使用强密钥，"
            "不能为开发默认值或空字符串。"
        )


class Settings(BaseSettings):
    """启动级配置，仅环境变量或 Python 配置文件；业务密钥进入加密配置中心。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 运行环境
    app_env: str = Field(default="development", description="运行环境")
    log_level: str = Field(default="INFO", description="日志级别")

    # 数据库（postgresql+psycopg://，环境变量优先，否则从配置文件读取）
    database_url: str = Field(
        default_factory=_resolve_database_url,
        description="PostgreSQL 连接串（环境变量 DATABASE_URL 优先，否则从配置文件读取）",
    )

    # Redis
    redis_url: str = Field(
        default_factory=lambda: _load_py_config().get("REDIS_URL", "redis://localhost:6379/0"),
        description="Redis 连接串",
    )

    # JWT
    jwt_secret: str = Field(
        default_factory=lambda: _load_py_config().get("JWT_SECRET", "change-me"),
        description="JWT 签名密钥",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    jwt_access_ttl_seconds: int = Field(default=3600, description="Access token 有效期（秒）")
    jwt_refresh_ttl_seconds: int = Field(default=604800, description="Refresh token 有效期（秒）")
    jwt_capture_ttl_seconds: int = Field(
        default=300, description="截图模式短期 token 有效期（秒）"
    )

    # 前端地址（截图服务访问个股详情页使用）
    frontend_base_url: str = Field(
        default_factory=lambda: _load_py_config().get(
            "FRONTEND_BASE_URL", "http://localhost:5173"
        ),
        description="前端 base URL",
    )

    # 截图 Worker 地址（backend 调用截图服务使用）
    capture_worker_url: str = Field(
        default_factory=lambda: _load_py_config().get(
            "CAPTURE_WORKER_URL", "http://worker-capture:8001"
        ),
        description="截图 Worker HTTP 服务地址",
    )

    # 密钥管理（仅启动级占位，业务密钥进入配置中心）
    secret_master_key_provider: str = Field(
        default_factory=lambda: _load_py_config().get(
            "SECRET_MASTER_KEY_PROVIDER", "local-dev-only"
        ),
        description="密钥管理提供方",
    )
    secret_master_key: str = Field(
        default_factory=lambda: _load_py_config().get(
            "SECRET_MASTER_KEY", "replace-in-development-only"
        ),
        description="主密钥（仅开发环境）",
    )

    # 行情数据源配置（策略模式，参考 Chanlunpro exchange 设计）
    bars_data_source: str = Field(
        default_factory=lambda: _load_py_config().get("BARS_DATA_SOURCE", "pytdx"),
        description="行情数据源: pytdx / db",
    )
    bars_redis_cache_enabled: bool = Field(
        default_factory=lambda: _load_py_config().get("BARS_REDIS_CACHE_ENABLED", False),
        description="是否启用 Redis 查询缓存",
    )
    bars_redis_cache_ttl_seconds: int = Field(
        default_factory=lambda: _load_py_config().get("BARS_REDIS_CACHE_TTL_SECONDS", 60),
        description="Redis 缓存 TTL（秒）",
    )

    # 板块同步开关（C10 降级保护）
    # 生产默认 false：当前物理机 IP 被 THS 反爬拦截（成分股 403），
    # akshare 无 THS 成分接口。未配置时不得发起 THS 请求。
    # ALIGN-041 OPEN：至少一个同花顺语义 provider 真实返回完整目录+成分后方可开启。
    board_sync_enabled: bool = Field(
        default_factory=lambda: _load_py_config().get("BOARD_SYNC_ENABLED", False),
        description="板块同步开关：false 时 scheduled_board_sync 跳过执行",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回单例 Settings，并在返回前执行启动硬校验。

    校验规则见 _validate_database_url / _validate_worker_urls / _validate_security_settings。
    失败时抛异常，阻止应用启动（fail-fast，不吞异常）。

    注意：校验放在此处而非 model_post_init，避免 pydantic v2 将
    InvalidDatabaseURLError 包装为 ValidationError，导致调用方无法精确捕获。
    """
    s = Settings()
    _validate_database_url(s.database_url, s.app_env)
    _validate_security_settings(s)
    _validate_worker_urls(s.frontend_base_url, s.capture_worker_url, s.app_env)
    # [启动日志] - 描述: 打印截图相关生效地址与脱敏数据库 URL，便于排查容器间互访问题
    import logging
    logging.getLogger("app.config").info(
        "[启动配置] frontend_base_url=%s capture_worker_url=%s app_env=%s database_url=%s",
        s.frontend_base_url, s.capture_worker_url, s.app_env, _safe_database_url(s.database_url)
    )
    return s


if __name__ == "__main__":
    # 自测入口：验证配置加载与硬校验行为（无副作用，不实际连接数据库）
    # 直接调用 _validate_database_url / _validate_security_settings 验证校验规则

    # 场景 1：sqlite URL 必须拒绝（任何环境）
    try:
        _validate_database_url("sqlite:///./test.db", "development")
        raise AssertionError("sqlite URL 应被拒绝")
    except InvalidDatabaseURLError as exc:
        print(f"sqlite_rejected: {exc}")

    # 场景 2：development + bz_stock 通过
    _validate_database_url(
        "postgresql+psycopg://u:p@h:5432/bz_stock", "development"
    )
    print("dev_ok: postgresql+psycopg://u:***@h:5432/bz_stock")

    # 场景 3：development + _test 库拒绝（开发不得连测试库）
    try:
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/bz_stock_test", "development"
        )
        raise AssertionError("开发环境连测试库应被拒绝")
    except InvalidDatabaseURLError as exc:
        print(f"dev_test_rejected: {exc}")

    # 场景 4：test 环境 + _test 库通过
    _validate_database_url(
        "postgresql+psycopg://u:p@h:5432/bz_stock_test", "test"
    )
    print("test_ok: postgresql+psycopg://u:***@h:5432/bz_stock_test")

    # 场景 5：production + _test 库拒绝（生产不得连测试库）
    try:
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/bz_stock_test", "production"
        )
        raise AssertionError("生产环境连测试库应被拒绝")
    except InvalidDatabaseURLError as exc:
        print(f"prod_test_rejected: {exc}")

    # 场景 6：production + 正式库通过
    _validate_database_url(
        "postgresql+psycopg://u:p@h:5432/bz_stock", "production"
    )
    print("prod_ok: postgresql+psycopg://u:***@h:5432/bz_stock")

    # 场景 7：development + 非 bz_stock 库拒绝
    try:
        _validate_database_url(
            "postgresql+psycopg://u:p@h:5432/other_db", "development"
        )
        raise AssertionError("开发环境非 bz_stock 库应被拒绝")
    except InvalidDatabaseURLError as exc:
        print(f"dev_other_db_rejected: {exc}")

    # 场景 8：生产环境弱 JWT_SECRET 拒绝
    class _FakeSettings:
        app_env = "production"
        jwt_secret = "change-me"
        secret_master_key = "strong-master-key"

    try:
        _validate_security_settings(_FakeSettings())
        raise AssertionError("生产环境 change-me JWT_SECRET 应被拒绝")
    except MissingRequiredSettingError as exc:
        print(f"prod_weak_jwt_rejected: {exc}")

    # 场景 9：生产环境弱 SECRET_MASTER_KEY 拒绝
    class _FakeSettings2:
        app_env = "production"
        jwt_secret = "strong-jwt-secret"
        secret_master_key = "replace-in-development-only"

    try:
        _validate_security_settings(_FakeSettings2())
        raise AssertionError("生产环境默认 SECRET_MASTER_KEY 应被拒绝")
    except MissingRequiredSettingError as exc:
        print(f"prod_weak_master_key_rejected: {exc}")

    print("OK")
