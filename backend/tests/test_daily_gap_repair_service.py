"""[GAP-REPAIR] market-wide 单日缺口修复 owner 单元测试（纯单元，mock session / provider）。

覆盖契约：
1. 只修一个 trade_date：写入记录必须全部是目标日，绝不触碰 09-09 / 09-11；
2. ``on_conflict_do_nothing``：绝不覆盖既有行（含既有 adj_factor）；
3. **一个共享 AsyncClient**：不逐股新建 client；
4. 有界并发：在途请求数不得超过 concurrency；
5. exact-date 校验：拿不到「恰好 1 根 T 日 bar」的股票计入 failed，不写库；
6. 可重跑幂等：第二次运行只剩仍未补齐的行；
7. dry_run 只拉取不写库；
8. A/B 门禁：坏点比例超限必须抛 SourceConsistencyError，禁止写生产。
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects.postgresql import Insert

from app.services import daily_gap_repair_service as repair_mod
from app.services import eod_daily_refresh_service as refresh_mod
from app.services.daily_gap_repair_service import (
    ConsistencyReport,
    SourceConsistencyError,
    bulk_insert_raw_daily_repair,
    repair_market_wide_daily_gap,
    validate_consistency,
)
from app.services.ths_raw_daily_provider import ThsProviderError

TRADE_DATE = date(2026, 9, 10)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0] if self._rows else (0, 0, 0, 0)


class _RecordingSession:
    """记录 INSERT 的 session；不带真实 DB。"""

    def __init__(self) -> None:
        self.insert_statements: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        if isinstance(stmt, Insert):
            self.insert_statements.append(stmt)
        return _FakeResult([])

    async def commit(self) -> None:
        self.commits += 1


class _Inst:
    def __init__(self, symbol: str, market: str = "SH") -> None:
        import uuid

        self.id = uuid.uuid4()
        self.symbol = symbol
        self.market = market


def _record(day: date, *, close: float = 10.5) -> dict[str, Any]:
    return {
        "datetime": day.isoformat(),
        "open": 10.0,
        "high": close + 0.3,
        "low": 9.9,
        "close": close,
        "volume": 1000.0,
        "amount": 10000.0,
    }


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=pg_dialect.dialect()))


def _sql_literals(stmt: Any) -> str:
    return str(
        stmt.compile(dialect=pg_dialect.dialect(), compile_kwargs={"literal_binds": True})
    )


# =========================================================================
# 1. bulk insert：only target date + on_conflict_do_nothing
# =========================================================================


@pytest.mark.asyncio
async def test_bulk_insert_only_accepts_target_date_rows() -> None:
    """非目标日的记录必须被丢弃：修复 09-10 绝不允许写入 09-09 / 09-11。"""
    session = _RecordingSession()
    rows = [
        (_Inst("600519").id, _record(date(2026, 9, 9))),
        (_Inst("600519").id, _record(TRADE_DATE)),
        (_Inst("000001").id, _record(date(2026, 9, 11))),
    ]

    inserted = await bulk_insert_raw_daily_repair(session, rows, TRADE_DATE)  # type: ignore[arg-type]

    assert inserted == 1
    assert len(session.insert_statements) == 1
    literals = _sql_literals(session.insert_statements[0])
    assert "'2026-09-10'" in literals
    assert "'2026-09-09'" not in literals
    assert "'2026-09-11'" not in literals


@pytest.mark.asyncio
async def test_bulk_insert_uses_on_conflict_do_nothing() -> None:
    """绝不覆盖既有行（含 09-09/09-11 与既有 adj_factor）。"""
    session = _RecordingSession()
    rows = [(_Inst("600519").id, _record(TRADE_DATE))]

    await bulk_insert_raw_daily_repair(session, rows, TRADE_DATE)  # type: ignore[arg-type]

    sql = _sql(session.insert_statements[0])
    assert "ON CONFLICT (instrument_id, trade_date) DO NOTHING" in sql
    assert "DO UPDATE SET" not in sql
    assert session.commits == 1


@pytest.mark.asyncio
async def test_bulk_insert_rejects_structurally_invalid_rows() -> None:
    """OHLC <= 0 / high < max(o,c) / 负量额 → 跳过，不得写库。"""
    session = _RecordingSession()
    bad_price = _record(TRADE_DATE)
    bad_price["close"] = -1.0
    bad_high = _record(TRADE_DATE)
    bad_high["high"] = 1.0  # < max(open=10, close=10.5)
    bad_volume = _record(TRADE_DATE)
    bad_volume["volume"] = -5.0

    inserted = await bulk_insert_raw_daily_repair(
        session,
        [
            (_Inst("600519").id, bad_price),
            (_Inst("000001", market="SZ").id, bad_high),
            (_Inst("300750", market="SZ").id, bad_volume),
        ],
        TRADE_DATE,  # type: ignore[arg-type]
    )

    assert inserted == 0
    assert session.insert_statements == []


# =========================================================================
# 2. repair：共享 client / exact date / 有界并发 / 幂等 / dry-run
# =========================================================================


def _patch_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing_batches: list[list[_Inst]],
    *,
    eligible: int = 100,
) -> None:
    monkeypatch.setattr(
        refresh_mod,
        "count_active_a_share_instruments",
        AsyncMock(return_value=eligible),
    )
    monkeypatch.setattr(
        refresh_mod,
        "find_missing_daily_instruments",
        AsyncMock(side_effect=list(missing_batches)),
    )


def _patch_ths(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    """把同花顺 provider 替换为替身（否则测试会打真实网络）。"""
    monkeypatch.setattr(repair_mod, "fetch_ths_raw_daily", fn)


def _patch_em(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    monkeypatch.setattr(repair_mod, "fetch_eastmoney_daily_kline", fn)


def _no_em(monkeypatch: pytest.MonkeyPatch) -> None:
    """东财兜底永久失败（同花顺成功的用例里它不应被调用）。"""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("同花顺已成功，不得调用 Eastmoney 兜底")

    _patch_em(monkeypatch, _boom)


@pytest.mark.asyncio
async def test_repair_uses_single_shared_client_and_exact_target_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有请求必须复用同一个 client，且请求区间收口为 [T, T]。"""
    instruments = [_Inst(f"6005{i:02d}") for i in range(5)]
    _patch_missing(monkeypatch, [instruments, []])

    clients: list[Any] = []
    windows: list[tuple[date, date]] = []

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        clients.append(client)
        windows.append((start, end))
        return [_record(TRADE_DATE)]

    _patch_ths(monkeypatch, fake_ths)
    _no_em(monkeypatch)

    sentinel_client = object()
    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session,  # type: ignore[arg-type]
        TRADE_DATE,
        client=sentinel_client,  # type: ignore[arg-type]
        concurrency=4,
    )

    assert len({id(c) for c in clients}) == 1
    assert clients[0] is sentinel_client
    assert windows == [(TRADE_DATE, TRADE_DATE)] * 5
    assert result.requested == 5
    assert result.fetched == 5
    assert result.inserted == 5
    assert result.still_missing == 0
    assert result.verified == 5
    assert result.coverage_after == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_repair_fails_symbols_without_exactly_one_target_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拿到 0 根或多根 T 日 bar → 该股 fail，不得写库（禁止把别日数据当 T）。"""
    instruments = [_Inst("600519"), _Inst("000001", market="SZ")]
    _patch_missing(monkeypatch, [instruments, []])

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        if symbol == "600519":
            return [_record(date(2026, 9, 9))]  # 只有 09-09
        return [_record(TRADE_DATE), _record(TRADE_DATE)]  # 重复两根

    _patch_ths(monkeypatch, fake_ths)
    _no_em(monkeypatch)

    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object()  # type: ignore[arg-type]
    )

    assert result.fetched == 0
    assert result.inserted == 0
    assert set(result.failed_symbols) == {"600519", "000001"}
    assert session.insert_statements == []


@pytest.mark.asyncio
async def test_repair_bounds_inflight_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发上限必须生效（有界并发，不是无限制 gather）。"""
    instruments = [_Inst(f"6005{i:02d}") for i in range(20)]
    _patch_missing(monkeypatch, [instruments, []])

    state = {"inflight": 0, "max": 0}
    lock = asyncio.Lock()

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        async with lock:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
        await asyncio.sleep(0.005)
        async with lock:
            state["inflight"] -= 1
        return [_record(TRADE_DATE)]

    _patch_ths(monkeypatch, fake_ths)
    _no_em(monkeypatch)

    await repair_market_wide_daily_gap(
        _RecordingSession(),  # type: ignore[arg-type]
        TRADE_DATE,
        client=object(),  # type: ignore[arg-type]
        concurrency=3,
        chunk_size=5,
    )

    assert state["max"] <= 3


