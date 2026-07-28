"""邀请码 30 天周期计算测试。

验证：
1. 默认 1 周期 = 30 天
2. N 周期 = N × 30 天
3. 跨月/跨年仍按天数（不按自然月）
4. 过期边界
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.subscription_service import _compute_expires_at_from_months


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
