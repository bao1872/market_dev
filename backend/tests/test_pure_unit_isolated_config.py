"""纯单元测试环境的隔离合同（CHANGE-20260802-002 配套修复）。

目的：证明 PURE_UNIT_TEST=1 且未设置 DATABASE_URL / TEST_DATABASE_URL 时，
pytest 仍能完成收集、非 postgres/external_data 测试可执行，且不会发生任何
真实数据库连接。

这些断言本身在纯单元 job 中运行（不连库），因此它们的存在即证明
collection 阶段未因 import 期 MissingRequiredSettingError 而中断。

该测试不导入任何会建立真实连接的模块；它只验证环境契约与 sentinel 不可达。
"""

from __future__ import annotations

import os
import socket

import pytest

_PURE_UNIT_DB_SENTINEL = (
    "postgresql+asyncpg://panji_unit:panji_unit@127.0.0.1:1/panji_unit_unreachable"
)


def test_pure_unit_env_contract() -> None:
    """纯单元环境契约：DATABASE_URL 为 sentinel，TEST_DATABASE_URL 未设置。"""
    # 本测试仅在 PURE_UNIT_TEST=1 且有意义的纯单元场景下有效。
    assert os.environ.get("PURE_UNIT_TEST", "").lower() in ("1", "true", "yes")
    # 不设置 TEST_DATABASE_URL（避免与 PG job 语义混淆）。
    assert not os.environ.get("TEST_DATABASE_URL"), (
        "纯单元模式不得设置 TEST_DATABASE_URL"
    )
    # DATABASE_URL 必须被 conftest 注入为不可连接 sentinel。
    assert os.environ.get("DATABASE_URL") == _PURE_UNIT_DB_SENTINEL


def test_pure_unit_settings_resolved_without_real_db() -> None:
    """import 期 get_settings() 用 sentinel 完成解析，不连接数据库。"""
    from app.config import get_settings

    settings = get_settings()
    assert settings.database_url == _PURE_UNIT_DB_SENTINEL
    assert settings.app_env == "pure-unit"


def test_sentinel_db_is_unreachable() -> None:
    """sentinel 地址（127.0.0.1:1）不可达：任何真实连接都会失败。

    这同时验证 requirement 8 —— 若 pure-unit 测试错误地尝试连接 sentinel，
    会因连接拒绝而失败，暴露错误的分类，而非静默通过。
    """
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", 1), timeout=2):
            pass