@pytest.mark.asyncio
async def test_repair_is_idempotent_on_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """重跑：第一次补齐后，第二次 find_missing 返回空 → 不再写任何行。"""
    instruments = [_Inst(f"6005{i:02d}") for i in range(3)]
    _patch_missing(monkeypatch, [instruments, []])  # 第二次仍返回空

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        return [_record(TRADE_DATE)]

    _patch_ths(monkeypatch, fake_ths)
    _no_em(monkeypatch)

    session = _RecordingSession()
    first = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object()  # type: ignore[arg-type]
    )
    # 第二次：missing 已为空
    monkeypatch.setattr(
        refresh_mod, "find_missing_daily_instruments", AsyncMock(return_value=[])
    )
    second = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object()  # type: ignore[arg-type]
    )

    assert first.inserted == 3
    assert second.requested == 0
    assert second.inserted == 0
    assert len(session.insert_statements) == 1  # 只有第一次写


@pytest.mark.asyncio
async def test_repair_dry_run_fetches_but_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instruments = [_Inst(f"6005{i:02d}") for i in range(3)]
    _patch_missing(monkeypatch, [instruments, instruments])

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        return [_record(TRADE_DATE)]

    _patch_ths(monkeypatch, fake_ths)
    _no_em(monkeypatch)

    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object(), dry_run=True  # type: ignore[arg-type]
    )

    assert result.dry_run is True
    assert result.fetched == 3
    assert result.inserted == 0
    assert session.insert_statements == []


