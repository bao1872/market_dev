"""管理员重置用户密码 — 纯单元测试（不连接数据库）。

背景：``POST /v1/admin/users/{user_id}/reset-password`` 之前是占位端点，
标记 ``deprecated=True`` 并直接 501。本轮补上真实实现：

    ResetPasswordRequest(new_password, 8-128)
      → get_password_hash()（唯一正式 canonical 密码哈希 owner）
      → User.password_hash / User.updated_at
      → write_audit_log(action="user.reset_password")
      → commit

审计约束（重点审计项）：审计日志**不得**出现明文密码、password_hash
或密码长度等任何密码相关信息。

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_admin_reset_password.py -v
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.admin_subscription import (
    ResetPasswordRequest,
    reset_user_password,
)
from app.core.security import get_password_hash, verify_password

_OLD_PASSWORD = "old-password-123"
_NEW_PASSWORD = "new-password-456"


class _FakeDB:
    """最小 AsyncSession 替身，只记录 flush/commit 调用。"""

    def __init__(self) -> None:
        self.flush_count = 0
        self.commit_count = 0

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1


def _make_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="member@example.com",
        password_hash=get_password_hash(_OLD_PASSWORD),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _run_reset(
    monkeypatch: pytest.MonkeyPatch,
    user: SimpleNamespace,
    new_password: str = _NEW_PASSWORD,
) -> tuple[SimpleNamespace, dict, _FakeDB]:
    """调用端点，捕获 write_audit_log 的入参与最终 user 状态。"""
    import app.api.admin_subscription as mod

    db = _FakeDB()
    captured: dict = {}

    async def _fake_fetch(_db, _user_id):
        return user

    async def _fake_audit(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(mod, "_fetch_user_or_404", _fake_fetch)
    monkeypatch.setattr(mod, "write_audit_log", _fake_audit)

    actor = SimpleNamespace(id=uuid.uuid4())
    resp = await reset_user_password(
        user_id=user.id,
        payload=ResetPasswordRequest(new_password=new_password),
        db=db,  # type: ignore[arg-type]
        current_user=actor,
    )
    return resp, captured, db


# =============================================================================
# 核心行为
# =============================================================================


async def test_admin_can_reset_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理员可成功重置密码，返回 200 语义响应并 commit。"""
    user = _make_user()
    resp, _audit, db = await _run_reset(monkeypatch, user)

    assert resp.user_id == user.id
    assert db.flush_count == 1
    assert db.commit_count == 1


async def test_password_hash_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """重置后 password_hash 必须改变。"""
    user = _make_user()
    before = user.password_hash
    await _run_reset(monkeypatch, user)
    assert user.password_hash != before


async def test_new_password_verifies_and_old_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_password(新)=True；verify_password(旧)=False。"""
    user = _make_user()
    await _run_reset(monkeypatch, user)

    assert verify_password(_NEW_PASSWORD, user.password_hash) is True
    assert verify_password(_OLD_PASSWORD, user.password_hash) is False


async def test_uses_canonical_bcrypt_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """必须复用 canonical bcrypt owner（哈希形如 $2b$…），禁止另写一套。"""
    user = _make_user()
    await _run_reset(monkeypatch, user)
    assert user.password_hash.startswith("$2b$")


async def test_updated_at_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    """重置必须刷新 User.updated_at。"""
    user = _make_user()
    before = user.updated_at
    await _run_reset(monkeypatch, user)
    assert user.updated_at > before


# =============================================================================
# 审计不得泄露密码信息（重点）
# =============================================================================


async def test_audit_contains_no_password_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 payload 不得包含明文密码 / password_hash / 密码长度。"""
    user = _make_user()
    _resp, audit, _db = await _run_reset(monkeypatch, user)

    assert audit["action"] == "user.reset_password"
    assert audit["target_type"] == "user"
    assert audit["target_id"] == str(user.id)
    assert "actor_user_id" in audit

    blob = repr(audit)
    assert _NEW_PASSWORD not in blob, "审计中不得出现明文新密码"
    assert _OLD_PASSWORD not in blob, "审计中不得出现旧密码"
    assert user.password_hash not in blob, "审计中不得出现 password_hash"
    assert str(len(_NEW_PASSWORD)) not in audit.get("after_data", {}), (
        "审计中不得记录密码长度"
    )


async def test_audit_after_data_is_boolean_flag_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """after_data 只允许 '发生过一次重置' 这一事实。"""
    user = _make_user()
    _resp, audit, _db = await _run_reset(monkeypatch, user)
    assert audit["after_data"] == {"password_reset": True}


# =============================================================================
# 请求约束（与 UserRegister 一致：8-128）
# =============================================================================


def test_request_rejects_short_password() -> None:
    """< 8 字符 → pydantic 校验失败（端点层表现为 422）。"""
    with pytest.raises(ValidationError):
        ResetPasswordRequest(new_password="short1")


def test_request_accepts_boundary_lengths() -> None:
    """8 与 128 字符边界均可接受。"""
    assert ResetPasswordRequest(new_password="a" * 8).new_password == "a" * 8
    assert len(ResetPasswordRequest(new_password="a" * 128).new_password) == 128


def test_request_rejects_overlong_password() -> None:
    """129 字符 → 校验失败。"""
    with pytest.raises(ValidationError):
        ResetPasswordRequest(new_password="a" * 129)


def test_request_has_no_confirm_field() -> None:
    """确认密码只在前端校验，不作为请求字段（避免多传一份明文）。"""
    assert "confirm_password" not in ResetPasswordRequest.model_fields
