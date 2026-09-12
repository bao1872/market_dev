"""[EOD-SNAPSHOT] 盘后快照落库服务单元测试（纯单元，mock session）。

覆盖：
1. universe 同步：新股 INSERT、已有股 UPDATE（name/market）、不写 listing_date、
   snapshot 缺股不自动 delist、重复 symbol 去重；
2. 当日 raw 日线批量 upsert：入参校验（trade_date 不符 / 空 OHLC / <=0 /
   high<max(o,c) / low>min(o,c) / 负 volume.amount 全部跳过）；
3. **adj_factor 契约**：conflict 分支不得出现 adj_factor（保留既有前复权因子）；
4. 缺口集合差查询复用 stock_symbol_sql_filter；
5. 历史 fallback：pytdx 优先；失败 → Eastmoney fqt=0；Eastmoney 落库为
   insert-only（on_conflict_do_nothing，不得覆盖既有行）；
   **BJ 不得调用 pytdx**；**成功 ⟺ DB 中目标日真实存在**（禁止假成功）；
6. pytdx 连续失败熔断；
7. 补缺窗口（窄）与新股历史窗口（listing_date/2023-01-01）区分；
8. provider 整体失败退回 legacy 逐股路径，且不静默（daily_mode 明确）；
9. 快照规模防护（接口筛选失效 → fail-closed，避免 fallback storm）；
10. 日线连续性扫描：09-10 整日空洞不得被 max(trade_date)=09-11 掩盖；
    修复计划区分 market_wide_gap / sparse_symbol_gap；
11. 空 universe 时仍执行快照发现；空 universe + 快照失败 → fail-closed；
    periods 参数（None=d+15m+60m / ("d",)=仅日线）；过去交易日禁用当日快照；
    legacy daily 的 end_date 必须显式透传（不得写死今天）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects.postgresql import Insert

from app.models.instrument import Instrument
from app.repositories import bar_repository as bar_repo
from app.services import bars_scheduler_service as scheduler_module
from app.services import eod_daily_refresh_service as refresh_mod
from app.services import eod_market_snapshot_provider as prov
from app.services.adjustment_factor_service import FactorSourceUnavailableError
from app.services.bars_scheduler_service import (
    BarsSchedulerService,
    FactorProviderHealth,
)
from app.services.eod_daily_refresh_service import (
    DailyContinuityBlockedError,
    DailyGap,
    InstrumentSyncResult,
)
from app.services.eod_market_snapshot_provider import EodSnapshotRow, SnapshotProviderError

TRADE_DATE = date(2026, 9, 11)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _ScriptedSession:
    """记录的 session：INSERT 语句被捕获，SELECT 按脚本返回行。

    ``scalar_queue`` 供 ``session.scalar()`` 使用（回补成功判定走
    ``has_daily_bar`` / ``count_daily_bars``，都是 scalar 查询）。
    默认返回 None = 「DB 里没有该数据」= 回补未成功。
    """

    def __init__(
        self,
        select_queue: list[list[Any]] | None = None,
        scalar_queue: list[Any] | None = None,
    ) -> None:
        self.insert_statements: list[Any] = []
        self.select_statements: list[Any] = []
        self.scalar_statements: list[Any] = []
        self._select_queue = list(select_queue or [])
        self._scalar_queue = list(scalar_queue or [])
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        if isinstance(stmt, Insert):
            self.insert_statements.append(stmt)
            return _FakeResult([])
        self.select_statements.append(stmt)
        rows = self._select_queue.pop(0) if self._select_queue else []
        return _FakeResult(rows)

    async def scalar(self, stmt: Any) -> Any:
        self.scalar_statements.append(stmt)
        return self._scalar_queue.pop(0) if self._scalar_queue else None

    async def scalars(self, stmt: Any) -> _FakeResult:
        self.select_statements.append(stmt)
        rows = self._select_queue.pop(0) if self._select_queue else []
        return _FakeResult(rows)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=pg_dialect.dialect()))


def _sql_literals(stmt: Any) -> str:
    """把绑定参数内联，便于断言正则/常量字面量确实进入 SQL。"""
    return str(
        stmt.compile(
            dialect=pg_dialect.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _set_clause(sql: str) -> str:
    """取出 ON CONFLICT ... DO UPDATE SET 之后的部分。"""
    assert "DO UPDATE SET" in sql, sql
    return sql.split("DO UPDATE SET", 1)[1]


def _snap(
    symbol: str,
    *,
    name: str = "测试股",
    market: str = "SH",
    trade_date: date | None = TRADE_DATE,
    open_: Any = Decimal("10.00"),
    high: Any = Decimal("10.80"),
    low: Any = Decimal("9.90"),
    close: Any = Decimal("10.50"),
    volume: Any = Decimal("12345"),
    amount: Any = Decimal("6789012"),
) -> EodSnapshotRow:
    # DTO 保留完整 f124 时间（updated_at），trade_date 是其日期投影。
    updated_at = (
        datetime.combine(trade_date, time(15, 30)) if trade_date is not None else None
    )
    return EodSnapshotRow(
        symbol=symbol,
        name=name,
        market=market,
        updated_at=updated_at,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        previous_close=Decimal("10.00"),
    )


def _instrument(symbol: str, *, name: str = "测试股", market: str = "SH") -> Instrument:
    return Instrument(symbol=symbol, name=name, market=market, status="active")


# ===========================================================================
# 1. universe 同步
# ===========================================================================


@pytest.mark.asyncio
async def test_universe_inserts_new_symbols_with_pinyin_and_no_listing_date() -> None:
    session = _ScriptedSession([[], [_instrument("920001", market="BJ")]])
    rows = [_snap("920001", name="北新科技", market="BJ"), _snap("600519", name="贵州茅台")]

    result = await refresh_mod.sync_instruments_from_eod_snapshot(session, rows, TRADE_DATE)  # type: ignore[arg-type]

    assert result.new_symbols == ["920001", "600519"]
    assert result.updated_symbols == []
    assert len(session.insert_statements) == 1
    sql = _sql(session.insert_statements[0])
    assert "pinyin_initials" in sql
    # 新建行不得携带 listing_date（未知上市日不得伪造）
    assert "listing_date" not in sql
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_universe_updates_existing_name_and_market_but_keeps_status_active() -> None:
    existing = _instrument("600519", name="旧名称", market="SZ")
    session = _ScriptedSession([[existing]])

    result = await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("600519", name="贵州茅台", market="SH")], TRADE_DATE  # type: ignore[arg-type]
    )

    assert existing.name == "贵州茅台"
    assert existing.market == "SH"
    assert existing.status == "active"
    assert result.updated_symbols == ["600519"]
    assert result.new_symbols == []
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_universe_unchanged_symbol_produces_no_update() -> None:
    existing = _instrument("600519", name="贵州茅台", market="SH")
    session = _ScriptedSession([[existing]])

    result = await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("600519", name="贵州茅台", market="SH")], TRADE_DATE  # type: ignore[arg-type]
    )

    assert result.updated_symbols == []
    assert result.new_symbols == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_universe_does_not_delist_symbol_missing_from_snapshot() -> None:
    """snapshot 中缺席的股票必须保持原状，不得自动 delist。"""
    existing = _instrument("600519", name="贵州茅台")
    session = _ScriptedSession([[existing], [_instrument("920001", market="BJ")]])

    await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("920001", market="BJ")], TRADE_DATE  # type: ignore[arg-type]
    )

    assert existing.status == "active"


@pytest.mark.asyncio
async def test_universe_dedups_repeated_symbol() -> None:
    session = _ScriptedSession([[], [_instrument("600519")]])

    result = await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("600519"), _snap("600519")], TRADE_DATE  # type: ignore[arg-type]
    )

    assert result.new_symbols == ["600519"]


@pytest.mark.asyncio
async def test_universe_empty_snapshot_is_noop() -> None:
    session = _ScriptedSession()
    result = await refresh_mod.sync_instruments_from_eod_snapshot(session, [], TRADE_DATE)  # type: ignore[arg-type]
    assert result.new_symbols == []
    assert session.select_statements == []
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_universe_reactivates_inactive_symbol_with_valid_today_bar() -> None:
    """inactive 股票恢复交易：名称/市场都没变，但只要拿到有效 T 日日线就必须复活。

    旧逻辑只在 name/market 变化时才置 active，导致这类股票永远回不到覆盖率分母。
    """
    existing = _instrument("600519", name="贵州茅台", market="SH")
    existing.status = "inactive"
    session = _ScriptedSession([[existing]])

    result = await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("600519", name="贵州茅台", market="SH")], TRADE_DATE  # type: ignore[arg-type]
    )

    assert existing.status == "active"
    assert result.updated_symbols == ["600519"]


@pytest.mark.asyncio
async def test_universe_does_not_reactivate_inactive_symbol_without_valid_bar() -> None:
    """停牌/空价（OHLC 缺失）的 snapshot 行不得把 inactive 股票强行置 active。"""
    existing = _instrument("600519", name="贵州茅台", market="SH")
    existing.status = "inactive"
    session = _ScriptedSession([[existing]])
    suspended = _snap(
        "600519",
        name="贵州茅台",
        market="SH",
        open_=None,
        high=None,
        low=None,
        close=None,
    )

    await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [suspended], TRADE_DATE  # type: ignore[arg-type]
    )

    assert existing.status == "inactive"


@pytest.mark.asyncio
async def test_universe_new_symbol_without_valid_bar_is_inactive() -> None:
    """新 symbol 但没有任何有效 T 日行情 → 先 inactive，不进覆盖率分母。"""
    session = _ScriptedSession([[], []])
    suspended = _snap("920999", market="BJ", volume=None, amount=None)

    await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [suspended], TRADE_DATE  # type: ignore[arg-type]
    )

    sql = _sql_literals(session.insert_statements[0])
    assert "'inactive'" in sql


# ===========================================================================
# 2/3. 当日 raw 日线 upsert + adj_factor 契约
# ===========================================================================


@pytest.mark.asyncio
async def test_upsert_raw_daily_writes_ohlcv_and_preserves_adj_factor_on_conflict() -> None:
    session = _ScriptedSession()
    iid = uuid.uuid4()

    written = await refresh_mod.upsert_raw_daily_snapshot(
        session, TRADE_DATE, [(iid, _snap("600519"))]  # type: ignore[arg-type]
    )

    assert written == 1
    assert len(session.insert_statements) == 1
    sql = _sql(session.insert_statements[0])
    assert "ON CONFLICT (instrument_id, trade_date) DO UPDATE SET" in sql
    set_clause = _set_clause(sql)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        assert col in set_clause
    # 核心契约：conflict 不得触碰 adj_factor（保留既有前复权因子）
    assert "adj_factor" not in set_clause
    # 但首插仍写入 adj_factor
    assert "adj_factor" in sql.split("ON CONFLICT", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snap",
    [
        _snap("600519", trade_date=date(2026, 9, 10)),      # 非目标交易日（老时间戳）
        _snap("600519", trade_date=None),                    # 无法判定日期
        _snap("600519", open_=None),                         # 缺 open
        _snap("600519", close=None),                         # 缺 close
        _snap("600519", high=None, low=None),
        _snap("600519", open_=Decimal("0")),                 # 非正价格
        _snap("600519", close=Decimal("-1")),
        _snap("600519", open_=Decimal("11.0"), close=Decimal("10.0"), high=Decimal("10.5")),  # high < max(o,c)
        _snap("600519", open_=Decimal("10.0"), close=Decimal("11.0"), low=Decimal("10.5")),   # low > min(o,c)
        _snap("600519", volume=Decimal("-1")),
        _snap("600519", amount=Decimal("-1")),
        _snap("600519", volume=None),
        _snap("600519", amount=None),
    ],
    ids=[
        "stale-trade-date",
        "null-trade-date",
        "missing-open",
        "missing-close",
        "missing-high-low",
        "zero-open",
        "negative-close",
        "high-below-body",
        "low-above-body",
        "negative-volume",
        "negative-amount",
        "null-volume",
        "null-amount",
    ],
)
async def test_upsert_raw_daily_rejects_invalid_rows(snap: EodSnapshotRow) -> None:
    session = _ScriptedSession()
    written = await refresh_mod.upsert_raw_daily_snapshot(
        session, TRADE_DATE, [(uuid.uuid4(), snap)]  # type: ignore[arg-type]
    )
    assert written == 0
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_upsert_raw_daily_returns_valid_count_only() -> None:
    session = _ScriptedSession()
    good = _snap("600519")
    bad = _snap("000001", volume=Decimal("-5"))
    written = await refresh_mod.upsert_raw_daily_snapshot(
        session, TRADE_DATE, [(uuid.uuid4(), good), (uuid.uuid4(), bad)]  # type: ignore[arg-type]
    )
    assert written == 1


# ===========================================================================
# 4. 缺口集合差
# ===========================================================================


@pytest.mark.asyncio
async def test_find_missing_uses_stock_symbol_sql_filter() -> None:
    missing = [_instrument("600519")]
    session = _ScriptedSession([missing])

    got = await refresh_mod.find_missing_daily_instruments(session, TRADE_DATE)  # type: ignore[arg-type]

    assert got == missing
    sql = _sql_literals(session.select_statements[0])
    # 分母必须是「活跃 A 股股票」，与覆盖率口径一致（排除指数/ETF）
    assert "920[0-9]{3}" in sql
    assert "status" in sql


# ===========================================================================
# 5/6/7. 历史 fallback
# ===========================================================================


class _DummyAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _DummyAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _kline_records(days: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(days):
        d = date(2026, 9, 1) + timedelta(days=i)
        out.append(
            {
                "datetime": d.isoformat(),
                "open": 10.0 + i,
                "high": 10.8 + i,
                "low": 9.9 + i,
                "close": 10.5 + i,
                "volume": 1000.0 + i,
                "amount": 10000.0 + i,
            }
        )
    return out


@pytest.mark.asyncio
async def test_backfill_prefers_pytdx_and_skips_eastmoney(monkeypatch: pytest.MonkeyPatch) -> None:
    """SH/SZ 回补以 pytdx 为主源，主源完成后禁止再调 Eastmoney。

    pytdx 成功落库（DB 查询确认覆盖）→ 不得请求 Eastmoney（减少无意义 provider I/O）。
    """
    calls: list[str] = []

    async def fake_refresh(session: Any, instrument_id: Any, start: Any, end: Any, adapter: Any = None) -> Any:
        import pandas as pd

        calls.append("pytdx")
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        calls.append("eastmoney")
        raise AssertionError("pytdx 已成功，不得调用 Eastmoney")

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # pytdx 写入后由 DB 查询确认（has_daily_bar 命中）
    session = _ScriptedSession(scalar_queue=[1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is True
    assert calls == ["pytdx"]


@pytest.mark.asyncio
async def test_backfill_falls_back_to_eastmoney_when_pytdx_has_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytdx 主源拿不到数据 → Eastmoney fqt=0 兜底（仅 SH/SZ）。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        calls.append("pytdx")
        return pd.DataFrame()  # 主源空

    async def fake_em(client: Any, symbol: str, market: str, start: Any, end: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # pytdx 空 → 先判失败一次；Eastmoney 补齐后命中
    session = _ScriptedSession(scalar_queue=[None, 1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is True
    assert calls == ["pytdx", "eastmoney"]


@pytest.mark.asyncio
async def test_backfill_provider_error_is_not_silent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """两边都失败必须返回 False —— 不得把 provider 错误伪装成成功。"""

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pytdx down")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        raise SnapshotProviderError("eastmoney down")

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession()
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is False
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_backfill_empty_results_do_not_count_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """两侧都返回空 → 不得算成功（必须靠 DB 目标日查询判定）。"""

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        return pd.DataFrame()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession(scalar_queue=[None, None])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is False
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_backfill_success_requires_target_date_in_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """补 T 日成功 ⟺ bars_daily(T) 真实存在。

    回归防护：过去用「provider 返回非空历史」代表成功 —— 抓到的是 09-01 的数据，
    但目标 09-11 仍然缺失，却会被计成 fallback 成功。
    """
    import pandas as pd

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        return pd.DataFrame({"close": [1.0]})   # 有数据，但 DB 里没有目标日

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records(days=3)           # 只有 09-01~09-03，不含 09-11

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # 两次校验都返回 None：目标日不在 DB 中 → 必须判失败
    session = _ScriptedSession(scalar_queue=[None, None])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session,
        _instrument("600519"),
        date(2026, 9, 1),
        TRADE_DATE,
        target_trade_date=TRADE_DATE,
    )

    assert ok is False
    # 且校验用的必须是「目标交易日」查询（has_daily_bar）
    assert session.scalar_statements, "必须执行 DB 校验查询"
    assert "trade_date" in _sql(session.scalar_statements[0])


@pytest.mark.asyncio
async def test_fill_missing_counts_only_target_date_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fill_missing 的返回值必须是「T 日确认存在」的只数。

    两只股票都拿到了非空历史，但只有第二只在 DB 中确认了 T 日 → 返回 1。
    """
    import pandas as pd

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records(days=3)

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # 每只股票各两次校验：#1 不过，#2 只有第二只通过
    session = _ScriptedSession(scalar_queue=[None, None, None, 1])
    written = await refresh_mod.fill_missing_daily_instruments(
        session,  # type: ignore[arg-type]
        [_instrument("600519"), _instrument("000001", market="SZ")],
        TRADE_DATE,
    )

    assert written == 1


