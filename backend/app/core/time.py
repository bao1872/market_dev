"""统一时区工具模块。

设计原则：
- 数据库时间戳：UTC + TIMESTAMPTZ
- 业务日期与调度判断：Asia/Shanghai
- API和消息展示：Asia/Shanghai
- 服务器日志：Asia/Shanghai

用法：
    python -m app.core.time    # 自测：打印当前 UTC / 上海时间 / 业务日期 / ISO 字符串
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc


def now_utc() -> datetime:
    """返回当前 UTC 时间（用于数据库存储）。"""
    return datetime.now(UTC_TZ)


def now_shanghai() -> datetime:
    """返回当前上海时间（用于业务逻辑判断）。"""
    return datetime.now(SHANGHAI_TZ)


def shanghai_business_date() -> date:
    """返回当前上海业务日期（A股交易日判断用）。"""
    return now_shanghai().date()


def to_shanghai_iso(value: datetime) -> str:
    """将 datetime 转换为上海时区 ISO 字符串。

    无 tzinfo 的 datetime 视为 UTC 进行转换。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC_TZ)
    return value.astimezone(SHANGHAI_TZ).isoformat()


if __name__ == "__main__":
    print(f"now_utc: {now_utc()}")
    print(f"now_shanghai: {now_shanghai()}")
    print(f"shanghai_business_date: {shanghai_business_date()}")
    print(f"to_shanghai_iso: {to_shanghai_iso(now_utc())}")
    print("OK")
