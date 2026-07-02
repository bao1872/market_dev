"""用户资格服务 - Worker 用户资格唯一事实源。

资格条件（Worker 监控 universe 投放对象）：
- User.status = 'active'
- 用户有 member 角色
- 用户无 admin 角色（admin 不进入 universe，admin 不是普通会员监控对象）
- Subscription 有效：status='active' AND starts_at <= now AND expires_at > now
  （订阅有效条件与 app.models.subscription.Subscription 文档定义一致）

设计要点：
- Worker 场景仅有 user_id（无 FastAPI 请求上下文），不能复用 access_control_service
  （后者依赖 deps._get_user_roles 与 User._roles 属性）
- 单条 SQL JOIN + EXISTS 子查询实现批量资格判定，避免逐用户 N+1 查询
- is_user_eligible / filter_eligible_recipients / list_eligible_user_ids 共用同一组
  资格条件（_member_role_exists / _admin_role_absent），保证单一事实源
- admin 排除：即使 admin 同时拥有 member 角色和有效订阅，也不进入 universe

用法：
    from app.services.eligible_user_service import (
        list_eligible_user_ids, is_user_eligible, filter_eligible_recipients,
    )
    ids = await list_eligible_user_ids(db)
    eligible = await is_user_eligible(db, user_id)
    filtered = await filter_eligible_recipients(db, [uid1, uid2])

模块自测：
    python -m app.services.eligible_user_service
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole

logger = logging.getLogger("eligible_user_service")

# 普通会员角色名（规则 7：普通用户角色为 member，不是 user）
_MEMBER_ROLE = "member"
# 管理员角色名（规则 8：admin 无套餐无 subscription，不进入监控 universe）
_ADMIN_ROLE = "admin"


def _member_role_exists():
    """构造「用户拥有 member 角色」EXISTS 子查询条件。

    返回 SQLAlchemy 布尔表达式，可用于 WHERE 子句。
    子查询自动关联外层 User.id。
    """
    return exists(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == User.id, Role.name == _MEMBER_ROLE)
    )


def _admin_role_absent():
    """构造「用户不拥有 admin 角色」NOT EXISTS 子查询条件。

    admin 不进入 universe：即使同时拥有 member 角色与有效订阅，也排除。
    """
    return ~exists(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == User.id, Role.name == _ADMIN_ROLE)
    )


async def list_eligible_user_ids(db: AsyncSession) -> list[UUID]:
    """返回所有有资格进入监控 universe 的用户 ID 列表。

    资格条件：
    - User.status = 'active'
    - 有 member 角色
    - 无 admin 角色
    - Subscription 有效（status='active' AND starts_at <= now AND expires_at > now）

    单条 SQL JOIN + EXISTS 子查询，向量化批量查询（无 Python for 循环）。

    Args:
        db: 异步会话

    Returns:
        有资格的用户 ID 列表
    """
    stmt = (
        select(User.id)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.status == "active",
            Subscription.status == "active",
            Subscription.starts_at <= func.now(),
            Subscription.expires_at > func.now(),
            _member_role_exists(),
            _admin_role_absent(),
        )
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def is_user_eligible(db: AsyncSession, user_id: UUID) -> bool:
    """检查单个用户是否有资格进入监控 universe。

    复用与 list_eligible_user_ids 相同的资格条件，仅追加 user_id 过滤，
    避免查询全部用户。

    Args:
        db: 异步会话
        user_id: 用户 ID

    Returns:
        True 表示有资格，False 表示无资格
    """
    stmt = (
        select(User.id)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.id == user_id,
            User.status == "active",
            Subscription.status == "active",
            Subscription.starts_at <= func.now(),
            Subscription.expires_at > func.now(),
            _member_role_exists(),
            _admin_role_absent(),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.first() is not None


async def filter_eligible_recipients(
    db: AsyncSession, user_ids: list[UUID],
) -> list[UUID]:
    """批量过滤有资格的用户 ID。

    单条 SQL 批量查询（user_ids IN (...)），无逐用户 for 循环。

    Args:
        db: 异步会话
        user_ids: 待过滤的用户 ID 列表

    Returns:
        有资格的用户 ID 列表（输入为空时返回空列表）
    """
    if not user_ids:
        return []

    stmt = (
        select(User.id)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.id.in_(user_ids),
            User.status == "active",
            Subscription.status == "active",
            Subscription.starts_at <= func.now(),
            Subscription.expires_at > func.now(),
            _member_role_exists(),
            _admin_role_absent(),
        )
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


if __name__ == "__main__":
    # 自测入口：验证函数签名与可调用性（不连接数据库，无副作用）
    import inspect

    for fn in (list_eligible_user_ids, is_user_eligible, filter_eligible_recipients):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        params = list(inspect.signature(fn).parameters.keys())
        print(f"{fn.__name__} params={params}")
        assert "db" in params

    # 验证角色常量（规则 7：member 不是 user）
    assert _MEMBER_ROLE == "member"
    assert _ADMIN_ROLE == "admin"

    # 验证 EXISTS 子查询构造器可调用
    member_cond = _member_role_exists()
    admin_cond = _admin_role_absent()
    print(f"member_role_exists={member_cond}")
    print(f"admin_role_absent={admin_cond}")
    print("OK")
