"""邀请码注册并发安全测试（Task 4.2 - 悲观锁）。

验证 register_with_invite_code / renew_with_invite_code 使用 SELECT ... FOR UPDATE
悲观锁，防止并发注册同一邀请码导致一码多用（创建两个用户）。

测试内容：
1. register_with_invite_code 的 InviteCode 查询包含 FOR UPDATE（TDD RED - 验证修复存在）
2. renew_with_invite_code 的 InviteCode 查询包含 FOR UPDATE（TDD RED - 验证修复存在）
3. 顺序注册同一邀请码，第二次失败（回归测试，确保行为正确）
4. 并发注册同一邀请码，只有一个成功（并发测试，使用双独立 session）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- SQL 验证：包装 db.execute 捕获语句，编译后检查 FOR UPDATE 关键字
- 并发测试：用 TestAsyncSessionLocal 创建两个独立 session，asyncio.gather 并发执行
- SQLite 不支持 FOR UPDATE，测试必须在 PostgreSQL 下运行
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
    renew_with_invite_code,
)


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（用于生成邀请码）。"""
    now = datetime.now(UTC)
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    await _ensure_role(db, "user")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


def _is_invite_code_select_with_for_update(stmt: object) -> bool:
    """判断语句是否为 InviteCode 表的 SELECT 且包含 FOR UPDATE。

    通过编译为 PostgreSQL 方言检查 SQL 文本，避免依赖 SQLAlchemy 内部属性。
    """
    try:
        compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    except Exception:
        return False
    sql = str(compiled).upper()
    # 必须同时满足：是 SELECT、查 invite_codes 表、含 FOR UPDATE
    return "SELECT" in sql and "INVITE_CODES" in sql and "FOR UPDATE" in sql


# ============================================================
# TDD RED：验证 register_with_invite_code 使用 FOR UPDATE 悲观锁
# ============================================================


@pytest.mark.asyncio
async def test_register_with_invite_code_uses_pessimistic_lock(db_session: AsyncSession) -> None:
    """register_with_invite_code 的 InviteCode 查询必须使用 FOR UPDATE 悲观锁。

    [Concurrency] - 描述: 防止两个并发请求同时读到 status=unused 导致一码多用
    """
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    await db_session.flush()
    raw_code = results[0][1]

    # 包装 db.execute 捕获所有传入的语句
    captured: list[object] = []
    original_execute = db_session.execute

    async def capture_execute(stmt: object, *args: object, **kwargs: object) -> object:
        captured.append(stmt)
        return await original_execute(stmt, *args, **kwargs)  # type: ignore[arg-type]

    db_session.execute = capture_execute  # type: ignore[assignment]

    email = f"lock_{uuid.uuid4().hex[:8]}@test.com"
    await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=raw_code,
    )
    await db_session.flush()

    db_session.execute = original_execute  # type: ignore[assignment]

    invite_selects_with_lock = [s for s in captured if _is_invite_code_select_with_for_update(s)]
    assert len(invite_selects_with_lock) >= 1, (
        "register_with_invite_code 的 InviteCode 查询未使用 FOR UPDATE 悲观锁，"
        f"捕获到的语句: {[str(s.compile(dialect=postgresql.dialect())) for s in captured if _is_invite_code_select(s)]}"
    )


def _is_invite_code_select(stmt: object) -> bool:
    """判断语句是否为 InviteCode 表的 SELECT（不检查 FOR UPDATE）。"""
    try:
        compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    except Exception:
        return False
    sql = str(compiled).upper()
    return "SELECT" in sql and "INVITE_CODES" in sql


# ============================================================
# TDD RED：验证 renew_with_invite_code 使用 FOR UPDATE 悲观锁
# ============================================================


@pytest.mark.asyncio
async def test_renew_with_invite_code_uses_pessimistic_lock(db_session: AsyncSession) -> None:
    """renew_with_invite_code 的 InviteCode 查询必须使用 FOR UPDATE 悲观锁。

    [Concurrency] - 描述: 续期场景同样需要防止并发兑换同一邀请码
    """
    admin = await _create_admin(db_session)
    reg_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    renew_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    await db_session.flush()

    email = f"renewlock_{uuid.uuid4().hex[:8]}@test.com"
    user, _ = await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=reg_results[0][1],
    )
    await db_session.flush()

    # 包装 db.execute 捕获 renew 调用中的语句
    captured: list[object] = []
    original_execute = db_session.execute

    async def capture_execute(stmt: object, *args: object, **kwargs: object) -> object:
        captured.append(stmt)
        return await original_execute(stmt, *args, **kwargs)  # type: ignore[arg-type]

    db_session.execute = capture_execute  # type: ignore[assignment]

    await renew_with_invite_code(
        db=db_session, user_id=user.id,
        raw_invite_code=renew_results[0][1],
    )
    await db_session.flush()

    db_session.execute = original_execute  # type: ignore[assignment]

    invite_selects_with_lock = [s for s in captured if _is_invite_code_select_with_for_update(s)]
    assert len(invite_selects_with_lock) >= 1, (
        "renew_with_invite_code 的 InviteCode 查询未使用 FOR UPDATE 悲观锁，"
        f"捕获到的语句: {[str(s.compile(dialect=postgresql.dialect())) for s in captured if _is_invite_code_select(s)]}"
    )