@pytest.mark.asyncio
async def test_backfill_bj_never_calls_pytdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """北交所必须直接走 Eastmoney：pytdx 标准接口不覆盖 BSE。

    若仍先调 pytdx，三次 BJ 失败就会把共享 breaker 熔断，连带影响沪深标的。
    """
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        calls.append("pytdx")
        raise AssertionError("BJ 不得调用 pytdx")

    async def fake_em(client: Any, symbol: str, market: str, start: Any, end: Any) -> Any:
        calls.append("eastmoney")
        assert market == "BJ"
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # BJ 只走 Eastmoney（use_pytdx=False），因此只有一次 DB 校验
    session = _ScriptedSession(scalar_queue=[1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session,
        _instrument("920001", market="BJ"),
        date(2026, 9, 1),
        TRADE_DATE,
        target_trade_date=TRADE_DATE,
    )

    assert ok is True
    assert calls == ["eastmoney"]


# ===========================================================================
# 6. pytdx 熔断
# ===========================================================================


def test_pytdx_breaker_opens_after_consecutive_failures_and_resets() -> None:
    breaker = refresh_mod._PytdxBreaker(limit=3)  # noqa: SLF001
    assert breaker.allow is True
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow is True
    breaker.record_failure()
    assert breaker.allow is False
    breaker.record_success()
    assert breaker.allow is True


@pytest.mark.asyncio
async def test_backfill_skips_pytdx_once_breaker_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytdx 是主源，breaker 打开后必须停止 pytdx 尝试，直接走 Eastmoney fallback。

    注意顺序：pytdx primary，连续失败达到 limit 后熔断，剩余沪深标的不再尝试 pytdx。
    """
    attempts = {"pytdx": 0}

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        attempts["pytdx"] += 1
        raise RuntimeError("pytdx down")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession()
    breaker = refresh_mod._PytdxBreaker(limit=2)  # noqa: SLF001
    for _ in range(5):
        await refresh_mod._backfill_one(  # noqa: SLF001
            session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE,  # type: ignore[arg-type]
            breaker=breaker,
        )

    # 只在前 2 只上尝试过 pytdx，第 3 只起熔断
    assert attempts["pytdx"] == 2


# ===========================================================================
# 6b. G1A source policy（pytdx 主源 / Eastmoney fallback 调用顺序）
# ===========================================================================


@pytest.mark.asyncio
async def test_backfill_pytdx_success_no_eastmoney(monkeypatch: pytest.MonkeyPatch) -> None:
    """[A] SH/SZ + pytdx 成功并完成 DB coverage → pytdx 1 次、Eastmoney 0 次。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        calls.append("pytdx")
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        calls.append("eastmoney")
        raise AssertionError("pytdx 已成功，不得调 Eastmoney")

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession(scalar_queue=[1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )
    assert ok is True
    assert calls == ["pytdx"]


@pytest.mark.asyncio
async def test_backfill_pytdx_exception_triggers_fallback_and_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """[B] SH/SZ + pytdx 异常 → breaker 失败 +1，且 Eastmoney fallback 被调用。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        calls.append("pytdx")
        raise RuntimeError("pytdx down")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    breaker = refresh_mod._PytdxBreaker(limit=3)  # noqa: SLF001
    session = _ScriptedSession(scalar_queue=[None, 1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE,  # type: ignore[arg-type]
        breaker=breaker,
    )
    assert ok is True
    assert calls == ["pytdx", "eastmoney"]
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_backfill_pytdx_empty_falls_back_and_breaker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """[C] SH/SZ + pytdx 返回空 / 未完成 DB coverage → breaker 失败，Eastmoney 兜底。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        calls.append("pytdx")
        return pd.DataFrame()  # 空 → 未完成任务

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    breaker = refresh_mod._PytdxBreaker(limit=3)  # noqa: SLF001
    # pytdx 空 → _did_backfill None；Eastmoney 补齐 → 1
    session = _ScriptedSession(scalar_queue=[None, 1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE,  # type: ignore[arg-type]
        breaker=breaker,
    )
    assert ok is True
    assert calls == ["pytdx", "eastmoney"]
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_backfill_breaker_open_skips_pytdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """[D] breaker 已打开 → pytdx 0 次，Eastmoney 1 次。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        calls.append("pytdx")
        raise AssertionError("breaker 已打开，不得调 pytdx")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    breaker = refresh_mod._PytdxBreaker(limit=1)  # noqa: SLF001
    breaker.record_failure()  # 已达上限 → allow False
    assert breaker.allow is False
    session = _ScriptedSession(scalar_queue=[1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE,  # type: ignore[arg-type]
        breaker=breaker,
    )
    assert ok is True
    assert calls == ["eastmoney"]


@pytest.mark.asyncio
async def test_backfill_bj_uses_eastmoney_and_does_not_touch_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """[E] BJ → pytdx 0 次、Eastmoney 1 次，且 breaker 状态不变。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        calls.append("pytdx")
        raise AssertionError("BJ 不得调用 pytdx")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    breaker = refresh_mod._PytdxBreaker(limit=3)  # noqa: SLF001
    session = _ScriptedSession(scalar_queue=[1])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("920001", market="BJ"), date(2026, 9, 1), TRADE_DATE,  # type: ignore[arg-type]
        breaker=breaker,
    )
    assert ok is True
    assert calls == ["eastmoney"]
    assert breaker.consecutive_failures == 0  # BJ 不污染 breaker


@pytest.mark.asyncio
async def test_backfill_fallback_judged_by_db_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """[F] fallback 完成后仍必须以 _did_backfill() 为成功判据（DB 真实存在）。"""
    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        return pd.DataFrame()  # 主源空

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records()  # 写入 fallback 数据，但不含目标日

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # Eastmoney 写入记录但不含目标日 → 两次 _did_backfill 都 None
    session = _ScriptedSession(scalar_queue=[None, None])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session,
        _instrument("600519"),
        date(2026, 9, 1),
        TRADE_DATE,
        target_trade_date=TRADE_DATE,
    )
    assert ok is False
    # 必须执行过 DB 校验查询（target_trade_date 路径）
    assert session.scalar_statements
    assert "trade_date" in _sql(session.scalar_statements[0])


@pytest.mark.asyncio
async def test_backfill_pytdx_nonempty_but_target_day_missing_goes_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """[G] pytdx 返回非空但目标 T 日仍不存在 → 不能误判成功，必须进入 fallback。"""
    calls: list[str] = []

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        calls.append("pytdx")
        return pd.DataFrame({"close": [1.0]})  # 有数据，但 DB 里无目标日

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return _kline_records(days=3)  # 09-01~09-03，不含 09-11

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    # 两次校验都 None：目标日不在 DB → 先判 pytdx 失败，再判 fallback 失败
    session = _ScriptedSession(scalar_queue=[None, None])
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session,
        _instrument("600519"),
        date(2026, 9, 1),
        TRADE_DATE,
        target_trade_date=TRADE_DATE,
    )
    assert ok is False
    assert calls == ["pytdx", "eastmoney"]  # 主源未完成任务 → 进入 fallback


