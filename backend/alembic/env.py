"""Alembic 异步迁移环境配置。

使用 psycopg3 同步驱动执行迁移（postgresql+psycopg://）。
Target metadata 从 app.models 导入，支持 autogenerate（当前为空，手动编写迁移）。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 确保 backend/ 在 sys.path 中，以便导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

# Alembic 配置对象
config = context.config

# 从应用配置读取 DATABASE_URL，覆盖 alembic.ini 中的默认值
# 优先使用环境变量，便于 CI/CD 注入
settings = get_settings()
db_url = os.environ.get("DATABASE_URL", settings.database_url)
config.set_main_option("sqlalchemy.url", db_url)

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata（用于 autogenerate）
# 当前为空，迁移手动编写；后续添加 ORM 模型后从此导入
target_metadata = None


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
