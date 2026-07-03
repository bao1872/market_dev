"""Phase C: 飞书 Platform App Only 约束测试。

TDD 红灯阶段：验证 feishu_webhook 运行时已被永久删除，统一为 Platform App only。

测试用例：
1. test_create_channel_rejects_feishu_webhook: 创建 feishu_webhook 渠道应被拒绝
2. test_create_channel_accepts_feishu_platform_app: 创建 feishu_platform_app 渠道成功
3. test_feishu_webhook_adapter_file_deleted: feishu_webhook_adapter 模块不存在
4. test_feishu_adapter_types_only_platform_app: _FEISHU_ADAPTER_TYPES 仅含 platform_app
5. test_admin_feishu_config_returns_platform_app_format: 管理员配置返回 Platform App 格式
6. test_migration_fails_when_feishu_webhook_rows_exist: migration 在有 webhook 行时主动失败
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

# ============================================================
# 测试 3: feishu_webhook_adapter 模块已删除
# ============================================================


def test_feishu_webhook_adapter_file_deleted() -> None:
    """app.services.feishu_webhook_adapter 模块应已删除（ImportError）。"""
    with pytest.raises(ImportError):
        importlib.import_module("app.services.feishu_webhook_adapter")


# ============================================================
# 测试 4: _FEISHU_ADAPTER_TYPES 仅含 feishu_platform_app
# ============================================================


def test_feishu_adapter_types_only_platform_app() -> None:
    """_FEISHU_ADAPTER_TYPES 应仅包含 feishu_platform_app。"""
    from app.services.notification_service import _FEISHU_ADAPTER_TYPES

    assert _FEISHU_ADAPTER_TYPES == {"feishu_platform_app"}
    assert "feishu_webhook" not in _FEISHU_ADAPTER_TYPES


# ============================================================
# 测试 5: 管理员飞书配置返回 Platform App 格式
# ============================================================


def test_admin_feishu_config_returns_platform_app_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_admin_feishu_config() 应返回 app_id/app_secret/receive_id/receive_id_type 格式。"""
    monkeypatch.setenv("ADMIN_FEISHU_APP_ID", "cli_test_app_001")
    monkeypatch.setenv("ADMIN_FEISHU_APP_SECRET", "test_secret_value")
    monkeypatch.setenv("ADMIN_FEISHU_RECEIVE_ID", "bg33237")
    monkeypatch.setenv("ADMIN_FEISHU_RECEIVE_ID_TYPE", "user_id")

    # 清理旧 webhook 环境变量
    monkeypatch.delenv("ADMIN_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is not None
    assert config["app_id"] == "cli_test_app_001"
    assert config["app_secret"] == "test_secret_value"
    assert config["receive_id"] == "bg33237"
    assert config["receive_id_type"] == "user_id"
    # 不应包含 webhook 字段
    assert "webhook_url" not in config
    assert "sign_secret" not in config


def test_admin_feishu_config_returns_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置 Platform App 环境变量时返回 None。"""
    monkeypatch.delenv("ADMIN_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_RECEIVE_ID", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_RECEIVE_ID_TYPE", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is None


def test_admin_feishu_config_receive_id_type_defaults_to_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置 RECEIVE_ID_TYPE 时默认为 user_id。"""
    monkeypatch.setenv("ADMIN_FEISHU_APP_ID", "cli_test_app_002")
    monkeypatch.setenv("ADMIN_FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("ADMIN_FEISHU_RECEIVE_ID", "bg12345")
    monkeypatch.delenv("ADMIN_FEISHU_RECEIVE_ID_TYPE", raising=False)

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is not None
    assert config["receive_id_type"] == "user_id"


# ============================================================
# 测试 1: create_channel 拒绝 feishu_webhook
# ============================================================


@pytest.mark.asyncio
async def test_create_channel_rejects_feishu_webhook(
    db_session, test_user,
) -> None:
    """创建 adapter_type='feishu_webhook' 渠道应被拒绝（ValueError）。"""
    from app.services.notification_service import (
        NotificationServiceError,
        create_channel,
    )

    with pytest.raises((ValueError, NotificationServiceError)) as exc_info:
        await create_channel(
            db=db_session,
            user_id=test_user.id,
            adapter_type="feishu_webhook",
            display_name="应被拒绝的Webhook",
            target_config={"webhook_url": "http://example.com/hook"},
        )
    # 错误信息应明确提及 feishu_webhook 不再支持
    assert "feishu_webhook" in str(exc_info.value)


# ============================================================
# 测试 2: create_channel 接受 feishu_platform_app
# ============================================================


@pytest.mark.asyncio
async def test_create_channel_accepts_feishu_platform_app(
    db_session, test_user,
) -> None:
    """创建 adapter_type='feishu_platform_app' 渠道成功。"""
    from app.services.notification_service import create_channel

    channel = await create_channel(
        db=db_session,
        user_id=test_user.id,
        adapter_type="feishu_platform_app",
        display_name="Platform App 渠道",
        target_config={
            "app_id": "cli_test_001",
            "app_secret": "secret_value",
            "receive_id": "bg12345",
            "receive_id_type": "user_id",
        },
    )
    assert channel.status == "pending"
    assert channel.adapter_type == "feishu_platform_app"
    assert channel.user_id == test_user.id


# ============================================================
# 测试 6: migration 在有 feishu_webhook 行时主动失败
# ============================================================


def test_migration_fails_when_feishu_webhook_rows_exist() -> None:
    """当 notification_channels 存在 feishu_webhook 行时，migration upgrade 应 raise RuntimeError。"""
    import importlib.util
    from pathlib import Path

    # 动态加载 migration 模块（避免 alembic 上下文依赖）
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic" / "versions" / "055_feishu_platform_app_only.py"
    )
    if not migration_path.exists():
        pytest.skip(f"migration 文件尚未创建: {migration_path}")

    spec = importlib.util.spec_from_file_location(
        "migration_055", migration_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # mock op.get_bind() 返回有 feishu_webhook 行的连接
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 3  # 3 条 feishu_webhook 记录
    mock_conn.execute.return_value = mock_result

    with patch("alembic.op.get_bind", return_value=mock_conn):
        with pytest.raises(RuntimeError, match="feishu_webhook"):
            module.upgrade()


def test_migration_passes_when_no_feishu_webhook_rows() -> None:
    """当 notification_channels 无 feishu_webhook 行时，migration upgrade 应成功。"""
    import importlib.util
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic" / "versions" / "055_feishu_platform_app_only.py"
    )
    if not migration_path.exists():
        pytest.skip(f"migration 文件尚未创建: {migration_path}")

    spec = importlib.util.spec_from_file_location(
        "migration_055_pass", migration_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # mock op.get_bind() 返回无 feishu_webhook 行的连接
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 0  # 0 条 feishu_webhook 记录
    mock_conn.execute.return_value = mock_result

    with patch("alembic.op.get_bind", return_value=mock_conn), \
         patch("alembic.op.create_check_constraint"), \
         patch("alembic.op.drop_index"), \
         patch("alembic.op.create_index"):
        # upgrade 不应抛异常
        module.upgrade()
        # 应创建 CHECK 约束
        assert mock_conn.execute.call_count >= 1


# ============================================================
# 额外验证: FeishuPlatformAppAdapter 仍可正常注册
# ============================================================


def test_feishu_platform_app_adapter_registered() -> None:
    """FeishuPlatformAppAdapter 应在注册表中且 feishu_webhook 不在。"""
    from app.services.channel_adapter import list_supported_adapters

    adapters = list_supported_adapters()
    assert "feishu_platform_app" in adapters
    assert "feishu_webhook" not in adapters


def test_sensitive_fields_only_app_secret() -> None:
    """_SENSITIVE_FIELDS 应仅包含 app_secret（sign_secret 不再需要）。"""
    from app.services.notification_service import _SENSITIVE_FIELDS

    assert _SENSITIVE_FIELDS == {"app_secret"}
    assert "sign_secret" not in _SENSITIVE_FIELDS


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