@pytest.mark.asyncio
async def test_backfill_eastmoney_fallback_is_insert_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eastmoney fallback 落库必须 insert-only（on_conflict_do_nothing）。"""
    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        import pandas as pd

        return pd.DataFrame()  # 主源空 → 走 fallback

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession(scalar_queue=[None, 1])
    await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )
    assert len(session.insert_statements) == 1
    sql = _sql(session.insert_statements[0])
    assert "ON CONFLICT (instrument_id, trade_date) DO NOTHING" in sql
    assert "DO UPDATE SET" not in sql


# ===========================================================================
# 7. 补缺窗口 vs 新股窗口
# ===========================================================================


@pytest.mark.asyncio
async def test_fill_missing_uses_narrow_lookback_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    windows: list[tuple[date, date]] = []

    async def fake_refresh(session: Any, iid: Any, start: Any, end: Any, adapter: Any = None) -> Any:
        windows.append((start, end))
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return []

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    inst = _instrument("600519")
    inst.listing_date = date(2020, 1, 1)
    await refresh_mod.fill_missing_daily_instruments(
        _ScriptedSession(), [inst], TRADE_DATE  # type: ignore[arg-type]
    )

    assert windows == [(TRADE_DATE - timedelta(days=10), TRADE_DATE)]


@pytest.mark.asyncio
async def test_backfill_new_instruments_starts_from_listing_date(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    windows: list[tuple[date, date]] = []

    async def fake_refresh(session: Any, iid: Any, start: Any, end: Any, adapter: Any = None) -> Any:
        windows.append((start, end))
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return []

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)
    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        AsyncMock(return_value=date(2026, 9, 10)),
    )

    with_listing = _instrument("688001")     # 科创板新股
    with_listing.listing_date = date(2026, 6, 1)
    without_listing = _instrument("600519")
    without_listing.listing_date = None

    await refresh_mod.backfill_new_instruments(
        _ScriptedSession(), [with_listing, without_listing], TRADE_DATE  # type: ignore[arg-type]
    )

    assert windows == [
        (date(2026, 6, 1), TRADE_DATE),
        (date(2023, 1, 1), TRADE_DATE),
    ]


# ===========================================================================
# 8. BarsScheduler 集成：快照成功短路 d 阶段 / 失败退回 legacy
# ===========================================================================


def _patch_daily_harness(
    monkeypatch: pytest.MonkeyPatch,
    service: BarsSchedulerService,
    instruments: list[Any],
    *,
    eod_ready: bool = True,
) -> list[str]:
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    # 当日收盘窗口守卫依赖真实时钟；测试必须显式控制，否则会在 15:05 前后表现出
    # 完全不同的行为（这正是该守卫存在的意义）。
    monkeypatch.setattr(
        prov, "can_use_same_day_eod_snapshot", lambda *a, **k: eod_ready
    )
    started: list[str] = []

    async def fake_serial(_instruments: Any, *, period: str, **kwargs: Any) -> tuple[int, int]:
        started.append(period)
        return 0, 0

    async def fake_parallel(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("parallel path should not run for workers=1")

    monkeypatch.setattr(service, "_run_serial_period", fake_serial)
    monkeypatch.setattr(service, "_run_parallel_period", fake_parallel)
    # 连续性硬门禁：默认放行（缺口场景由专门用例覆盖）
    monkeypatch.setattr(
        service, "_scan_daily_continuity_gate", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        service, "_run_post_daily_phase", AsyncMock(return_value=None)
    )
    return started


@pytest.mark.asyncio
async def test_daily_snapshot_success_short_circuits_legacy_d_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service, "_refresh_daily_from_market_snapshot", AsyncMock(return_value=None)
    )

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False
    )

    assert result.daily_mode == "snapshot"
    assert "d" not in started          # 日线阶段被快照短路
    assert "15m" in started and "60m" in started  # 分钟阶段本轮保持不动
    assert result.period_counts["d"] == 0


@pytest.mark.asyncio
async def test_daily_snapshot_provider_failure_falls_back_to_legacy_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service,
        "_refresh_daily_from_market_snapshot",
        AsyncMock(side_effect=SnapshotProviderError("eastmoney down")),
    )

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False
    )

    assert result.daily_mode == "legacy_fallback"   # 不可静默退回
    assert "d" in started                            # 逐股日线仍然执行


@pytest.mark.asyncio
async def test_daily_snapshot_disabled_kwarg_uses_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service,
        "_refresh_daily_from_market_snapshot",
        AsyncMock(side_effect=AssertionError("snapshot path must be disabled")),
    )

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False
    )

    assert result.daily_mode is None
    assert "d" in started


# ===========================================================================
# 9. 快照规模防护 + DB 真成功判定 owner
# ===========================================================================


def test_snapshot_sanity_skips_ratio_check_for_small_universe() -> None:
    """小基数/空库不适用比例校验（否则新库首跑必然误判）。"""
    refresh_mod.check_snapshot_universe_sanity(["600519"], 10)
    refresh_mod.check_snapshot_universe_sanity([], 0)


def test_snapshot_sanity_allows_normal_ipo_delist_churn() -> None:
    """正常 IPO/退市带来的少量差异不得触发 fail-closed（98% 覆盖）。"""
    refresh_mod.check_snapshot_universe_sanity(
        [f"600{i:03d}" for i in range(4900)], 5000
    )


def test_snapshot_sanity_fails_closed_on_suspiciously_small_snapshot() -> None:
    """接口筛选失效只返回 200 只 / 存在 5000 只 → 必须 fail-closed。

    否则集合差会算出 4800 只缺口，全部推入 historical fallback（fallback storm）。
    """
    with pytest.raises(SnapshotProviderError, match="suspiciously small"):
        refresh_mod.check_snapshot_universe_sanity(
            [f"600{i:03d}" for i in range(200)], 5000
        )


@pytest.mark.asyncio
async def test_has_daily_bar_true_only_when_row_exists() -> None:
    iid = uuid.uuid4()
    found = await refresh_mod.has_daily_bar(
        _ScriptedSession(scalar_queue=[iid]), iid, TRADE_DATE  # type: ignore[arg-type]
    )
    assert found is True
    missing = await refresh_mod.has_daily_bar(
        _ScriptedSession(scalar_queue=[None]), iid, TRADE_DATE  # type: ignore[arg-type]
    )
    assert missing is False


@pytest.mark.asyncio
async def test_count_daily_bars_handles_range_and_empty() -> None:
    iid = uuid.uuid4()
    assert (
        await refresh_mod.count_daily_bars(
            _ScriptedSession(scalar_queue=[3]), iid, TRADE_DATE, TRADE_DATE  # type: ignore[arg-type]
        )
        == 3
    )
    assert (
        await refresh_mod.count_daily_bars(
            _ScriptedSession(scalar_queue=[None]), iid  # type: ignore[arg-type]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_count_active_a_share_instruments_uses_stock_filter() -> None:
    session = _ScriptedSession(scalar_queue=[4321])
    assert await refresh_mod.count_active_a_share_instruments(session) == 4321  # type: ignore[arg-type]
    sql = _sql_literals(session.scalar_statements[0])
    assert "status" in sql
    assert "920[0-9]{3}" in sql


# ===========================================================================
# 10. 日线连续性扫描（整日空洞）
# ===========================================================================


def test_plan_daily_repair_classifies_market_wide_vs_sparse() -> None:
    wide = refresh_mod.DailyGap(
        trade_date=date(2026, 9, 10), covered=0, eligible=1000,
        coverage=0.0, missing_count=1000, is_total_gap=True,
    )
    assert refresh_mod.plan_daily_repair(wide).mode == "market_wide_gap"

    sparse = refresh_mod.DailyGap(
        trade_date=date(2026, 9, 11), covered=990, eligible=1000,
        coverage=0.99, missing_count=10, is_total_gap=False,
    )
    assert refresh_mod.plan_daily_repair(sparse).mode == "sparse_symbol_gap"

    # 边界：恰好 20% 即视为 market_wide（禁止偷偷发起几千次 provider 请求）
    edge = refresh_mod.DailyGap(
        trade_date=date(2026, 9, 11), covered=800, eligible=1000,
        coverage=0.80, missing_count=200, is_total_gap=False,
    )
    assert refresh_mod.plan_daily_repair(edge).missing_ratio == pytest.approx(0.2)
    assert refresh_mod.plan_daily_repair(edge).mode == "market_wide_gap"


@pytest.mark.asyncio
async def test_get_recent_expected_trade_dates_sorted_asc_from_calendar() -> None:
    session = _ScriptedSession(
        select_queue=[[date(2026, 9, 11), date(2026, 9, 9), date(2026, 9, 10)]]
    )
    got = await refresh_mod.get_recent_expected_trade_dates(
        session, date(2026, 9, 11), 3  # type: ignore[arg-type]
    )
    assert got == [date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
    sql = _sql_literals(session.select_statements[0])
    # 唯一事实源是 trading_calendar，不得自己按 weekday 判周末/节假日
    assert "trading_calendar" in sql
    assert "is_trading_day" in sql
    assert "market" in sql


@pytest.mark.asyncio
async def test_scan_daily_continuity_detects_total_gap_masked_by_max_date() -> None:
    """09-09 正常 / 09-10 全空 / 09-11 正常 —— 必须仍发现 09-10。

    max(bars_daily.trade_date) 此时是 09-11，整日空洞会被完全掩盖。
    """
    session = _ScriptedSession(
        select_queue=[[date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]],
        # eligible=1000; covered: 09-09=950 / 09-10=0 / 09-11=980
        scalar_queue=[1000, 950, 0, 980],
    )

    gaps = await refresh_mod.scan_daily_continuity(
        session, date(2026, 9, 11)  # type: ignore[arg-type]
    )

    assert [g.trade_date for g in gaps] == [date(2026, 9, 10)]
    gap = gaps[0]
    assert gap.covered == 0
    assert gap.is_total_gap is True
    assert gap.missing_count == 1000
    assert gap.coverage == 0.0


@pytest.mark.asyncio
async def test_scan_daily_continuity_reports_sparse_gap() -> None:
    session = _ScriptedSession(
        select_queue=[[date(2026, 9, 11)]],
        scalar_queue=[1000, 800],   # 80% < 90% 阈值
    )
    gaps = await refresh_mod.scan_daily_continuity(
        session, date(2026, 9, 11)  # type: ignore[arg-type]
    )
    assert len(gaps) == 1
    assert gaps[0].is_total_gap is False
    assert gaps[0].missing_count == 200


# ===========================================================================
# 11. 空 universe 闭环 / periods / 收盘窗口 / end_date 透传
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_universe_still_runs_snapshot_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 universe 为空时快照路径仍必须执行（这是新股发现的唯一入口）。"""
    service = BarsSchedulerService(fetch_processes=1)
    discovered = [SimpleNamespace(id=uuid.uuid4(), symbol="920001")]
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(side_effect=[[], discovered])
    )
    monkeypatch.setattr(prov, "can_use_same_day_eod_snapshot", lambda *a, **k: True)
    snapshot = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_refresh_daily_from_market_snapshot", snapshot)
    monkeypatch.setattr(service, "_run_post_daily_phase", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_run_serial_period", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(
        service, "_scan_daily_continuity_gate", AsyncMock(return_value=[])
    )

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert snapshot.await_count == 1, "空 universe 也必须执行快照发现"
    assert result.daily_mode == "snapshot"
    assert result.total == 1          # 重新读取后的 universe
    assert discovered[0].symbol == "920001"


