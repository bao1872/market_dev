"""邀请码 30 天周期计算测试（纯单元测试，不连接数据库）。

验证：
1. 默认 1 周期 = 30 天
2. N 周期 = N × 30 天
3. 跨月/跨年仍按天数（不按自然月）
4. 过期边界
5. 旧邀请码 grant_days 兼容（_compute_expires_at）
6. 非法值回退默认 30 天
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.services.subscription_service import (
    _compute_expires_at,
    _compute_expires_at_from_months,
)


@dataclass
class _MockInvite:
    """InviteCode 替身（纯单元测试，不依赖 ORM/DB）。"""

    grant_months: int | None = None
    grant_days: int | None = None


def test_default_1_period_is_30_days():
    """1 周期 = 30 天（非自然月）。"""
    base = datetime(2026, 1, 15, tzinfo=UTC)
    result = _compute_expires_at_from_months(base, 1)
    assert result == base + timedelta(days=30)
    assert result == datetime(2026, 2, 14, tzinfo=UTC)  # 1/15 + 30天 = 2/14


def test_n_periods_is_n_times_30_days():
    """N 周期 = N × 30 天。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for n in [2, 3, 6, 12]:
        result = _compute_expires_at_from_months(base, n)
        assert result == base + timedelta(days=30 * n), f"Failed for n={n}"


def test_cross_month_uses_days_not_natural_month():
    """跨月按天数计算，不按自然月。

    自然月：1/31 + 1月 = 2/28（非闰年）
    30 天周期：1/31 + 30天 = 3/2
    """
    base = datetime(2026, 1, 31, tzinfo=UTC)
    result = _compute_expires_at_from_months(base, 1)
    # 自然月会是 2/28，但 30 天周期是 3/2
    assert result == datetime(2026, 3, 2, tzinfo=UTC)
    assert result != datetime(2026, 2, 28, tzinfo=UTC)


def test_cross_year_uses_days():
    """跨年仍按天数计算。"""
    base = datetime(2026, 12, 20, tzinfo=UTC)
    result = _compute_expires_at_from_months(base, 1)
    assert result == datetime(2027, 1, 19, tzinfo=UTC)  # 12/20 + 30天


def test_expiration_boundary():
    """过期边界：expires_at 时刻 == now 时已过期（<= 判断）。"""
    base = datetime(2026, 7, 28, tzinfo=UTC)
    expires = _compute_expires_at_from_months(base, 1)
    # expires_at 时刻本身已过期（<= now 判断）
    assert expires <= base + timedelta(days=30)
    # 1 秒前未过期
    assert expires > base + timedelta(days=30, seconds=-1)


def test_none_grant_months_fallback_30_days():
    """grant_months=None 时回退 30 天。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    result = _compute_expires_at_from_months(base, None)
    assert result == base + timedelta(days=30)


def test_12_periods_is_360_days_not_365():
    """12 周期 = 360 天（非 365 天/自然年）。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    result = _compute_expires_at_from_months(base, 12)
    assert result == base + timedelta(days=360)
    assert result != base + timedelta(days=365)


# =============================================================================
# _compute_expires_at 兼容性测试（旧邀请码 grant_days 路径）
# =============================================================================


def test_grant_months_positive_uses_30x_months():
    """grant_months 为正数时使用 30 × grant_months 天。"""
    base = datetime(2026, 3, 1, tzinfo=UTC)
    invite = _MockInvite(grant_months=2, grant_days=45)
    result = _compute_expires_at(base, invite)
    assert result == base + timedelta(days=60)  # 2 × 30 = 60，不使用 grant_days=45


def test_grant_months_none_uses_grant_days():
    """grant_months 为空且 grant_days 为正数时使用原 grant_days。"""
    base = datetime(2026, 3, 1, tzinfo=UTC)
    invite = _MockInvite(grant_months=None, grant_days=45)
    result = _compute_expires_at(base, invite)
    assert result == base + timedelta(days=45)  # 旧邀请码 grant_days=45


def test_grant_months_zero_uses_grant_days():
    """grant_months=0（非正数）且 grant_days 为正数时使用 grant_days。"""
    base = datetime(2026, 3, 1, tzinfo=UTC)
    invite = _MockInvite(grant_months=0, grant_days=15)
    result = _compute_expires_at(base, invite)
    assert result == base + timedelta(days=15)


def test_both_none_fallback_30_days():
    """grant_months 和 grant_days 都为空时回退默认 30 天。"""
    base = datetime(2026, 3, 1, tzinfo=UTC)
    invite = _MockInvite(grant_months=None, grant_days=None)
    result = _compute_expires_at(base, invite)
    assert result == base + timedelta(days=30)


def test_both_invalid_fallback_30_days():
    """grant_months 和 grant_days 都为非正数时回退默认 30 天。"""
    base = datetime(2026, 3, 1, tzinfo=UTC)
    invite = _MockInvite(grant_months=-1, grant_days=0)
    result = _compute_expires_at(base, invite)
    assert result == base + timedelta(days=30)


def test_old_grant_days_cross_month():
    """旧邀请码 grant_days 跨月仍按天数计算。"""
    base = datetime(2026, 1, 31, tzinfo=UTC)
    invite = _MockInvite(grant_months=None, grant_days=45)
    result = _compute_expires_at(base, invite)
    # 1/31 + 45天 = 3/17
    assert result == datetime(2026, 3, 17, tzinfo=UTC)
