"""Instrument 上市生命周期 owner — listing boundary only.

CHANGE-20260816-002: Auction 120D PIT population 需要 listing boundary。

本轮只解决 "股票在历史交易日 T 是否已上市" 的**上市边界**：

- authoritative source = pytdx get_finance_info().ipo_date（YYYYMMDD int）
- 落库字段 = Instrument.listing_date（已存在，nullable Date）
- resolver 规则：stock_symbol_sql_filter AND market in ('SH','SZ')
  AND listing_date IS NOT NULL AND listing_date <= trade_date
- **明确排除** Instrument.status（operational state，非历史上市生命周期）

重要边界（必须由 ChatGPT 后续确认）：

- DELISTING_BOUNDARY_PENDING：本轮**不**实现退市边界。
  resolver 仅按 listing_date <= T 判断；若窗口内存在正式终止上市的股票，
  会导致退市后日期仍被错误包含 → 见 120-day delisting impact audit（本服务不负责）。
- SUSPENSION 仍属 listed population：status='inactive'/'suspended' 但 listing_date <= T
  仍被包含（与 missing != zero 一致）。

禁止把本服务用于：
- Auction canonicalization contract
- qfq algorithm
- Scope membership / UniverseMembership
- Review
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("instrument_lifecycle_service")

# Auction historical validation 当前覆盖的 A-share 范围（见 PRD §AU-04）。
# BJ / B-share / ETF / fund / bond 不进本轮 lifecycle owner，除非 Auction PRD 显式要求。
_AUCTION_A_SHARE_MARKETS = ("SH", "SZ")


class PytdxFinanceInfoProvider(Protocol):
    """pytdx finance-info 提供方接口（便于纯单元测试 mock）。

    实际实现为 app.core.pytdx_adapter.PytdxAdapter.get_finance_info，
    返回 dict 含 'ipo_date_raw': int | None（YYYYMMDD 原值）。

    生产 owner 是**同步**接口（PytdxAdapter.get_finance_info 是同步 I/O）；
    lifecycle service 通过 asyncio.to_thread 调用，不阻塞事件循环，
    不建立第二套 adapter（与 instrument_share_sync_service 一致）。
    """

    def get_finance_info(self, symbol: str) -> dict[str, Any] | None:
        ...


# ---------------------------------------------------------------------------
# 纯函数：ipo_date 归一化
# ---------------------------------------------------------------------------
def normalize_pytdx_ipo_date(value: Any) -> date | None:
    """将 pytdx 原始 ipo_date 归一化为 date。

    规则（严格，禁止 fallback）：
    - 有效 YYYYMMDD int（如 19910403）→ date
    - 0 / None / 空字符串 / 非数字 / malformed / 非法日历日期 → None

    禁止：
    - 1970 epoch fallback
    - today fallback
    - 首个 bar 日期 fallback
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            v = int(value)
        except (ValueError, OverflowError):
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s or not s.isdigit():
            return None
        try:
            v = int(s)
        except ValueError:
            return None
    else:
        return None

    if v <= 0:
        return None
    try:
        return date(v // 10000, (v // 100) % 100, v % 100)
    except ValueError:
        # 非法日历（如 20261345 / 20260230）
        return None


# ---------------------------------------------------------------------------
# PIT resolver — listing boundary only
# ---------------------------------------------------------------------------
def is_listed_a_share_at(
    symbol: str,
    market: str,
    listing_date: date | None,
    trade_date: date,
    *,
    markets: tuple[str, ...] = _AUCTION_A_SHARE_MARKETS,
) -> bool:
    """纯函数版 listing-boundary 判定（供单元测试与 resolver 共用）。

    规则：
    - market in markets（默认 SH/SZ，排除 BJ）
    - listing_date IS NOT NULL
    - listing_date <= trade_date
    - is_stock_symbol(symbol, market) 必须为真（排除指数/基金/ETF）

    **不**依赖 Instrument.status。
    """
    from app.services.instrument_maintenance_service import is_stock_symbol

    if market not in markets:
        return False
    if not is_stock_symbol(symbol, market):
        return False
    if listing_date is None:
        return False
    return listing_date <= trade_date


def listed_a_share_filter_at(
    trade_date: date,
    *,
    markets: tuple[str, ...] = _AUCTION_A_SHARE_MARKETS,
) -> ColumnElement[bool]:
    """构造 "在 trade_date 已上市 A-share" 的 SQL 过滤条件（仅 listing boundary）。

    规则：
    - stock_symbol_sql_filter: 排除指数/基金/ETF（含 BJ）
    - market in markets: 仅 SH/SZ（Auction 范围，排除 BJ）
    - listing_date IS NOT NULL
    - listing_date <= trade_date

    **不**包含任何 status 过滤（status 是 operational，非 lifecycle）。
    """
    return (
        stock_symbol_sql_filter(Instrument)
        & Instrument.market.in_(markets)
        & Instrument.listing_date.is_not(None)
        & (Instrument.listing_date <= trade_date)
    )


async def resolve_listed_a_share_instruments_at(
    session: AsyncSession,
    trade_date: date,
    *,
    markets: tuple[str, ...] = _AUCTION_A_SHARE_MARKETS,
) -> list[Instrument]:
    """返回 trade_date 当日已上市的 SH/SZ A-share 标的（仅 listing boundary）。

    Args:
        session: 数据库会话
        trade_date: 历史交易日 T
        markets: 限制市场（默认 SH/SZ）

    Returns:
        Instrument 列表（按 instrument_id 排序，确定性）
    """
    stmt = (
        select(Instrument)
        .where(listed_a_share_filter_at(trade_date, markets=markets))
        .order_by(Instrument.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# listing_date 同步 owner
# ---------------------------------------------------------------------------
class ListingDateSyncResult:
    """listing_date 同步结果（幂等，fail-closed 不静默覆盖）。"""

    def __init__(self) -> None:
        self.scanned = 0
        self.finance_success = 0
        self.listing_date_inserted = 0
        self.listing_date_unchanged = 0
        self.listing_date_missing = 0
        self.listing_date_conflict = 0
        self.source_error = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "finance_success": self.finance_success,
            "listing_date_inserted": self.listing_date_inserted,
            "listing_date_unchanged": self.listing_date_unchanged,
            "listing_date_missing": self.listing_date_missing,
            "listing_date_conflict": self.listing_date_conflict,
            "source_error": self.source_error,
        }


async def sync_listing_dates(
    session: AsyncSession,
    finance_provider: PytdxFinanceInfoProvider,
    *,
    markets: tuple[str, ...] = _AUCTION_A_SHARE_MARKETS,
    dry_run: bool = False,
) -> ListingDateSyncResult:
    """将 pytdx ipo_date 同步到 Instrument.listing_date。

    职责仅限：获取 authoritative listing lifecycle → normalize → persist listing_date。
    不刷新 bars、不跑 auction、不跑 Review、不建 Scope membership。

    写入语义（fail-closed，不静默覆盖）：
    - source ipo_date valid → 写 authoritative listing_date
    - source ipo_date missing (None/0/malformed) → 保持现有值，写 None 覆盖
    - existing listing_date == source → unchanged
    - existing listing_date != source（均非 None）→ LISTING_DATE_CONFLICT（报告，不覆盖）
    - existing is None + source valid → 写 source

    幂等：同一 symbol + 同一 ipo_date 重复运行无变化。

    Args:
        session: 数据库会话
        finance_provider: pytdx finance-info 提供方（生产用 PytdxAdapter）
        markets: 限制市场（默认 SH/SZ）
        dry_run: True 时只计算 proposed，不 commit

    Returns:
        ListingDateSyncResult
    """
    res = ListingDateSyncResult()

    # 仅遍历 SH/SZ 股票身份标的（security-list owner 已保证 identity）
    stmt = (
        select(Instrument)
        .where(
            stock_symbol_sql_filter(Instrument) & Instrument.market.in_(markets)
        )
        .order_by(Instrument.id)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    for inst in rows:
        res.scanned += 1
        try:
            # 生产 owner PytdxAdapter.get_finance_info 是同步 I/O；
            # 通过 asyncio.to_thread 调用，不阻塞事件循环，不建立第二套 adapter。
            info = await asyncio.to_thread(
                finance_provider.get_finance_info, inst.symbol
            )
        except Exception:  # 网络/解析失败，计入 source_error，不中止
            res.source_error += 1
            logger.warning("sync_listing_dates finance_error symbol=%s", inst.symbol)
            continue

        if not info:
            res.source_error += 1
            continue

        res.finance_success += 1
        ipo_raw = info.get("ipo_date_raw")
        norm = normalize_pytdx_ipo_date(ipo_raw)

        if norm is None:
            res.listing_date_missing += 1
            continue

        existing = inst.listing_date
        if existing is None:
            inst.listing_date = norm
            res.listing_date_inserted += 1
        elif existing == norm:
            res.listing_date_unchanged += 1
        else:
            # 冲突：不静默覆盖，记录供人工/后续决策
            res.listing_date_conflict += 1
            logger.warning(
                "LISTING_DATE_CONFLICT symbol=%s existing=%s pytdx_ipo=%s",
                inst.symbol, existing, norm,
            )

    if not dry_run:
        await session.commit()
    else:
        await session.rollback()

    return res