@pytest.mark.asyncio
async def test_empty_universe_and_snapshot_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 universe 为空 + 快照失败 → 必须 fail-closed，不得伪装成成功空结果。"""
    service = BarsSchedulerService(fetch_processes=1)
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(service, "_get_active_instruments", AsyncMock(return_value=[]))
    monkeypatch.setattr(prov, "can_use_same_day_eod_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(
        service,
        "_refresh_daily_from_market_snapshot",
        AsyncMock(side_effect=SnapshotProviderError("eastmoney down")),
    )

    with pytest.raises(RuntimeError, match="EMPTY_UNIVERSE_AND_SNAPSHOT_UNAVAILABLE"):
        await service.refresh_all_instruments(
            TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
        )


@pytest.mark.asyncio
async def test_empty_universe_without_snapshot_keeps_legacy_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照关闭时空 universe 行为不变（不抛错），避免误伤历史回补等调用点。"""
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(service, "_get_active_instruments", AsyncMock(return_value=[]))

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False
    )

    assert result.total == 0
    assert result.daily_mode is None


@pytest.mark.asyncio
async def test_periods_default_none_keeps_all_three_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """periods=None 必须与历史行为一致：d + 15m + 60m。"""
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False
    )

    assert started == ["d", "15m", "60m"]
    assert set(result.period_counts) == {"d", "15m", "60m"}