# =========================================================================
# 3. A/B 一致性门禁
# =========================================================================


def test_validate_consistency_passes_on_clean_report() -> None:
    report = ConsistencyReport(
        trade_date=TRADE_DATE,
        ohlc_compared=100,
        ohlc_bad=0,
        volume_compared=100,
        volume_bad=0,
        amount_compared=100,
        amount_bad=0,
    )
    validate_consistency(report)  # 不抛


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ohlc_bad_ratio", 0.02),
        ("volume_bad_ratio", 0.05),
        ("amount_bad_ratio", 0.10),
    ],
)
def test_validate_consistency_blocks_on_bad_ratio(field: str, value: float) -> None:
    """任一坏点比例 > 1% → 必须抛 SourceConsistencyError（禁止写生产）。"""
    report = ConsistencyReport(trade_date=TRADE_DATE)
    setattr(report, field, value)

    with pytest.raises(SourceConsistencyError, match="SOURCE_CONSISTENCY_GATE_FAILED"):
        validate_consistency(report)


def test_validate_consistency_allows_ratio_at_limit() -> None:
    """恰好 1% 不阻断（阈值语义是「超过」）。"""
    report = ConsistencyReport(
        trade_date=TRADE_DATE, ohlc_compared=100, ohlc_bad=1, ohlc_bad_ratio=0.01
    )
    validate_consistency(report)


def test_amount_tolerance_requires_both_abs_and_rel_exceeded() -> None:
    """成交额坏点判定：必须「绝对差 > 1000 元」**且**「相对差 > 0.1%」同时成立。"""
    assert repair_mod._AMOUNT_ABS_TOLERANCE == Decimal("1000")
    assert repair_mod._AMOUNT_REL_TOLERANCE == Decimal("0.001")


# =========================================================================
# 4. 数据源顺序：同花顺 primary / Eastmoney fallback
# =========================================================================