# ============================================================
# 回归测试：顺序注册同一邀请码，第二次失败
# ============================================================


@pytest.mark.asyncio
async def test_sequential_registration_second_fails(db_session: AsyncSession) -> None:
    """顺序注册同一邀请码，第二次必须失败（邀请码已被使用）。

    回归测试：确保悲观锁修复不破坏正常顺序行为。
    """
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    await db_session.flush()
    raw_code = results[0][1]

    email1 = f"seq1_{uuid.uuid4().hex[:8]}@test.com"
    await register_with_invite_code(
        db=db_session, email=email1, password="password-12345",
        raw_invite_code=raw_code,
    )
    await db_session.flush()

    email2 = f"seq2_{uuid.uuid4().hex[:8]}@test.com"
    with pytest.raises(ValueError, match="邀请码已被使用"):
        await register_with_invite_code(
            db=db_session, email=email2, password="password-12345",
            raw_invite_code=raw_code,
        )


# ============================================================
# 并发测试：并发注册同一邀请码，只有一个成功
# ============================================================


@pytest.mark.asyncio
async def test_concurrent_registration_only_one_succeeds() -> None:
    """并发注册同一邀请码，只有一个成功，另一个抛出 ValueError。

    [Concurrency] - 描述: 两个独立 session + asyncio.gather 并发注册同一邀请码，
    FOR UPDATE 行锁确保第二个请求阻塞直到第一个提交，然后读到 status=used 失败。
    无悲观锁时两个请求可能都读到 unused 并都成功（漏洞）。

    注意：不使用 conftest 的 db_session fixture（其 begin_nested + dispose 不适合并发），
    直接用 TestAsyncSessionLocal 创建独立 session，手动管理事务与清理。
    """
    from tests.conftest import TestAsyncSessionLocal

    setup_session = TestAsyncSessionLocal()
    admin_email: str | None = None
    try:
        admin = await _create_admin(setup_session)
        admin_email = admin.email
        results = await generate_invite_codes(
            db=setup_session, count=1, created_by=admin.id,
            plan_code="observe_20", grant_months=1,
        )
        await setup_session.commit()
        raw_code = results[0][1]
    finally:
        await setup_session.close()

    email_a = f"conc_a_{uuid.uuid4().hex[:8]}@test.com"
    email_b = f"conc_b_{uuid.uuid4().hex[:8]}@test.com"

    async def register_and_commit(session: AsyncSession, email: str) -> tuple[User, object]:
        """注册并提交事务（释放 FOR UPDATE 锁）。"""
        try:
            result = await register_with_invite_code(
                db=session, email=email, password="password-12345",
                raw_invite_code=raw_code,
            )
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise

    session_a = TestAsyncSessionLocal()
    session_b = TestAsyncSessionLocal()
    results_concurrent: list[object] = []
    try:
        results_concurrent = await asyncio.gather(
            register_and_commit(session_a, email_a),
            register_and_commit(session_b, email_b),
            return_exceptions=True,
        )
    finally:
        await session_a.close()
        await session_b.close()

    successes = [r for r in results_concurrent if not isinstance(r, Exception)]
    failures = [r for r in results_concurrent if isinstance(r, Exception)]

    assert len(successes) == 1, (
        f"预期恰好 1 个成功，实际 {len(successes)} 个成功。"
        f"结果: {results_concurrent}"
    )
    assert len(failures) == 1, (
        f"预期恰好 1 个失败，实际 {len(failures)} 个失败。"
        f"结果: {results_concurrent}"
    )
    assert isinstance(failures[0], ValueError), (
        f"失败的异常类型应为 ValueError，实际为 {type(failures[0]).__name__}: {failures[0]}"
    )
    assert "邀请码已被使用" in str(failures[0]), (
        f"失败异常应包含'邀请码已被使用'，实际: {failures[0]}"
    )

    # 清理：按 FK 依赖顺序删除测试数据，避免污染测试库
    # 顺序：invite_codes（解除 used_by/created_by FK）→ users（级联 subscriptions/user_roles/invite_redemptions）
    from app.models.membership import InviteCode
    from app.services.subscription_service import hash_invite_code

    cleanup_session = TestAsyncSessionLocal()
    try:
        # 1. 删除邀请码（解除 used_by FK 约束，级联删除 invite_redemptions）
        code_hash = hash_invite_code(raw_code)
        invite_stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
        invite_result = await cleanup_session.execute(invite_stmt)
        invite = invite_result.scalar_one_or_none()
        if invite is not None:
            await cleanup_session.delete(invite)

        # 2. 删除用户（级联删除 subscriptions / user_roles / invite_redemptions）
        for email in (email_a, email_b, admin_email):
            if email is None:
                continue
            user_stmt = select(User).where(User.email == email)
            user_result = await cleanup_session.execute(user_stmt)
            user = user_result.scalar_one_or_none()
            if user is not None:
                await cleanup_session.delete(user)

        await cleanup_session.commit()
    finally:
        await cleanup_session.close()
        # 不 dispose engine（由 conftest session fixture 管理）


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
