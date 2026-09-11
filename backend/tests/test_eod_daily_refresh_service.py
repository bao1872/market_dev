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
6. pytdx 连续失败熔断；
7. 补缺窗口（窄）与新股历史窗口（listing_date/2023-01-01）区分；
8. provider 整体失败退回 legacy 逐股路径，且不静默（daily_mode 明确）。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects.postgresql import Insert

from app.models.instrument import Instrument
from app.repositories import bar_repository as bar_repo
from app.services import bars_scheduler_service as scheduler_module
from app.services import eod_daily_refresh_service as refresh_mod
from app.services import eod_market_snapshot_provider as prov
from app.services.bars_scheduler_service import BarsSchedulerService
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
    """记录的 session：INSERT 语句被捕获，SELECT 按脚本返回行。"""

    def __init__(self, select_queue: list[list[Any]] | None = None) -> None:
        self.insert_statements: list[Any] = []
        self.select_statements: list[Any] = []
        self._select_queue = list(select_queue or [])
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        if isinstance(stmt, Insert):
            self.insert_statements.append(stmt)
            return _FakeResult([])
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
    return EodSnapshotRow(
        symbol=symbol,
        name=name,
        market=market,
        trade_date=trade_date,
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

    result = await refresh_mod.sync_instruments_from_eod_snapshot(session, rows)  # type: ignore[arg-type]

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
        session, [_snap("600519", name="贵州茅台", market="SH")]  # type: ignore[arg-type]
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
        session, [_snap("600519", name="贵州茅台", market="SH")]  # type: ignore[arg-type]
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
        session, [_snap("920001", market="BJ")]  # type: ignore[arg-type]
    )

    assert existing.status == "active"


@pytest.mark.asyncio
async def test_universe_dedups_repeated_symbol() -> None:
    session = _ScriptedSession([[], [_instrument("600519")]])

    result = await refresh_mod.sync_instruments_from_eod_snapshot(
        session, [_snap("600519"), _snap("600519")]  # type: ignore[arg-type]
    )

    assert result.new_symbols == ["600519"]


@pytest.mark.asyncio
async def test_universe_empty_snapshot_is_noop() -> None:
    session = _ScriptedSession()
    result = await refresh_mod.sync_instruments_from_eod_snapshot(session, [])  # type: ignore[arg-type]
    assert result.new_symbols == []
    assert session.select_statements == []
    assert session.insert_statements == []


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
    import pandas as pd

    calls: list[str] = []

    async def fake_refresh(session: Any, instrument_id: Any, start: Any, end: Any, adapter: Any = None) -> Any:
        calls.append("pytdx")
        return pd.DataFrame({"close": [1.0]})

    async def fake_em(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        calls.append("eastmoney")
        return []

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession()
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is True
    assert calls == ["pytdx"]


@pytest.mark.asyncio
async def test_backfill_falls_back_to_eastmoney_insert_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytdx 失败 → Eastmoney fqt=0，且落库必须 insert-only（不覆盖既有行）。"""

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pytdx 拉取失败 calling function error")

    captured: dict[str, Any] = {}

    async def fake_em(client: Any, symbol: str, market: str, start: Any, end: Any) -> Any:
        captured["symbol"] = symbol
        captured["market"] = market
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession()
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("000001", market="SZ"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is True
    assert captured == {"symbol": "000001", "market": "SZ"}
    assert len(session.insert_statements) == 1
    sql = _sql(session.insert_statements[0])
    # insert-only：绝不覆盖既有行的 OHLCV / adj_factor
    assert "ON CONFLICT (instrument_id, trade_date) DO NOTHING" in sql
    assert "DO UPDATE SET" not in sql


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
async def test_backfill_empty_pytdx_does_not_count_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytdx 返回空 DataFrame 不算成功（必须继续 fallback）。"""
    import pandas as pd

    async def fake_refresh(*args: Any, **kwargs: Any) -> Any:
        return pd.DataFrame()

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        return _kline_records()

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)
    monkeypatch.setattr(prov, "fetch_eastmoney_daily_kline", fake_em)
    monkeypatch.setattr(refresh_mod.httpx, "AsyncClient", _DummyAsyncClient)

    session = _ScriptedSession()
    ok = await refresh_mod._backfill_one(  # noqa: SLF001
        session, _instrument("600519"), date(2026, 9, 1), TRADE_DATE  # type: ignore[arg-type]
    )

    assert ok is True
    assert len(session.insert_statements) == 1


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
    """熔断打开后必须直接走 Eastmoney，不再为每只股票付出 pytdx 连接代价。"""
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
# 7. 补缺窗口 vs 新股窗口
# ===========================================================================


@pytest.mark.asyncio
async def test_fill_missing_uses_narrow_lookback_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    windows: list[tuple[date, date]] = []

    async def fake_refresh(session: Any, iid: Any, start: Any, end: Any, adapter: Any = None) -> Any:
        windows.append((start, end))
        return pd.DataFrame({"close": [1.0]})

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)

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

    monkeypatch.setattr(bar_repo, "refresh_daily_bars", fake_refresh)

    with_listing = _instrument("920001", market="BJ")
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
    monkeypatch: pytest.MonkeyPatch, service: BarsSchedulerService, instruments: list[Any]
) -> list[str]:
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    started: list[str] = []

    async def fake_serial(_instruments: Any, *, period: str, **kwargs: Any) -> tuple[int, int]:
        started.append(period)
        return 0, 0

    async def fake_parallel(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("parallel path should not run for workers=1")

    monkeypatch.setattr(service, "_run_serial_period", fake_serial)
    monkeypatch.setattr(service, "_run_parallel_period", fake_parallel)
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
