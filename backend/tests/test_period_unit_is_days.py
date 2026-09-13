"""周期单位合同测试：1 单位 = 1 天（回归护栏：禁止 ×30 回漂）。

直接调用 service 层验证到期日计算，不依赖外部 HTTP API。

背景：原实现中“1 单位 = 30 天”（grant_months × 30）。现统一为“1 单位 = 1 天”
（grant_days = 有效天数）。历史邀请码若仍带 grant_months 字段，Redeem 路径按
×30 天兼容旧意图；新邀请码/订阅/续期一律按 grant_days 天解释。
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.subscription import Subscription
from app.services.subscription_service import (
    generate_invite_codes,
    grant_subscription_to_user,
    register_with_invite_code,
    renew_subscription,
)

_TOL = timedelta(seconds=5)


@pytest.mark.asyncio
async def test_invite_code_grant_days_is_literal_days(db_session, member_user) -> None:
    """邀请码 grant_days=N 应恰好约等于 N 天（不得 ×30）。"""
    for n in (1, 7, 30):
        codes = await generate_invite_codes(
            db=db_session, count=1, plan_code="observe_20",
            grant_days=n, created_by=None,
        )
        raw = codes[0][1]
        email = f"lock-inv-{n}-{uuid.uuid4().hex[:6]}@test.local"
        _user, subscription = await register_with_invite_code(
            db=db_session, email=email, password="test-pass-123",
            raw_invite_code=raw,
        )
        base = datetime.now(UTC)
        delta = subscription.expires_at - base
        # [合同] 1 单位 = 1 天
        assert timedelta(days=n) - _TOL <= delta <= timedelta(days=n) + _TOL, (
            f"邀请码 grant_days={n} 应约等于 {n} 天，实际 {delta}"
        )
        # [回归护栏] grant_days=1 绝不应得到 ~30 天
        if n == 1:
            assert delta < timedelta(days=30), "grant_days=1 被错误解释为 30 天（×30 回漂）"


@pytest.mark.asyncio
async def test_subscription_grant_days_is_literal_days(db_session, member_user) -> None:
    """订阅授予 grant_days=N 应恰好约等于 N 天（不得 ×30）。"""
    for n in (1, 7, 30):
        subscription = await grant_subscription_to_user(
            db=db_session, user_id=member_user.id, plan_code="observe_20",
            grant_days=n, actor_user_id=None,
        )
        base = datetime.now(UTC)
        delta = subscription.expires_at - base
        assert timedelta(days=n) - _TOL <= delta <= timedelta(days=n) + _TOL, (
            f"订阅授予 grant_days={n} 应约等于 {n} 天，实际 {delta}"
        )
        if n == 1:
            assert delta < timedelta(days=30), "grant_days=1 被错误解释为 30 天（×30 回漂）"


@pytest.mark.asyncio
async def test_subscription_renew_days_is_literal_days(db_session, member_user) -> None:
    """已到期订阅续期 grant_days=N 应恰好约等于 N 天（不得 ×30）。"""
    await grant_subscription_to_user(
        db=db_session, user_id=member_user.id, plan_code="observe_20",
        grant_days=1, actor_user_id=None,
    )
    sub = await db_session.scalar(
        select(Subscription).where(Subscription.user_id == member_user.id)
    )
    # 手动令其过期，触发“从当前时间重新计算”分支
    sub.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    n = 7
    _sub, _old, new_expires = await renew_subscription(
        db=db_session, user_id=member_user.id, grant_days=n, actor_user_id=None,
    )
    base = datetime.now(UTC)
    delta = new_expires - base
    assert timedelta(days=n) - _TOL <= delta <= timedelta(days=n) + _TOL, (
        f"续期 grant_days={n} 应约等于 {n} 天，实际 {delta}"
    )
    if n == 1:
        assert delta < timedelta(days=30), "grant_days=1 被错误解释为 30 天（×30 回漂）"
