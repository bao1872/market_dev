"""自助修改密码（POST /v1/auth/change-password）— 纯单元测试（不连接数据库）。

背景（USER-FIX-3 / TASK B）：
    当前登录用户输入「当前密码 + 新密码」自助修改；此前系统只有管理员
    ``POST /v1/admin/users/{user_id}/reset-password``，普通用户无任何入口。

合同：
    ChangePasswordRequest(current_password, new_password 8-128)
      → verify_password(current_password, user.password_hash)   # 不匹配 → 401
      → 拒绝 new == current                                      # → 400
      → get_password_hash(new_password)                          # canonical bcrypt owner
      → User.password_hash / User.updated_at
      → commit

安全约束：不返回、不记录任何密码或哈希；错误信息不泄露内部细节。

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_change_password.py -v
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.auth import change_password
from app.core.security import get_password_hash, verify_password
from app.schemas.user import ChangePasswordRequest

pytestmark = pytest.mark.pure_unit

_OLD_PASSWORD = "old-password-123"
_NEW_PASSWORD = "new-password-456"


class _FakeDB:
    """最小 AsyncSession 替身，只记录 commit 调用。"""

    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


def _make_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="member@example.com",
        password_hash=get_password_hash(_OLD_PASSWORD),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        # 授权相关数据：断言本端点绝不改动它们
        status="active",
        _roles=["member"],
    )


async def _run_change(
    user: SimpleNamespace,
    *,
    current_password: str = _OLD_PASSWORD,
    new_password: str = _NEW_PASSWORD,
) -> _FakeDB:
    db = _FakeDB()
    await change_password(
        payload=ChangePasswordRequest(
            current_password=current_password,
            new_password=new_password,
        ),
        current_user=user,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
    )
    return db


# =============================================================================
# 成功路径
# =============================================================================


async def test_change_password_success_commits() -> None:
    """正确当前密码 → 成功写入并 commit。"""
    user = _make_user()
    db = await _run_change(user)
    assert db.commit_count == 1


async def test_new_password_verifies_and_old_fails() -> None:
    """修改后：新密码可验证通过，旧密码不再有效。"""
    user = _make_user()
    await _run_change(user)
    assert verify_password(_NEW_PASSWORD, user.password_hash) is True
    assert verify_password(_OLD_PASSWORD, user.password_hash) is False


async def test_password_hash_changes() -> None:
    """password_hash 必须实际改变。"""
    user = _make_user()
    before = user.password_hash
    await _run_change(user)
    assert user.password_hash != before


async def test_uses_canonical_bcrypt_owner() -> None:
    """必须复用 canonical bcrypt owner（哈希形如 $2b$…），禁止另写一套。"""
    user = _make_user()
    await _run_change(user)
    assert user.password_hash.startswith("$2b$")


async def test_updated_at_refreshed() -> None:
    """updated_at 必须刷新（User 模型无 onupdate，需显式写入）。"""
    user = _make_user()
    before = user.updated_at
    await _run_change(user)
    assert user.updated_at > before


# =============================================================================
# 失败路径
# =============================================================================


async def test_wrong_current_password_rejected_and_original_still_valid() -> None:
    """错误当前密码 → 401；且原密码仍然可登录（hash 未被改动，无 commit）。"""
    user = _make_user()
    before_hash = user.password_hash

    with pytest.raises(HTTPException) as ei:
        await _run_change(user, current_password="definitely-wrong-password")

    assert ei.value.status_code == 401
    assert user.password_hash == before_hash
    assert verify_password(_OLD_PASSWORD, user.password_hash) is True
    assert verify_password(_NEW_PASSWORD, user.password_hash) is False


async def test_wrong_current_password_does_not_commit() -> None:
    """失败路径不得 commit（禁止部分写入）。"""
    user = _make_user()
    db = _FakeDB()
    with pytest.raises(HTTPException):
        await change_password(
            payload=ChangePasswordRequest(
                current_password="wrong-password-xxx",
                new_password=_NEW_PASSWORD,
            ),
            current_user=user,  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
        )
    assert db.commit_count == 0


async def test_new_password_equal_to_current_rejected() -> None:
    """新密码与当前密码相同 → 400，且不 commit。"""
    user = _make_user()
    before_hash = user.password_hash
    db = _FakeDB()

    with pytest.raises(HTTPException) as ei:
        await change_password(
            payload=ChangePasswordRequest(
                current_password=_OLD_PASSWORD,
                new_password=_OLD_PASSWORD,
            ),
            current_user=user,  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
        )

    assert ei.value.status_code == 400
    assert user.password_hash == before_hash
    assert db.commit_count == 0


async def test_error_detail_does_not_leak_hash() -> None:
    """错误信息不得包含密码哈希或明文。"""
    user = _make_user()
    with pytest.raises(HTTPException) as ei:
        await _run_change(user, current_password="wrong-password-xxx")
    detail = str(ei.value.detail)
    assert "$2b$" not in detail
    assert _OLD_PASSWORD not in detail
    assert "wrong-password-xxx" not in detail


# =============================================================================
# 密码策略（与注册/创建统一：8-128）
# =============================================================================


def test_new_password_too_short_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password=_OLD_PASSWORD, new_password="short7")


def test_new_password_too_long_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password=_OLD_PASSWORD, new_password="x" * 129)


def test_new_password_boundary_lengths_accepted() -> None:
    """8 与 128 为合法边界。"""
    ChangePasswordRequest(current_password=_OLD_PASSWORD, new_password="a" * 8)
    ChangePasswordRequest(current_password=_OLD_PASSWORD, new_password="a" * 128)


# =============================================================================
# 授权数据不变（invariants）
# =============================================================================


async def test_authorization_state_untouched() -> None:
    """修改密码不得改动 status / 角色等授权数据。"""
    user = _make_user()
    status_before = user.status
    roles_before = list(user._roles)

    await _run_change(user)

    assert user.status == status_before
    assert list(user._roles) == roles_before


async def test_only_password_and_updated_at_mutated() -> None:
    """被修改的字段只有 password_hash 与 updated_at（无其它副作用）。"""
    user = _make_user()
    snapshot = {
        "id": user.id,
        "email": user.email,
        "status": user.status,
        "_roles": list(user._roles),
    }

    await _run_change(user)

    assert user.id == snapshot["id"]
    assert user.email == snapshot["email"]
    assert user.status == snapshot["status"]
    assert list(user._roles) == snapshot["_roles"]
