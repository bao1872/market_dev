"""管理员代管用户通知渠道（Admin per-user Feishu）— 纯单元测试（不连数据库）。

``NotificationChannel`` 本身就是 per-user owner，用户自助链路已完整。
本轮只补"管理员代管"这一层：把作用域从 ``current_user.id`` 换成管理员指定的
``target user_id``，**然后调用完全相同的 notification_service 函数**。

本文件锁定的正是审计重点——**不得绕过现有 service / ownership owner 自己写第二套逻辑**：

1. 每个 admin 端点必须把 target user_id（而非 current_user.id）传给 service
2. 响应必须走 ``mask_target_config``（app_secret → ****末4位）
3. 渠道不属于 target user 时 fail-closed（404），不回退、不跨用户
4. ``DuplicateActiveChannelError`` → 409，沿用现有 service contract
5. 所有端点声明 ``require_roles("admin")``

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_admin_notification_channels.py -v
"""
from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.admin_notifications as mod
from app.api.admin_notifications import (
    admin_create_user_channel,
    admin_delete_user_channel,
    admin_list_user_channels,
    admin_test_user_channel,
    admin_update_user_channel,
    admin_verify_user_channel,
)
from app.schemas.notification import (
    CreateChannelRequest,
    UpdateChannelRequest,
)
from app.services.notification_service import (
    ChannelNotFoundError,
    ChannelOwnershipError,
    DuplicateActiveChannelError,
)

_TARGET_USER = uuid.uuid4()
_ADMIN_USER = uuid.uuid4()
_CHANNEL_ID = uuid.uuid4()
_OTHER_CHANNEL_ID = uuid.uuid4()

_REAL_SECRET = "abcdefgh1234"  # 末 4 位 = 1234


class _FakeDB:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


def _channel(
    *,
    channel_id: uuid.UUID = _CHANNEL_ID,
    user_id: uuid.UUID = _TARGET_USER,
    adapter_type: str = "feishu_platform_app",
    status: str = "pending",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        user_id=user_id,
        adapter_type=adapter_type,
        display_name="小Z飞书",
        target_config={
            "app_id": "cli_xxxxxx",
            "app_secret": _REAL_SECRET,
            "receive_id": "ou_xxxxxx",
            "receive_id_type": "user_id",
        },
        status=status,
        last_verified_at=None,
        last_error_code=None,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def _patch_service(monkeypatch: pytest.MonkeyPatch, name: str, fake) -> None:
    monkeypatch.setattr(mod, name, fake)


def _capture(monkeypatch: pytest.MonkeyPatch, name: str, ret):
    """替换 service 函数并记录调用参数（含位置参数）。"""
    seen: dict = {}

    async def _fake(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        if isinstance(ret, Exception):
            raise ret
        return ret

    _patch_service(monkeypatch, name, _fake)
    return seen


def _passed_user_id(seen: dict) -> uuid.UUID | None:
    """兼容位置参数与关键字参数两种调用形式。"""
    if "user_id" in seen.get("kwargs", {}):
        return seen["kwargs"]["user_id"]
    args = seen.get("args", ())
    # 约定：service 签名的第一个参数是 db，第二个是 user_id（list/verify/test）
    return args[1] if len(args) >= 2 else None


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=_ADMIN_USER)


# =============================================================================
# 1. 必须把 target user_id（而非 current_user.id）传给 service
# =============================================================================


async def test_list_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "list_user_channels", [_channel()])
    await admin_list_user_channels(
        user_id=_TARGET_USER, db=_FakeDB(), _current_user=_admin(),
    )
    assert _passed_user_id(seen) == _TARGET_USER


async def test_create_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "create_channel", _channel())
    await admin_create_user_channel(
        user_id=_TARGET_USER,
        request=CreateChannelRequest(
            adapter_type="feishu_platform_app",
            display_name="小Z飞书",
            target_config={"app_id": "cli_x", "app_secret": "s"},
        ),
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert seen["kwargs"]["user_id"] == _TARGET_USER


async def test_update_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "update_channel", _channel())
    await admin_update_user_channel(
        user_id=_TARGET_USER,
        channel_id=_CHANNEL_ID,
        request=UpdateChannelRequest(display_name="改名"),
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert seen["kwargs"]["user_id"] == _TARGET_USER
    assert seen["kwargs"]["channel_id"] == _CHANNEL_ID


async def test_delete_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "delete_channel", _channel(status="inactive"))
    await admin_delete_user_channel(
        user_id=_TARGET_USER,
        channel_id=_CHANNEL_ID,
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert seen["kwargs"]["user_id"] == _TARGET_USER


async def test_verify_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "verify_channel", _channel(status="active"))
    await admin_verify_user_channel(
        user_id=_TARGET_USER,
        channel_id=_CHANNEL_ID,
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert _passed_user_id(seen) == _TARGET_USER


async def test_test_uses_target_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.schemas.notification import DeliveryResult

    seen = _capture(
        monkeypatch,
        "test_channel",
        (_channel(status="active"), DeliveryResult(success=True)),
    )
    await admin_test_user_channel(
        user_id=_TARGET_USER,
        channel_id=_CHANNEL_ID,
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert _passed_user_id(seen) == _TARGET_USER


# =============================================================================
# 2. 响应必须脱敏（复用 mask_target_config，不重新实现）
# =============================================================================


async def test_list_response_masks_app_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, "list_user_channels", [_channel()])
    resp = await admin_list_user_channels(
        user_id=_TARGET_USER, db=_FakeDB(), _current_user=_admin(),
    )
    assert resp.items[0].target_config["app_secret"] == f"****{_REAL_SECRET[-4:]}"
    assert _REAL_SECRET not in str(resp.items[0].target_config)


async def test_create_response_masks_app_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, "create_channel", _channel())
    resp = await admin_create_user_channel(
        user_id=_TARGET_USER,
        request=CreateChannelRequest(
            adapter_type="feishu_platform_app",
            display_name="n",
            target_config={"app_secret": _REAL_SECRET},
        ),
        db=_FakeDB(),  # type: ignore[arg-type]
        current_user=_admin(),
    )
    assert resp.target_config["app_secret"] == "****1234"