@pytest.mark.asyncio
async def test_periods_d_only_runs_only_daily_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert started == ["d"]
    assert set(result.period_counts) == {"d"}


@pytest.mark.asyncio
async def test_after_close_periods_d_only_calls_no_minute_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AfterClose 传 periods=("d",) 时 15m/60m provider 调用次数必须为 0。

    盘后 Core 是 daily-only；分钟周期仍保留在独立 bars scheduler 与手工更新路径。
    """
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service, "_refresh_daily_from_market_snapshot", AsyncMock(return_value=None)
    )

    result = await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert result.daily_mode == "snapshot"
    assert started == [], "不得调用任何逐股 provider 阶段（含 15m/60m）"
    assert "15m" not in result.period_counts
    assert "60m" not in result.period_counts


@pytest.mark.asyncio
async def test_past_trade_date_never_uses_today_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过去交易日重跑：必须走 historical/legacy 路径，不得拿今天的快照写历史日线。"""
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    started = _patch_daily_harness(monkeypatch, service, instruments, eod_ready=False)
    monkeypatch.setattr(
        service,
        "_refresh_daily_from_market_snapshot",
        AsyncMock(side_effect=AssertionError("过去交易日不得使用当日快照")),
    )

    result = await service.refresh_all_instruments(
        date(2026, 9, 10), db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert result.daily_mode == "legacy_fallback"
    assert started == ["d"]


def test_build_provider_request_honours_explicit_end_date() -> None:
    """legacy daily 不得写死今天：重跑历史 T 必须把 end_date 设为 T。"""
    item = scheduler_module._InstrumentItem(0, uuid.uuid4(), "600519")

    req = BarsSchedulerService._build_provider_request(
        item, period="d", count=5, start_date=None, end_date=date(2026, 9, 11)
    )
    assert req["end_date"] == date(2026, 9, 11)
    assert req["start_date"] == date(2026, 9, 6)

    past = BarsSchedulerService._build_provider_request(
        item, period="d", count=5, start_date=None, end_date=date(2026, 9, 10)
    )
    assert past["end_date"] == date(2026, 9, 10)

    # 回补窗口：显式 start_date 优先
    window = BarsSchedulerService._build_provider_request(
        item, period="d", count=5, start_date=date(2023, 1, 1),
        end_date=date(2026, 9, 11),
    )
    assert window["start_date"] == date(2023, 1, 1)
    assert window["end_date"] == date(2026, 9, 11)


# ===========================================================================
# 9. periods 参数校验 / lazy pool / snapshot 计入 d 阶段
# ===========================================================================


@pytest.mark.asyncio
async def test_periods_unknown_value_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """periods=("foo",) 必须显式报错，禁止「什么都没跑但 succeeded=全市场」。"""
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    _patch_daily_harness(monkeypatch, service, instruments)

    with pytest.raises(ValueError, match="unsupported refresh periods"):
        await service.refresh_all_instruments(
            TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("foo",)
        )


@pytest.mark.asyncio
async def test_periods_empty_tuple_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BarsSchedulerService(fetch_processes=1, use_eod_snapshot=False)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    _patch_daily_harness(monkeypatch, service, instruments)

    with pytest.raises(ValueError, match="must not be empty"):
        await service.refresh_all_instruments(
            TRADE_DATE, db_session=object(), trigger_dsa=False, periods=()
        )


@pytest.mark.asyncio
async def test_snapshot_only_path_never_creates_process_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AfterClose 场景：periods=("d",) + 快照成功 → 根本不该创建 ProcessPool。

    ProcessPool 必须在真正进入 _run_parallel_period 之前才 lazy 创建，
    否则盘后每天白白 spawn 一批进程（且 snapshot 路径根本用不到）。
    """
    service = BarsSchedulerService(fetch_processes=4)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service, "_refresh_daily_from_market_snapshot", AsyncMock(return_value=None)
    )

    await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert service.last_process_metrics["pool_creations"] == 0


@pytest.mark.asyncio
async def test_snapshot_path_counts_upserted_rows_into_daily_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照成功 → period_counts["d"] 必须反映真实落库行数，不得保持 0。"""
    service = BarsSchedulerService(fetch_processes=1)
    inst_a = SimpleNamespace(id=uuid.uuid4(), symbol="600519")
    inst_b = SimpleNamespace(id=uuid.uuid4(), symbol="000001")
    session = _ScriptedSession(select_queue=[[inst_a, inst_b]])

    monkeypatch.setattr(
        prov,
        "fetch_full_a_share_snapshot",
        AsyncMock(return_value=[{"f12": "600519"}, {"f12": "000001"}]),
    )
    monkeypatch.setattr(
        prov,
        "normalize_snapshot_rows",
        lambda raw: [_snap("600519"), _snap("000001", market="SZ")],
    )
    monkeypatch.setattr(
        refresh_mod, "count_active_a_share_instruments", AsyncMock(return_value=5000)
    )
    monkeypatch.setattr(
        refresh_mod, "check_snapshot_universe_sanity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        refresh_mod,
        "sync_instruments_from_eod_snapshot",
        AsyncMock(return_value=InstrumentSyncResult()),
    )
    monkeypatch.setattr(
        refresh_mod, "upsert_raw_daily_snapshot", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        service, "_fetch_pytdx_primary_eod", AsyncMock(return_value=())
    )

    result = scheduler_module.BatchResult()
    result.period_counts["d"] = 0
    await service._refresh_daily_from_market_snapshot(  # noqa: SLF001
        TRADE_DATE, session, None, result
    )

    assert result.snapshot_upserted == 2
    assert result.period_counts["d"] == 2
    assert result.daily_missing_after_fallback == 0


# ===========================================================================
# 10. 日线连续性硬门禁
# ===========================================================================


@pytest.mark.asyncio
async def test_continuity_gap_blocks_post_daily_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """09-10 整日空洞（coverage=0）必须阻断 Core，post-daily 调用次数为 0。"""
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service, "_refresh_daily_from_market_snapshot", AsyncMock(return_value=None)
    )
    # 本用例必须走**真实**门禁实现，去掉 harness 的放行替身
    monkeypatch.delattr(service, "_scan_daily_continuity_gate", raising=False)
    gap = DailyGap(
        trade_date=date(2026, 9, 10),
        covered=0,
        eligible=5293,
        coverage=0.0,
        missing_count=5293,
        is_total_gap=True,
    )
    monkeypatch.setattr(
        refresh_mod, "scan_daily_continuity", AsyncMock(return_value=[gap])
    )
    post_daily = service._run_post_daily_phase  # 已被 harness 替身

    with pytest.raises(DailyContinuityBlockedError) as excinfo:
        await service.refresh_all_instruments(
            TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
        )

    assert excinfo.value.gaps[0].trade_date == date(2026, 9, 10)
    assert post_daily.await_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_continuity_pass_allows_post_daily_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无缺口 → 门禁放行，post-daily 正常执行一次。"""
    service = BarsSchedulerService(fetch_processes=1)
    instruments = [SimpleNamespace(id=uuid.uuid4(), symbol="600519")]
    _patch_daily_harness(monkeypatch, service, instruments)
    monkeypatch.setattr(
        service, "_refresh_daily_from_market_snapshot", AsyncMock(return_value=None)
    )
    # 真实门禁 + 空缺口 → 必须放行
    monkeypatch.delattr(service, "_scan_daily_continuity_gate", raising=False)
    monkeypatch.setattr(
        refresh_mod, "scan_daily_continuity", AsyncMock(return_value=[])
    )
    post_daily = service._run_post_daily_phase  # 已被 harness 替身

    await service.refresh_all_instruments(
        TRADE_DATE, db_session=object(), trigger_dsa=False, periods=("d",)
    )

    assert post_daily.await_count == 1  # type: ignore[attr-defined]


# ===========================================================================
# 11. 因子源健康门禁（禁止 TDX 挂掉时跑 5000+ 逐股 xdxr）
# ===========================================================================


@pytest.mark.asyncio
async def test_factor_provider_down_fails_closed_before_per_instrument_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """因子源探测失败 → 立即抛 FactorSourceUnavailableError，不得进入逐股循环。"""
    service = BarsSchedulerService(fetch_processes=1)
    unavailable = FactorProviderHealth(
        available=False,
        provider="pytdx",
        latency_seconds=1.0,
        error="calling function error",
    )
    monkeypatch.setattr(
        scheduler_module, "probe_factor_provider", AsyncMock(return_value=unavailable)
    )
    rebuild = AsyncMock(return_value={"checked": 0, "changed": 0, "rebuilt": 0, "failed": 0})
    monkeypatch.setattr(service, "_rebuild_factors_if_needed", rebuild)

    result = scheduler_module.BatchResult()
    with pytest.raises(FactorSourceUnavailableError, match="FACTOR_SOURCE_UNAVAILABLE"):
        await service._run_post_daily_phase(  # noqa: SLF001
            TRADE_DATE, [], object(), None, result, trigger_dsa=False
        )

    assert rebuild.await_count == 0
    assert result.factor_source_available is False
    assert result.factor_source_error == "calling function error"


# ===========================================================================
# 12. 新股历史补齐不得覆盖当日 canonical EOD 快照
# ===========================================================================


@pytest.mark.asyncio
async def test_new_instrument_backfill_ends_before_trade_date_when_today_bar_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T 日 bar 已由 snapshot 写入 → 历史补齐窗口必须止于 T-1（禁止覆盖 canonical）。"""
    windows: list[tuple[date, date]] = []

    async def fake_backfill_one(
        session: Any, inst: Any, start: date, end: date, **kwargs: Any
    ) -> bool:
        windows.append((start, end))
        return True

    monkeypatch.setattr(refresh_mod, "has_daily_bar", AsyncMock(return_value=True))
    monkeypatch.setattr(refresh_mod, "_backfill_one", fake_backfill_one)
    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        AsyncMock(return_value=date(2026, 9, 10)),
    )

    inst = _instrument("688001")
    inst.listing_date = date(2026, 6, 1)

    written = await refresh_mod.backfill_new_instruments(
        _ScriptedSession(), [inst], TRADE_DATE  # type: ignore[arg-type]
    )

    assert written == 1
    assert windows == [(date(2026, 6, 1), date(2026, 9, 10))]


