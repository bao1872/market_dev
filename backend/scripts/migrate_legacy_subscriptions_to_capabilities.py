"""旧订阅 → capability grants 迁移对账脚本（Phase I，PRD §15.3 过渡期迁移）。

功能：
1. 只读统计：count active subscriptions without capability grants
2. 幂等回填：为每个 active subscription 创建 3 个 capability grants
   - watchlist_management (limit_value = plan.monitor_limit)
   - market_screening (limit_value = NULL)
   - review_management (limit_value = NULL)
   - source_type='legacy_subscription', source_id=subscription.id
   - starts_at/expires_at from subscription
3. 核对：回填后统计 grant 数量，验证一致性

幂等性：UNIQUE(source_type, source_id, capability_key) 保证重复运行不创建重复 grant。
       已存在的 grant 会被跳过（INSERT ... ON CONFLICT DO NOTHING）。

用法：
  # 只读统计（dry-run，不修改数据）
  python scripts/migrate_legacy_subscriptions_to_capabilities.py --dry-run

  # 执行回填
  python scripts/migrate_legacy_subscriptions_to_capabilities.py --execute

  # 回填后核对
  python scripts/migrate_legacy_subscriptions_to_capabilities.py --verify

约束：
- 只处理 status='active' AND starts_at <= now AND expires_at > now 的订阅
- 不修改已有 grant（包括邀请码兑换的 grant）
- 不修改订阅表本身
- 不删除任何数据
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from app.db.session import async_session_factory
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.constants.capability_keys import (
    ALL_CAPABILITY_KEYS,
    SOURCE_LEGACY_SUBSCRIPTION,
    WATCHLIST_MANAGEMENT,
)
from app.models.capability_grant import UserCapabilityGrant
from app.models.plan import Plan
from app.models.subscription import Subscription


async def _get_plan_monitor_limits(db) -> dict[str, int]:
    """获取所有 active plan 的 monitor_limit 映射。"""
    stmt = select(Plan.plan_code, Plan.monitor_limit).where(Plan.status == "active")
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.all()}


async def _get_active_subscriptions_without_grants(db) -> list[Subscription]:
    """获取所有 active 订阅（status='active' AND starts_at <= now AND expires_at > now）。"""
    now = datetime.now(UTC)
    stmt = (
        select(Subscription)
        .where(
            Subscription.status == "active",
            Subscription.starts_at <= now,
            Subscription.expires_at > now,
        )
        .order_by(Subscription.created_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _count_existing_legacy_grants(db) -> int:
    """统计已存在的 legacy_subscription grant 数量。"""
    stmt = (
        select(func.count(UserCapabilityGrant.id))
        .where(UserCapabilityGrant.source_type == SOURCE_LEGACY_SUBSCRIPTION)
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def _count_grants_for_user(db, user_id: uuid.UUID) -> int:
    """统计指定用户的 legacy_subscription grant 数量。"""
    stmt = (
        select(func.count(UserCapabilityGrant.id))
        .where(
            UserCapabilityGrant.user_id == user_id,
            UserCapabilityGrant.source_type == SOURCE_LEGACY_SUBSCRIPTION,
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def dry_run() -> dict:
    """只读统计：不修改数据。"""
    async with async_session_factory() as db:
        plan_limits = await _get_plan_monitor_limits(db)
        subscriptions = await _get_active_subscriptions_without_grants(db)
        existing_grants = await _count_existing_legacy_grants(db)

        # 统计需要回填的订阅（无任何 legacy_subscription grant 的）
        users_needing_migration: list[dict] = []
        for sub in subscriptions:
            grant_count = await _count_grants_for_user(db, sub.user_id)
            if grant_count == 0:
                monitor_limit = plan_limits.get(sub.plan_code, 20)
                users_needing_migration.append({
                    "user_id": str(sub.user_id),
                    "plan_code": sub.plan_code,
                    "monitor_limit": monitor_limit,
                    "expires_at": sub.expires_at.isoformat(),
                })

        return {
            "mode": "dry-run",
            "active_subscriptions": len(subscriptions),
            "existing_legacy_grants": existing_grants,
            "users_needing_migration": len(users_needing_migration),
            "plan_limits": plan_limits,
            "sample_users": users_needing_migration[:5],
        }


async def execute_migration() -> dict:
    """执行幂等回填：为每个 active subscription 创建 3 个 capability grants。"""
    async with async_session_factory() as db:
        plan_limits = await _get_plan_monitor_limits(db)
        subscriptions = await _get_active_subscriptions_without_grants(db)

        created_count = 0
        skipped_count = 0
        errors: list[str] = []

        for sub in subscriptions:
            # 检查是否已有 legacy_subscription grant
            existing_count = await _count_grants_for_user(db, sub.user_id)
            if existing_count > 0:
                skipped_count += 1
                continue

            monitor_limit = plan_limits.get(sub.plan_code, 20)

            # 创建 3 个 grant
            for capability_key in ALL_CAPABILITY_KEYS:
                limit_value = monitor_limit if capability_key == WATCHLIST_MANAGEMENT else None
                stmt = pg_insert(UserCapabilityGrant).values(
                    user_id=sub.user_id,
                    capability_key=capability_key,
                    limit_value=limit_value,
                    source_type=SOURCE_LEGACY_SUBSCRIPTION,
                    source_id=str(sub.id),
                    starts_at=sub.starts_at,
                    expires_at=sub.expires_at,
                    revoked_at=None,
                ).on_conflict_do_nothing(
                    constraint="uq_grant_source_capability",
                )
                try:
                    result = await db.execute(stmt)
                    created_count += result.rowcount or 0
                except Exception as e:
                    errors.append(f"user={sub.user_id} cap={capability_key}: {e}")

        await db.commit()

        return {
            "mode": "execute",
            "active_subscriptions_processed": len(subscriptions),
            "grants_created": created_count,
            "users_skipped_already_migrated": skipped_count,
            "errors": errors,
        }


async def verify() -> dict:
    """回填后核对：统计 grant 数量与预期一致。"""
    async with async_session_factory() as db:
        subscriptions = await _get_active_subscriptions_without_grants(db)
        total_grants = await _count_existing_legacy_grants(db)

        # 每个用户应有 3 个 grant（三能力）
        users_with_grants: int = 0
        users_with_incomplete_grants: list[dict] = []
        for sub in subscriptions:
            grant_count = await _count_grants_for_user(db, sub.user_id)
            if grant_count == 3:
                users_with_grants += 1
            elif grant_count > 0:
                users_with_incomplete_grants.append({
                    "user_id": str(sub.user_id),
                    "grant_count": grant_count,
                    "expected": 3,
                })

        expected_grants = len(subscriptions) * 3
        return {
            "mode": "verify",
            "active_subscriptions": len(subscriptions),
            "total_legacy_grants": total_grants,
            "expected_grants": expected_grants,
            "users_with_complete_grants": users_with_grants,
            "users_with_incomplete_grants": users_with_incomplete_grants,
            "consistent": users_with_grants == len(subscriptions) and len(users_with_incomplete_grants) == 0,
        }


def main():
    parser = argparse.ArgumentParser(description="旧订阅 → capability grants 迁移对账")
    parser.add_argument("--dry-run", action="store_true", help="只读统计（不修改数据）")
    parser.add_argument("--execute", action="store_true", help="执行幂等回填")
    parser.add_argument("--verify", action="store_true", help="回填后核对")
    args = parser.parse_args()

    if not any([args.dry_run, args.execute, args.verify]):
        parser.print_help()
        sys.exit(1)

    if args.dry_run:
        result = asyncio.run(dry_run())
    elif args.execute:
        result = asyncio.run(execute_migration())
    else:
        result = asyncio.run(verify())

    print(result)


if __name__ == "__main__":
    main()
