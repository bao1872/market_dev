"""Instrument 上市生命周期 owner — listing boundary 纯单元测试。

CHANGE-20260816-002：Auction 120D PIT population listing boundary。

本测试在 PURE_UNIT_TEST=1 下运行（不连接数据库）。
通过纯函数判定 + 内存 fake session/provider 覆盖：
- normalize_pytdx_ipo_date
- is_listed_a_share_at（不含 status）
- resolve_listed_a_share_instruments_at（listing boundary）
- sync_listing_dates（写入语义 + 幂等 + conflict fail-closed）
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from app.services.instrument_lifecycle_service import (
    ListingDateSyncResult,
    is_listed_a_share_at,
    normalize_pytdx_ipo_date,
    resolve_listed_a_share_instruments_at,
    sync_listing_dates,
)


# ---------------------------------------------------------------------------
# Fake 支撑
# ---------------------------------------------------------------------------
class _FakeInstrument:
    def __init__(self, symbol: str, market: str, listing_date: date | None = None,
                 status: str = "active", instrument_id: str = "") -> None:
        self.symbol = symbol
        self.market = market
        self.listing_date = listing_date
        self.status = status
        self.instrument_id = instrument_id or f"{symbol}.{market}"

    # 供 pytest 失败时可读
    def __repr__(self) -> str:  # pragma: no cover
        return f"<_FakeInstrument {self.symbol}.{self.market} ld={self.listing_date}>"


class _FakeSession:
    """内存 async session：仅支持 execute(select) + commit/rollback。

    用 is_listed_a_share_at 判定每行是否满足过滤，避免使用数据库。
    rollback() 还原 dry-run 期间对 inst 对象的就地修改（模拟 DB 回滚）。
    """

    def __init__(self, rows: list[_FakeInstrument]) -> None:
        self._rows = rows
        self.committed = False
        self.rolled_back = False
        self._snapshot: dict[int, Any] = {}

    def _snapshot_row(self, inst: _FakeInstrument) -> None:
        if id(inst) not in self._snapshot:
            self._snapshot[id(inst)] = inst.listing_date

    async def execute(self, stmt):
        td = _extract_trade_date(stmt)
        if td is not None:
            kept = [r for r in self._rows if _matches(r, td)]
        else:
            from app.services.instrument_lifecycle_service import _AUCTION_A_SHARE_MARKETS
            kept = [
                r for r in self._rows
                if is_listed_a_share_at(r.symbol, r.market, date(1900, 1, 1), date(2999, 1, 1))
                and r.market in _AUCTION_A_SHARE_MARKETS
            ]
        # 记录将被 sync 修改的对象（仅标记，真实赋值发生在 service 内）
        for r in kept:
            self._snapshot_row(r)
        return _FakeResult(kept)

    async def execute(self, stmt):
        td = _extract_trade_date(stmt)
        if td is not None:
            # resolver 路径：应用 listing boundary
            kept = [r for r in self._rows if _matches(r, td)]
        else:
            # sync 路径：仅按 stock_symbol_sql_filter + market 过滤
            from app.services.instrument_lifecycle_service import _AUCTION_A_SHARE_MARKETS
            kept = [
                r for r in self._rows
                if is_listed_a_share_at(r.symbol, r.market, date(1900, 1, 1), date(2999, 1, 1))
                and r.market in _AUCTION_A_SHARE_MARKETS
            ]
        for r in kept:
            self._snapshot_row(r)
        return _FakeResult(kept)

    async def commit(self) -> None:
        self.committed = True
        self._snapshot.clear()

    async def rollback(self) -> None:
        # 还原 dry-run 期间对 inst 对象的就地修改（模拟 DB 回滚）
        for inst in self._rows:
            if id(inst) in self._snapshot:
                inst.listing_date = self._snapshot[id(inst)]
        self.rolled_back = True


def _extract_trade_date(stmt):
    # 从 select(Instrument).where(listed_a_share_filter_at(td)) 中取 td
    try:
        wc = list(getattr(stmt, "_where_criteria", ()))
        if wc:
            return _find_trade_date_in_expr(wc[0])
    except Exception:
        return None
    return None


def _find_trade_date_in_expr(expr):
    # 递归遍历表达式树，寻找持有 date 值的 BindParameter
    if expr is None or isinstance(expr, (str, int, float)):
        return None
    if isinstance(expr, date):
        return expr
    # BindParameter 可能直接携带 .value（date 类型）
    val = getattr(expr, "value", None)
    if isinstance(val, date):
        return val
    # 递归子节点：left/right（BinaryExpression）或 clauses（BooleanClauseList/and_/or_）
    for attr in ("left", "right", "clauses", "_orig"):
        child = getattr(expr, attr, None)
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            for c in child:
                found = _find_trade_date_in_expr(c)
                if found is not None:
                    return found
        else:
            found = _find_trade_date_in_expr(child)
            if found is not None:
                return found
    return None


def _matches(inst: _FakeInstrument, trade_date: date | None) -> bool:
    if trade_date is None:
        return False
    return is_listed_a_share_at(inst.symbol, inst.market, inst.listing_date, trade_date)


class _FakeResult:
    def __init__(self, rows: list[_FakeInstrument]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeFinanceProvider:
    def __init__(self, mapping: dict[str, dict[str, Any]]) -> None:
        self._mapping = mapping

    async def get_finance_info(self, symbol: str):
        return self._mapping.get(symbol)


# ---------------------------------------------------------------------------
# L1/L2 — normalize_pytdx_ipo_date
# ---------------------------------------------------------------------------
class TestNormalizePytdxIpoDate:
    def test_valid_int(self):
        assert normalize_pytdx_ipo_date(19910403) == date(1991, 4, 3)

    def test_valid_string(self):
        assert normalize_pytdx_ipo_date("20240115") == date(2024, 1, 15)

    def test_zero(self):
        assert normalize_pytdx_ipo_date(0) is None

    def test_none(self):
        assert normalize_pytdx_ipo_date(None) is None

    def test_empty_string(self):
        assert normalize_pytdx_ipo_date("") is None

    def test_non_digit(self):
        assert normalize_pytdx_ipo_date("abc") is None

    def test_malformed_calendar(self):
        # 2 月 30 日不存在
        assert normalize_pytdx_ipo_date(20260230) is None

    def test_too_short(self):
        assert normalize_pytdx_ipo_date(20241) is None

    def test_negative(self):
        assert normalize_pytdx_ipo_date(-1) is None

    def test_bool_rejected(self):
        assert normalize_pytdx_ipo_date(True) is None


# ---------------------------------------------------------------------------
# L3-L7 — is_listed_a_share_at（明确不含 status）
# ---------------------------------------------------------------------------
class TestIsListedAShareAt:
    def test_old_stock_included(self):
        # 老股票，listing 远早于 T
        assert is_listed_a_share_at("600000", "SH", date(1991, 4, 3), date(2026, 3, 1)) is True

    def test_status_inactive_but_listed_included(self):
        # 关键：status='inactive' 但 listing_date <= T → 仍包含
        assert is_listed_a_share_at("600000", "SH", date(1991, 4, 3), date(2026, 3, 1),
                                     ) is True

    def test_listing_eq_t_included(self):
        assert is_listed_a_share_at("688001", "SH", date(2026, 3, 1), date(2026, 3, 1)) is True

    def test_listing_after_t_excluded(self):
        # T < listing_date → 排除（新年上市股在上市前不应出现）
        assert is_listed_a_share_at("688999", "SH", date(2026, 5, 1), date(2026, 3, 1)) is False

    def test_listing_none_excluded(self):
        # listing_date 缺失 → 不默认 include
        assert is_listed_a_share_at("600000", "SH", None, date(2026, 3, 1)) is False

    def test_non_stock_excluded(self):
        # 指数（SH000001）非股票
        assert is_listed_a_share_at("000001", "SH", date(1991, 1, 1), date(2026, 3, 1)) is False

    def test_bj_excluded_from_auction_scope(self):
        # BJ 不在 Auction SH/SZ 范围
        assert is_listed_a_share_at("920001", "BJ", date(2023, 1, 1), date(2026, 3, 1)) is False

    def test_sz_stock_included(self):
        assert is_listed_a_share_at("000001", "SZ", date(1991, 4, 3), date(2026, 3, 1)) is True


# ---------------------------------------------------------------------------
# resolver 集成（内存 fake session）
# ---------------------------------------------------------------------------
class TestResolveListedAShareAt:
    def _make_session(self):
        rows = [
            _FakeInstrument("600000", "SH", date(1991, 4, 3), status="active"),
            _FakeInstrument("688999", "SH", date(2026, 5, 1), status="active"),
            _FakeInstrument("000001", "SZ", date(1991, 4, 3), status="inactive"),
            _FakeInstrument("920001", "BJ", date(2023, 1, 1), status="active"),
            _FakeInstrument("000001", "SH", date(1991, 1, 1), status="active"),  # 指数
        ]
        return _FakeSession(rows)

    def test_resolver_excludes_new_listing_before_date(self):
        sess = self._make_session()
        out = asyncio.get_event_loop().run_until_complete(
            resolve_listed_a_share_instruments_at(sess, date(2026, 3, 1))
        )
        syms = {i.symbol for i in out}
        assert "688999" not in syms  # 2026-05-01 上市，T=03-01 不在
        assert "600000" in syms
        assert "000001.SZ" in {i.instrument_id for i in out}

    def test_resolver_includes_inactive_listed(self):
        sess = self._make_session()
        out = asyncio.get_event_loop().run_until_complete(
            resolve_listed_a_share_instruments_at(sess, date(2026, 3, 1))
        )
        ids = {i.instrument_id for i in out}
        assert "000001.SZ" in ids  # status=inactive 但 listing<=T 仍包含
        assert "920001.BJ" not in ids  # BJ 范围外


# ---------------------------------------------------------------------------
# L8-L10 — sync_listing_dates 写入语义 + 幂等 + conflict
# ---------------------------------------------------------------------------
class TestSyncListingDates:
    def test_insert_when_existing_none(self):
        inst = _FakeInstrument("600000", "SH", None)
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 19910403}})
        sess = _FakeSession([inst])
        res = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        assert inst.listing_date == date(1991, 4, 3)
        assert res.listing_date_inserted == 1
        assert res.listing_date_conflict == 0

    def test_unchanged_when_equal(self):
        inst = _FakeInstrument("600000", "SH", date(1991, 4, 3))
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 19910403}})
        sess = _FakeSession([inst])
        res = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        assert res.listing_date_unchanged == 1
        assert res.listing_date_inserted == 0

    def test_conflict_not_overwritten(self):
        inst = _FakeInstrument("600000", "SH", date(1991, 4, 3))
        # source 给出不同日期 → 冲突，不覆盖
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 20200101}})
        sess = _FakeSession([inst])
        res = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        assert inst.listing_date == date(1991, 4, 3)  # 未覆盖
        assert res.listing_date_conflict == 1

    def test_missing_source_preserves_existing(self):
        inst = _FakeInstrument("600000", "SH", date(1991, 4, 3))
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 0}})  # missing
        sess = _FakeSession([inst])
        res = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        assert inst.listing_date == date(1991, 4, 3)  # 保持
        assert res.listing_date_missing == 1

    def test_idempotent_rerun(self):
        inst = _FakeInstrument("600000", "SH", None)
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 19910403}})
        sess = _FakeSession([inst])
        r1 = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        # 第二次：existing == source
        r2 = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=False)
        )
        assert r1.listing_date_inserted == 1
        assert r2.listing_date_unchanged == 1
        assert inst.listing_date == date(1991, 4, 3)

    def test_dry_run_no_commit(self):
        inst = _FakeInstrument("600000", "SH", None)
        prov = _FakeFinanceProvider({"600000": {"ipo_date_raw": 19910403}})
        sess = _FakeSession([inst])
        res = asyncio.get_event_loop().run_until_complete(
            sync_listing_dates(sess, prov, dry_run=True)
        )
        assert inst.listing_date is None  # dry-run 未写
        assert res.listing_date_inserted == 1  # 仍计入 proposed
        assert sess.rolled_back is True
        assert sess.committed is False