@pytest.mark.asyncio
async def test_new_instrument_backfill_uses_trade_date_when_no_today_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T 日没有 canonical bar → 历史补齐照常补到 T。"""
    windows: list[tuple[date, date]] = []

    async def fake_backfill_one(
        session: Any, inst: Any, start: date, end: date, **kwargs: Any
    ) -> bool:
        windows.append((start, end))
        return True

    monkeypatch.setattr(refresh_mod, "has_daily_bar", AsyncMock(return_value=False))
    monkeypatch.setattr(refresh_mod, "_backfill_one", fake_backfill_one)
    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        AsyncMock(return_value=date(2026, 9, 10)),
    )

    inst = _instrument("688001")
    inst.listing_date = date(2026, 6, 1)

    await refresh_mod.backfill_new_instruments(
        _ScriptedSession(), [inst], TRADE_DATE  # type: ignore[arg-type]
    )

    assert windows == [(date(2026, 6, 1), TRADE_DATE)]


# ===========================================================================
# 13. 因子源探测绕过 Redis 缓存 + 运行中熔断集成
# ===========================================================================


class _BreakerSession:
    """最小异步 session：仅支持 breaker 路径需要的 rollback/commit。"""

    async def rollback(self) -> None:  # noqa: D401
        pass

    async def commit(self) -> None:
        pass


def test_pytdx_probe_xdxr_uncached_bypasses_redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """probe_xdxr_uncached 必须直连远端，不读/写 Redis 缓存。"""
    import pandas as pd

    from app.core.pytdx_adapter import PytdxAdapter

    adapter = PytdxAdapter()
    calls = {"uncached": 0, "cached": 0}

    def fake_fetch(self: PytdxAdapter, symbol: str) -> pd.DataFrame:
        calls["uncached"] += 1
        return pd.DataFrame([{"date": pd.Timestamp("2024-01-02"), "category": 1}])

    def fake_get(self: PytdxAdapter, symbol: str) -> pd.DataFrame:
        calls["cached"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(PytdxAdapter, "_fetch_xdxr_from_pytdx", fake_fetch)
    monkeypatch.setattr(PytdxAdapter, "get_xdxr_info", fake_get)

    df = adapter.probe_xdxr_uncached("600519")
    assert calls["uncached"] == 1
    assert calls["cached"] == 0  # 不得走缓存路径
    assert df is not None


def test_pytdx_connected_server_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.pytdx_adapter import PYTDX_SERVERS, PytdxAdapter

    class FakeApi:
        def connect(self, host: str, port: int, time_out: float | None = None) -> bool:
            return True

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr("app.core.pytdx_adapter.TdxHq_API", lambda **k: FakeApi())
    adapter = PytdxAdapter()
    adapter.connect()
    assert adapter.connected_server == (PYTDX_SERVERS[0][0], PYTDX_SERVERS[0][1])
    adapter.disconnect()
    assert adapter.connected_server is None


def test_pytdx_connect_failover_to_live_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """前三台死、第四台活时，connect 必须落到第四台（避免前 3 台假阴性）。"""
    from app.core.pytdx_adapter import PytdxAdapter

    counter = {"n": 0}

    class FakeApi:
        def __init__(self, **k: Any) -> None:
            pass

        def connect(self, host: str, port: int, time_out: float | None = None) -> bool:
            counter["n"] += 1
            if counter["n"] <= 3:
                raise RuntimeError("dead")
            return True

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr("app.core.pytdx_adapter.TdxHq_API", lambda **k: FakeApi())
    adapter = PytdxAdapter(
        servers=[("h1", 1), ("h2", 2), ("h3", 3), ("h4", 4), ("h5", 5)],
        max_retries=1,
    )
    adapter.connect()
    assert adapter.connected_server == ("h4", 4)


@pytest.mark.asyncio
async def test_probe_factor_provider_reports_uncached_and_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入 adapter：probe 必须报告 server_attempts=5、connected_server、uncached_xdxr_ok。"""
    import pandas as pd

    class _FakeProbeAdapter:
        connected_server = ("1.2.3.4", 7709)

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def probe_xdxr_uncached(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame([{"date": pd.Timestamp("2024-01-02")}])

        def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
            raise AssertionError("probe must not use cached get_xdxr_info")

    health = await scheduler_module.probe_factor_provider(adapter=_FakeProbeAdapter())
    assert health.available is True
    assert health.server_attempts == 5
    assert health.connected_server == ("1.2.3.4", 7709)
    assert health.uncached_xdxr_ok is True


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_rebuild_single_symbol_provider_failure_blocks_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任意单只 detect 抛 provider error → 立即 FACTOR_SOURCE_SYMBOL_FRESHNESS_FAILED，
    不再等待连续 N 只；后续标的不得执行。"""
    from app.services.adjustment_factor_service import (
        AdjustmentFactorService,
        CorporateActionProviderError,
    )

    service = BarsSchedulerService(fetch_processes=1)
    monkeypatch.setattr(scheduler_module, "get_pytdx_adapter", lambda: object())

    state = {"calls": 0}

    async def fake_detect(*args: Any, **kwargs: Any) -> Any:
        state["calls"] += 1
        raise CorporateActionProviderError("boom")

    monkeypatch.setattr(
        AdjustmentFactorService, "detect_company_action_change", fake_detect
    )
    # 纯单元：避免 _delete_fingerprint 真的连 Redis（无 DB/Redis 环境）
    monkeypatch.setattr(AdjustmentFactorService, "_delete_fingerprint", MagicMock())

    instruments = [
        _instrument("600519"),
        _instrument("000001"),
        _instrument("000002"),
        _instrument("000003"),
        _instrument("000004"),
    ]
    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_SOURCE_SYMBOL_FRESHNESS_FAILED"
    ):
        await service._rebuild_factors_if_needed(  # noqa: SLF001
            TRADE_DATE, instruments, _BreakerSession(), job_run_id=None
        )
    # 第一只即 fail-closed，不继续后续标的
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_rebuild_freshness_failure_after_partial_success_blocks_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前面标的成功（detect 无变化），某只抛 provider error → 立即 hard fail，
    后续标的（C）不得执行。"""
    from app.services.adjustment_factor_service import (
        AdjustmentFactorService,
        CorporateActionProviderError,
    )

    service = BarsSchedulerService(fetch_processes=1)
    monkeypatch.setattr(scheduler_module, "get_pytdx_adapter", lambda: object())

    called: list[str] = []

    async def fake_detect(
        self: Any, session: Any, instrument_id: Any, symbol: str, adapter: Any, *,
        force_refresh: bool = False, effective_as_of: Any = None,
    ) -> None:
        called.append(symbol)
        if symbol == "000001":
            raise CorporateActionProviderError("boom")
        return None  # 无变化 → 跳过重建

    monkeypatch.setattr(
        AdjustmentFactorService, "detect_company_action_change", fake_detect
    )
    # 纯单元：避免 _delete_fingerprint 真的连 Redis（无 DB/Redis 环境）
    monkeypatch.setattr(AdjustmentFactorService, "_delete_fingerprint", MagicMock())

    instruments = [
        _instrument("600519"),  # A 成功
        _instrument("000001"),  # B 失败
        _instrument("000002"),  # C 不应执行
    ]
    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_SOURCE_SYMBOL_FRESHNESS_FAILED"
    ):
        await service._rebuild_factors_if_needed(  # noqa: SLF001
            TRADE_DATE, instruments, _BreakerSession(), job_run_id=None
        )
    # A 成功、B 失败即 fail-closed；C（000002）不得执行
    assert called == ["600519", "000001"]