# =============================================================================
# 3. 渠道不属于 target user → fail-closed（不回退、不跨用户）
# =============================================================================


async def test_verify_ownership_error_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(
        monkeypatch,
        "verify_channel",
        ChannelOwnershipError(f"无权操作渠道: channel_id={_OTHER_CHANNEL_ID}"),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_verify_user_channel(
            user_id=_TARGET_USER,
            channel_id=_OTHER_CHANNEL_ID,
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 404


async def test_test_ownership_error_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(
        monkeypatch,
        "test_channel",
        ChannelOwnershipError(f"无权操作渠道: channel_id={_OTHER_CHANNEL_ID}"),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_test_user_channel(
            user_id=_TARGET_USER,
            channel_id=_OTHER_CHANNEL_ID,
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 404


async def test_update_wrong_owner_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update_channel 内部按 user_id 做所有权校验 → ValueError → 404。"""
    _capture(monkeypatch, "update_channel", ValueError("渠道不存在或无权操作"))
    with pytest.raises(HTTPException) as exc:
        await admin_update_user_channel(
            user_id=_TARGET_USER,
            channel_id=_OTHER_CHANNEL_ID,
            request=UpdateChannelRequest(display_name="x"),
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 404


async def test_delete_wrong_owner_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, "delete_channel", ValueError("渠道不存在或无权操作"))
    with pytest.raises(HTTPException) as exc:
        await admin_delete_user_channel(
            user_id=_TARGET_USER,
            channel_id=_OTHER_CHANNEL_ID,
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 404


async def test_verify_missing_channel_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, "verify_channel", ChannelNotFoundError("渠道不存在"))
    with pytest.raises(HTTPException) as exc:
        await admin_verify_user_channel(
            user_id=_TARGET_USER,
            channel_id=_OTHER_CHANNEL_ID,
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 404


# =============================================================================
# 4. 沿用现有 service contract：DuplicateActiveChannelError → 409
# =============================================================================


async def test_create_duplicate_active_maps_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(
        monkeypatch,
        "create_channel",
        DuplicateActiveChannelError("用户已存在 active feishu_platform_app 渠道"),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_create_user_channel(
            user_id=_TARGET_USER,
            request=CreateChannelRequest(
                adapter_type="feishu_platform_app",
                display_name="n",
                target_config={},
            ),
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 409


async def test_update_duplicate_active_maps_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(
        monkeypatch,
        "update_channel",
        DuplicateActiveChannelError("不能同时拥有多条 active 飞书渠道"),
    )
    with pytest.raises(HTTPException) as exc:
        await admin_update_user_channel(
            user_id=_TARGET_USER,
            channel_id=_CHANNEL_ID,
            request=UpdateChannelRequest(display_name="x"),
            db=_FakeDB(),  # type: ignore[arg-type]
            current_user=_admin(),
        )
    assert exc.value.status_code == 409


# =============================================================================
# 5. 不得绕过 service 层 / 必须声明 admin 守卫
# =============================================================================


_MODULE_SRC = open("app/api/admin_notifications.py", encoding="utf-8").read()


def test_module_never_cruds_notification_channel_orm() -> None:
    """薄包装不得直接 CRUD NotificationChannel ORM。"""
    assert "from app.models.notification import" not in _MODULE_SRC
    assert "select(NotificationChannel" not in _MODULE_SRC
    assert "db.add(NotificationChannel" not in _MODULE_SRC
    assert "db.get(NotificationChannel" not in _MODULE_SRC


def test_module_reuses_existing_service_functions() -> None:
    """必须复用现有 notification_service 函数，不另写一套。"""
    src = _MODULE_SRC
    for fn in (
        "list_user_channels",
        "create_channel",
        "update_channel",
        "delete_channel",
        "verify_channel",
        "test_channel",
        "mask_target_config",
    ):
        assert fn in src, f"必须复用 {fn}"


@pytest.mark.parametrize(
    "fn",
    [
        admin_list_user_channels,
        admin_create_user_channel,
        admin_update_user_channel,
        admin_delete_user_channel,
        admin_verify_user_channel,
        admin_test_user_channel,
    ],
)
def test_all_endpoints_declare_admin_guard(fn) -> None:
    """所有端点必须声明 require_roles('admin') 依赖。

    ``require_roles(...)`` 返回内部闭包，repr 看不到角色名，
    因此直接检查依赖的 qualname + 源码中的调用形式。
    """
    sig = inspect.signature(fn)
    deps = [
        p.default for p in sig.parameters.values()
        if p.default is not inspect.Parameter.empty
    ]
    names = [
        getattr(getattr(d, "dependency", None), "__qualname__", "") for d in deps
    ]
    assert any("require_roles" in n for n in names), (
        f"{fn.__name__} 缺少 admin 守卫：{names}"
    )
    assert 'require_roles("admin")' in _MODULE_SRC


def test_router_prefix_is_admin() -> None:
    assert mod.router.prefix == "/v1/admin"
