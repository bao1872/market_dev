"""Mootdx 交易日历 Provider。

向 mootdx 0.11.7 的 holiday/holidays 工具发起调用，为 A 股交易日历提供判断能力：
- is_non_trading_day_by_mootdx: 判断单日期是否为非交易日
- is_trading_day_by_mootdx: 判断单日期是否为交易日
- get_historical_trading_days_by_mootdx: 获取历史交易日集合
- build_calendar_for_year: 构建全年日历
- validate_historical_calendar: 核验 holidays() 数据可用性

mootdx 函数语义（以 0.11.7 实测为准）：
- `holiday(date_string)`: 接收 YYYY-MM-DD 字符串，返回 True 表示非交易日
  （周末或官方节假日），False 表示交易日。用于 holidays() 未覆盖的未来日期。
- `holidays()`: 返回历史交易日 DataFrame（列 date/year），表示历史上真实开盘的日期，
  禁止将其当作节假日集合使用。

日期语义：
- 历史覆盖范围内（holidays() 返回的 min_date ~ max_date）：
  - 日期在 holidays() 集合中 -> OPEN + source=MOOTDX_HISTORICAL
  - 日期不在集合中（含周末、节假日） -> CLOSED + source=MOOTDX_HISTORICAL
- 超出历史覆盖范围（未来日期）：
  - holiday(date_str) 返回 False -> OPEN + source=MOOTDX_HOLIDAY
  - holiday(date_str) 返回 True -> CLOSED + source=MOOTDX_HOLIDAY

所有日期按上海业务日期语义处理；holiday() 调用必须显式传入格式化后的日期字符串，
禁止无参数调用（无参数会取系统当前时间，可能与服务端时区预期不一致）。
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from mootdx.utils.holiday import holiday, holidays

logger = logging.getLogger(__name__)

# [Calendar] - 描述: mootdx 数据源标识
MOOTDX_HOLIDAY_SOURCE = "MOOTDX_HOLIDAY"
MOOTDX_HISTORICAL_SOURCE = "MOOTDX_HISTORICAL"
MANUAL_OVERRIDE_SOURCE = "MANUAL_OVERRIDE"

# [Calendar] - 描述: 交易日历状态枚举
CALENDAR_STATUS_OPEN = "OPEN"
CALENDAR_STATUS_CLOSED = "CLOSED"
CALENDAR_STATUS_UNKNOWN = "UNKNOWN"

# [Calendar] - 描述: 历史交易日集合进程内缓存，避免重复调用 holidays()
_historical_trading_days_cache: set[date] | None = None
_historical_coverage_cache: tuple[date, date] | None = None


def _shanghai_date_string(target_date: date) -> str:
    """将 date 对象按上海业务日期语义格式化为 YYYY-MM-DD 字符串。

    所有日期按 Asia/Shanghai 业务日期理解，与 app.core.time 模块保持一致；
    仅做字符串格式化，不涉及时区转换（date 对象本身不带时区）。
    """
    return target_date.strftime("%Y-%m-%d")


def _load_historical_trading_days() -> tuple[set[date], date, date]:
    """加载 holidays() 历史交易日数据并返回覆盖范围。

    Returns:
        (trading_days_set, min_date, max_date)
    """
    global _historical_trading_days_cache, _historical_coverage_cache

    if _historical_trading_days_cache is not None and _historical_coverage_cache is not None:
        return _historical_trading_days_cache, *_historical_coverage_cache

    df = holidays()
    if df.empty or "date" not in df.columns:
        # [Calendar] - 描述: 数据异常时返回空集合与无效覆盖范围，触发上层回退到 holiday()
        return set(), date.max, date.min

    trading_days: set[date] = set(df["date"])
    min_date = min(trading_days)
    max_date = max(trading_days)

    _historical_trading_days_cache = trading_days
    _historical_coverage_cache = (min_date, max_date)
    return trading_days, min_date, max_date


def _clear_historical_cache() -> None:
    """清除历史交易日缓存（主要用于测试）。"""
    global _historical_trading_days_cache, _historical_coverage_cache
    _historical_trading_days_cache = None
    _historical_coverage_cache = None


def is_non_trading_day_by_mootdx(target_date: date) -> bool:
    """判断目标日期是否为非交易日（周末或节假日）。

    Args:
        target_date: 待判断的日期（上海业务日期语义）

    Returns:
        True 表示非交易日，False 表示交易日

    Note:
        - 周六、周日始终视为非交易日。
        - 在 holidays() 覆盖范围内的工作日：若不在历史交易日集合中则为节假日。
        - 超出 holidays() 覆盖范围时，回退到 mootdx.holiday(date_string)；
          若其返回 None，抛出 ValueError 并附带原始日期上下文。
    """
    if target_date.weekday() >= 5:
        return True

    historical_days, min_date, max_date = _load_historical_trading_days()

    if min_date <= target_date <= max_date:
        # [Calendar] - 描述: 历史覆盖范围内，以 holidays() 交易日集合为准
        return target_date not in historical_days

    # [Calendar] - 描述: 超出 holidays() 覆盖范围，回退到 mootdx.holiday()
    date_str = _shanghai_date_string(target_date)
    result = holiday(date_str)
    if result is None:
        raise ValueError(
            f"mootdx.holiday({date_str!r}) 返回 None，无法判断交易日状态"
        )
    return bool(result)


def is_trading_day_by_mootdx(target_date: date) -> bool:
    """判断目标日期是否为交易日。

    Args:
        target_date: 待判断的日期（上海业务日期语义）

    Returns:
        True 表示交易日，False 表示非交易日
    """
    return not is_non_trading_day_by_mootdx(target_date)


def get_historical_trading_days_by_mootdx() -> set[date]:
    """从 mootdx holidays() 获取历史交易日集合。

    Returns:
        历史交易日 date 对象集合

    Warning:
        holidays() 返回的是历史交易日 DataFrame（列 date/year），
        不是节假日集合，禁止当作节假日集合使用。
    """
    days, _, _ = _load_historical_trading_days()
    return days


def build_calendar_for_year(year: int) -> pd.DataFrame:
    """构建指定年份的完整交易日历。

    返回 DataFrame，列：
    - date: 日期
    - is_trading_day: 是否为交易日
    - status: OPEN / CLOSED / UNKNOWN
    - source: MOOTDX_HOLIDAY / MOOTDX_HISTORICAL / MANUAL_OVERRIDE

    状态规则：
    - 日期在 holidays() 历史交易日集合中 -> OPEN + source=MOOTDX_HISTORICAL
    - 日期为周末/节假日且仍在 holidays() 覆盖范围内 -> CLOSED + source=MOOTDX_HISTORICAL
    - 日期超出 holidays() 覆盖范围：
      - holiday(date_str) 返回 False -> OPEN + source=MOOTDX_HOLIDAY
      - holiday(date_str) 返回 True -> CLOSED + source=MOOTDX_HOLIDAY

    Args:
        year: 年份，如 2026

    Returns:
        全年日历 DataFrame
    """
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    all_dates = pd.date_range(start=start, end=end, freq="D").date
    historical_days, min_date, max_date = _load_historical_trading_days()

    records: list[dict] = []
    for d in all_dates:
        in_historical_range = min_date <= d <= max_date
        if in_historical_range:
            if d in historical_days:
                is_trading = True
                status = CALENDAR_STATUS_OPEN
            else:
                is_trading = False
                status = CALENDAR_STATUS_CLOSED
            source = MOOTDX_HISTORICAL_SOURCE
        else:
            # [Calendar] - 描述: 超出 holidays() 覆盖范围，使用 holiday() 在线判断
            date_str = _shanghai_date_string(d)
            try:
                is_holiday = holiday(date_str)
            except Exception as exc:
                raise ValueError(
                    f"mootdx.holiday({date_str!r}) 调用失败：{exc}"
                ) from exc
            if is_holiday is None:
                is_trading = False
                status = CALENDAR_STATUS_UNKNOWN
            else:
                is_trading = not bool(is_holiday)
                status = CALENDAR_STATUS_OPEN if is_trading else CALENDAR_STATUS_CLOSED
            source = MOOTDX_HOLIDAY_SOURCE

        records.append(
            {
                "date": d,
                "is_trading_day": is_trading,
                "status": status,
                "source": source,
            }
        )

    return pd.DataFrame(records)


def validate_historical_calendar(year: int) -> dict:
    """核验 mootdx holidays() 历史交易日数据覆盖、字段、非空。

    Args:
        year: 待核验的年份

    Returns:
        dict，包含：
        - ok: bool，是否通过校验
        - message: str，校验结果说明
        - count: int，该年份历史交易日条数
        - columns: list，DataFrame 列名
        - coverage: tuple(date, date)，历史数据覆盖的日期范围
    """
    df = holidays()

    errors: list[str] = []
    if df.empty:
        errors.append("holidays() 返回空 DataFrame")
    if "date" not in df.columns:
        errors.append("holidays() 返回 DataFrame 缺少 date 列")
    if "year" not in df.columns:
        errors.append("holidays() 返回 DataFrame 缺少 year 列")

    year_count = 0
    coverage: tuple[date, date] | None = None
    if not df.empty and "date" in df.columns:
        all_dates = df["date"]
        coverage = (min(all_dates), max(all_dates))
        if "year" in df.columns:
            min_year = int(df["year"].min())
            max_year = int(df["year"].max())
            if year < min_year:
                errors.append(f"holidays() 未包含 {year} 年之前数据")
            elif year > max_year:
                # [Calendar] - 描述: 超出 holidays() 覆盖的未来年份使用 holiday() 在线判断
                year_count = 0
            else:
                year_count = int((df["year"] == year).sum())
                if year_count == 0:
                    errors.append(f"holidays() 未包含 {year} 年数据")

    return {
        "ok": len(errors) == 0,
        "message": "; ".join(errors) if errors else "ok",
        "count": year_count,
        "columns": list(df.columns) if not df.empty else [],
        "coverage": coverage,
    }


if __name__ == "__main__":
    # [Calendar] - 自测入口：验证 mootdx provider 核心函数（不写库表）
    print("=== mootdx_calendar_provider 自测 ===")

    test_cases = [
        (date(2026, 6, 29), "周一/今日（交易日预期）"),
        (date(2026, 6, 27), "周六（非交易日预期）"),
        (date(2026, 6, 28), "周日（非交易日预期）"),
        (date(2026, 1, 1), "元旦（节假日/非交易日预期）"),
        (date(2026, 2, 17), "春节（节假日/非交易日预期）"),
        (date(2026, 6, 26), "历史交易日预期"),
    ]

    for d, desc in test_cases:
        try:
            non_trading = is_non_trading_day_by_mootdx(d)
            trading = is_trading_day_by_mootdx(d)
            print(f"{d} ({desc}): non_trading={non_trading}, trading={trading}")
        except Exception as exc:
            print(f"{d} ({desc}): ERROR {exc}")

    print("\n--- get_historical_trading_days_by_mootdx ---")
    hist_days = get_historical_trading_days_by_mootdx()
    print(f"历史交易日总数: {len(hist_days)}")
    sample_2026 = [d for d in hist_days if d.year == 2026]
    print(f"2026 年历史交易日数: {len(sample_2026)}")
    print(f"含 2026-06-26: {date(2026, 6, 26) in hist_days}")
    print(f"含 2026-01-01: {date(2026, 1, 1) in hist_days}")

    print("\n--- validate_historical_calendar(2026) ---")
    validation = validate_historical_calendar(2026)
    print(validation)

    print("\n--- build_calendar_for_year(2026) ---")
    cal_df = build_calendar_for_year(2026)
    print(f"shape={cal_df.shape}, columns={list(cal_df.columns)}")
    print("元旦前后:")
    mask = (cal_df["date"] >= date(2025, 12, 30)) & (cal_df["date"] <= date(2026, 1, 4))
    print(cal_df.loc[mask].to_string(index=False))
    print("春节期间:")
    mask2 = (cal_df["date"] >= date(2026, 2, 16)) & (cal_df["date"] <= date(2026, 2, 20))
    print(cal_df.loc[mask2].to_string(index=False))
    print(f"全年交易日数: {int(cal_df['is_trading_day'].sum())}")

    print("=== 自测结束 ===")