@pytest.mark.asyncio
async def test_run_post_daily_phase_reraises_breaker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_rebuild_factors_if_needed 抛 FactorSourceUnavailableError 必须向上传播。"""
    service = BarsSchedulerService(fetch_processes=1)
    healthy = FactorProviderHealth(
        available=True, provider="pytdx", latency_seconds=0.1, error=None
    )
    monkeypatch.setattr(
        scheduler_module, "probe_factor_provider", AsyncMock(return_value=healthy)
    )

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise FactorSourceUnavailableError("FACTOR_SOURCE_LOST_DURING_RUN")

    monkeypatch.setattr(service, "_rebuild_factors_if_needed", boom)

    result = scheduler_module.BatchResult()
    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_SOURCE_LOST_DURING_RUN"
    ):
        await service._run_post_daily_phase(  # noqa: SLF001
            TRADE_DATE, [], object(), None, result, trigger_dsa=False
        )


@pytest.mark.asyncio
async def test_run_post_daily_phase_reraises_audit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_audit_and_rebuild_factors 抛 FactorSourceUnavailableError 必须向上传播。"""
    service = BarsSchedulerService(fetch_processes=1)
    healthy = FactorProviderHealth(
        available=True, provider="pytdx", latency_seconds=0.1, error=None
    )
    monkeypatch.setattr(
        scheduler_module, "probe_factor_provider", AsyncMock(return_value=healthy)
    )
    rebuild = AsyncMock(
        return_value={"checked": 0, "changed": 0, "rebuilt": 0, "failed": 0}
    )
    monkeypatch.setattr(service, "_rebuild_factors_if_needed", rebuild)

    async def audit_boom(*args: Any, **kwargs: Any) -> Any:
        raise FactorSourceUnavailableError("FACTOR_AUDIT_UNHEALTHY")

    monkeypatch.setattr(service, "_audit_and_rebuild_factors", audit_boom)
    # 纯单元：resolve 阶段需真实 session，这里跳过（返回非空以触发 audit 阶段，
    # 验证 audit 抛 FactorSourceUnavailableError 向上传播；不验证 resolve/DSA 路径）
    monkeypatch.setattr(service, "_resolve_factor_audit_symbols", AsyncMock(return_value=["600519"]))

    result = scheduler_module.BatchResult()
    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_AUDIT_UNHEALTHY"
    ):
        await service._run_post_daily_phase(  # noqa: SLF001
            TRADE_DATE, [], object(), None, result, trigger_dsa=False
        )


@pytest.mark.asyncio
async def test_factor_audit_provider_outage_blocks_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 100% provider 错误 → FACTOR_AUDIT_UNHEALTHY，禁止继续 Core。"""
    from types import SimpleNamespace

    from app.services.factor_reconciliation import FactorReconciliationTask

    plan = SimpleNamespace(
        total_audited=5000,
        consistent_count=0,
        needs_rebuild_count=0,
        error_count=5000,
        items=[],
        degraded_count=0,
        degraded_symbols=[],
    )

    async def fake_dry_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        return plan

    monkeypatch.setattr(FactorReconciliationTask, "dry_run", fake_dry_run)

    service = BarsSchedulerService(fetch_processes=1)
    instruments = [_instrument("600519"), _instrument("000001")]
    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_AUDIT_UNHEALTHY"
    ):
        await service._audit_and_rebuild_factors(  # noqa: SLF001
            TRADE_DATE, instruments, _BreakerSession(), job_run_id=None
        )