@pytest.mark.asyncio
async def test_repair_prefers_ths_and_skips_eastmoney(monkeypatch: pytest.MonkeyPatch) -> None:
    """同花顺拿到目标日 bar 时，Eastmoney 兜底不得被调用。

    这条契约很重要：东财在生产出口已被 IP 级硬封，若顺序反了，
    每只股票都会先白付一次东财失败代价（并持续触发限流）。
    """
    instruments = [_Inst(f"6005{i:02d}") for i in range(3)]
    _patch_missing(monkeypatch, [instruments, []])
    calls: list[str] = []

    async def fake_ths(client: Any, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        calls.append("ths")
        return [_record(TRADE_DATE)]

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        raise AssertionError("同花顺已成功，不得调用 Eastmoney")

    _patch_ths(monkeypatch, fake_ths)
    _patch_em(monkeypatch, fake_em)

    result = await repair_market_wide_daily_gap(
        _RecordingSession(), TRADE_DATE, client=object()  # type: ignore[arg-type]
    )

    assert calls == ["ths"] * 3
    assert result.ths_fetched == 3
    assert result.eastmoney_fetched == 0


@pytest.mark.asyncio
async def test_repair_falls_back_to_eastmoney_when_ths_has_no_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同花顺空结果 → Eastmoney fqt=0 兜底，并计入 eastmoney_fetched。"""
    instruments = [_Inst("600519"), _Inst("000001", market="SZ")]
    _patch_missing(monkeypatch, [instruments, []])
    calls: list[str] = []

    async def fake_ths(*args: Any, **kwargs: Any) -> Any:
        calls.append("ths")
        return []  # 无数据

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        return [_record(TRADE_DATE)]

    _patch_ths(monkeypatch, fake_ths)
    _patch_em(monkeypatch, fake_em)

    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object()  # type: ignore[arg-type]
    )

    assert calls == ["ths", "eastmoney"] * 2
    assert result.ths_fetched == 0
    assert result.eastmoney_fetched == 2
    assert result.inserted == 2


@pytest.mark.asyncio
async def test_repair_counts_symbol_failed_when_both_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两侧都拿不到 → 计入 failed_symbols，不写库，且保留两侧错误原因。"""
    instruments = [_Inst("600519")]
    _patch_missing(monkeypatch, [instruments, [instruments]])

    async def fake_ths(*args: Any, **kwargs: Any) -> Any:
        raise ThsProviderError("ths down")

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("em down")

    _patch_ths(monkeypatch, fake_ths)
    _patch_em(monkeypatch, fake_em)

    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session, TRADE_DATE, client=object()  # type: ignore[arg-type]
    )

    assert result.fetched == 0
    assert result.failed_symbols == ["600519"]
    assert session.insert_statements == []
    assert result.error_samples
    assert "ths=" in result.error_samples[0] and "em=" in result.error_samples[0]


@pytest.mark.asyncio
async def test_repair_can_disable_eastmoney_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``use_eastmoney_fallback=False`` 时不得触碰 Eastmoney。

    生产实测缺陷：东财对出口 IP 硬封期间，每次兜底要跑满 3 轮 × 多主机重试
    （单只 10~20s）。5293 只标的里只要有几百只掉进兜底，整轮修复就从分钟级
    退化到小时级。因此兜底必须可关，且关闭后只报同花顺侧原因。
    """
    instruments = [_Inst("600519"), _Inst("000001", market="SZ")]
    _patch_missing(monkeypatch, [instruments, instruments])
    calls: list[str] = []

    async def fake_ths(*args: Any, **kwargs: Any) -> Any:
        calls.append("ths")
        return []  # 无数据

    async def fake_em(*args: Any, **kwargs: Any) -> Any:
        calls.append("eastmoney")
        raise AssertionError("兜底已关闭，不得调用 Eastmoney")

    _patch_ths(monkeypatch, fake_ths)
    _patch_em(monkeypatch, fake_em)

    session = _RecordingSession()
    result = await repair_market_wide_daily_gap(
        session,
        TRADE_DATE,
        client=object(),  # type: ignore[arg-type]
        use_eastmoney_fallback=False,
    )

    assert calls == ["ths", "ths"]
    assert result.ths_fetched == 0
    assert result.eastmoney_fetched == 0
    assert result.failed_symbols == ["600519", "000001"]
    assert session.insert_statements == []
    # 错误原因只来自同花顺侧，不含 em=
    assert result.error_samples
    assert "em=" not in result.error_samples[0]


# =========================================================================
# 5. 并发与分块参数默认值（实测最优点，防止被无意改回）
# =========================================================================


def test_default_concurrency_is_measured_optimum() -> None:
    """默认并发 3 是实测最优（2 太慢、4 触发 502 限流），不得无意改回。"""
    assert repair_mod._DEFAULT_CONCURRENCY == 3
    assert repair_mod._DEFAULT_CHUNK_SIZE == 200


def test_volume_tolerance_matches_canonical_shares_unit() -> None:
    """canonical volume 单位是「股」，容差必须是 100 股量级（而非 1 手）。"""
    assert repair_mod._VOLUME_TOLERANCE == Decimal("100")
