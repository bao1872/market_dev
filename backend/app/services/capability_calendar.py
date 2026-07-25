"""日历月计算 - V2.1 邀请码授权期限唯一纯函数。

PRD §6.3 期限语义：
- 使用日历月运算（非 N×30 天）
- 2026-07-25 10:00 + 1 月 = 2026-08-25 10:00
- 2026-01-31 + 1 月 = 2026-02-28（月末按实际天数收缩）
- 闰年按实际月末处理（2026-02-29 不存在 → 2026-02-28；2028-02-29 存在）
- 使用 timezone-aware 时间
- 业务月运算以 Asia/Shanghai 计算，再以 UTC 入库
- 有效区间 [starts_at, expires_at)，到达 expires_at 即失效

设计要点：
- 唯一纯函数，禁止在 API/Service 中重复实现日期逻辑
- 使用 dateutil.relativedelta 处理日历月（正确处理月末/闰年）
- 入参可以是 naive 或 aware datetime；naive 视为 Asia/Shanghai
- 返回 timezone-aware UTC datetime（便于 DB 存储）
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

#: 业务时区（PRD §6.3）
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def add_calendar_months_asiashanghai(
    base: datetime,
    months: int,
) -> datetime:
    """在 base 上增加 N 个日历月，返回 expires_at（UTC timezone-aware）。

    PRD §6.3 规则：
    - 业务月运算以 Asia/Shanghai 计算
    - 月末按实际天数收缩（2026-01-31 + 1 月 = 2026-02-28）
    - 闰年按实际月末处理
    - 返回 UTC datetime 便于 DB 存储

    Args:
        base: 基准时间（naive 视为 Asia/Shanghai；aware 转换为 Asia/Shanghai）
        months: 增加的月数（必须 > 0）

    Returns:
        expires_at（UTC timezone-aware datetime）

    Raises:
        ValueError: months <= 0

    Examples:
        >>> add_calendar_months_asiashanghai(
        ...     datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc), 1
        ... ).astimezone(SHANGHAI_TZ)
        datetime(2026, 8, 25, 18, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        >>> # 2026-01-31 18:00 Asia/Shanghai + 1 月 = 2026-02-28 18:00 Asia/Shanghai
        >>> # （2 月无 31 日，按月末收缩）
    """
    if months <= 0:
        raise ValueError(f"months 必须为正整数，当前={months}")

    # 统一转换为 Asia/Shanghai timezone-aware
    if base.tzinfo is None:
        shanghai_base = base.replace(tzinfo=SHANGHAI_TZ)
    else:
        shanghai_base = base.astimezone(SHANGHAI_TZ)

    # 日历月加法（relativedelta 自动处理月末/闰年）
    shanghai_expires = shanghai_base + relativedelta(months=months)

    # 转换为 UTC 返回（DB 存储）
    return shanghai_expires.astimezone(UTC)


if __name__ == "__main__":
    # 自测：覆盖 PRD §6.3 边界场景
    # 1. 普通月份：2026-07-25 10:00 Asia/Shanghai + 1 月 = 2026-08-25 10:00 Asia/Shanghai
    base1 = datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ)
    exp1 = add_calendar_months_asiashanghai(base1, 1)
    assert exp1.astimezone(SHANGHAI_TZ) == datetime(2026, 8, 25, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"普通月份失败: {exp1}"

    # 2. 月末：2026-01-31 10:00 + 1 月 = 2026-02-28 10:00（2 月无 31 日）
    base2 = datetime(2026, 1, 31, 10, 0, tzinfo=SHANGHAI_TZ)
    exp2 = add_calendar_months_asiashanghai(base2, 1)
    assert exp2.astimezone(SHANGHAI_TZ) == datetime(2026, 2, 28, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"月末失败: {exp2}"

    # 3. 闰年：2028-01-31 + 1 月 = 2028-02-29（2028 是闰年）
    base3 = datetime(2028, 1, 31, 10, 0, tzinfo=SHANGHAI_TZ)
    exp3 = add_calendar_months_asiashanghai(base3, 1)
    assert exp3.astimezone(SHANGHAI_TZ) == datetime(2028, 2, 29, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"闰年失败: {exp3}"

    # 4. 多月：2026-07-25 + 3 月 = 2026-10-25
    base4 = datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ)
    exp4 = add_calendar_months_asiashanghai(base4, 3)
    assert exp4.astimezone(SHANGHAI_TZ) == datetime(2026, 10, 25, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"多月失败: {exp4}"

    # 5. UTC 输入：2026-07-25 02:00 UTC = 2026-07-25 10:00 SHA + 1 月 = 2026-08-25 10:00 SHA = 2026-08-25 02:00 UTC
    base5 = datetime(2026, 7, 25, 2, 0, tzinfo=UTC)
    exp5 = add_calendar_months_asiashanghai(base5, 1)
    assert exp5 == datetime(2026, 8, 25, 2, 0, tzinfo=UTC), f"UTC 输入失败: {exp5}"

    # 6. naive 输入：视为 Asia/Shanghai
    base6 = datetime(2026, 7, 25, 10, 0)  # naive
    exp6 = add_calendar_months_asiashanghai(base6, 1)
    assert exp6.astimezone(SHANGHAI_TZ) == datetime(2026, 8, 25, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"naive 输入失败: {exp6}"

    # 7. months <= 0 拒绝
    try:
        add_calendar_months_asiashanghai(base1, 0)
        raise AssertionError("months=0 应拒绝")
    except ValueError:
        pass
    try:
        add_calendar_months_asiashanghai(base1, -1)
        raise AssertionError("months=-1 应拒绝")
    except ValueError:
        pass

    # 8. 月末 + 多月：2026-01-31 + 2 月 = 2026-03-31
    base8 = datetime(2026, 1, 31, 10, 0, tzinfo=SHANGHAI_TZ)
    exp8 = add_calendar_months_asiashanghai(base8, 2)
    assert exp8.astimezone(SHANGHAI_TZ) == datetime(2026, 3, 31, 10, 0, tzinfo=SHANGHAI_TZ), \
        f"月末+多月失败: {exp8}"

    print("add_calendar_months_asiashanghai 8 项自测全部通过")
