"""特征快照新旧实现一致性 + compute 只读 + write 原子性 + 批量查询边界测试。

用法（需测试库，禁止连接生产）：
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@127.0.0.1:5433/bz_stock_test \
        python -m pytest tests/test_feature_snapshot_consistency.py -q

设计要点（对应盘后快照性能改造 Commit 2）：
1. 固定业务时钟，避免测试随时间增长/变慢，且禁止触达外部行情：
   - TRADE_DATE=2026-06-25（过去交易日，所有 bars 均为已完成）
   - FROZEN_NOW=2026-06-25 16:00 Asia/Shanghai
   - monkeypatch mdas.now_shanghai / shanghai_business_date 为冻结值；
   - patch mdas._call_expected_last_completed_daily_bar 异步返回 TRADE_DATE；
   - patch mdas.fetch_daily_bars 为 AsyncMock(side_effect=AssertionError)，任何
     pytdx 回退立即失败（不等待网络）；
   - patch mdas._cache_get/_cache_set 绕过 Redis 短缓存。
   => OLD(get_bars) 与 NEW(load_bars_for_instruments) 均为 DB-only，等价可比。
2. 比较忽略 DB 生成字段（id/created_at/updated_at）；datetime 统一规范为
   naive 按 Asia/Shanghai 解释 -> UTC ISO；structural/temporal/summary payload、
   source times、degraded_reasons 必须深比较一致。
3. 四类关键验证：
   - 旧/新 10 股逐字段深比较（正常/日线不足/缺 15m/复权变化）；
   - compute 只读（外部不可见 + monkeypatch write 函数，compute 调用即失败）；
   - write 原子性（成功 commit 后一次性可见；第 2 批人为失败 rollback 后 0 新数据）；
   - 批量查询边界（loader_batch_size=20，查询次数为常数、不逐股调 MDAS/pytdx）
     + 50 股 compute-only smoke（记录耗时/RSS 增量，仅要求 RSS<1800 且无失控增长）。
"""

from __future__ import annotations

import json
import resource
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services import feature_snapshot_service as svc
from app.services import market_data_aggregation_service as mdas

# 业务交易日（过去日期，保证所有 bars 均为“已完成”，不被 _filter_unfinished 过滤）
TRADE_DATE = date(2026, 6, 25)
# 冻结业务时钟：收盘后，使 _resolve_date_range / _finalize_bars 均指向 TRADE_DATE
_FROZEN_NOW = datetime(2026, 6, 25, 16, 0, 0, tzinfo=svc._SHANGHAI_TZ)

# 15m 交易日内的 16 个 15 分钟槽（与 Node Cluster 15m=16 bars/日 对齐）
_M15_SLOTS = [
    dtime(9, 30), dtime(9, 45), dtime(10, 0), dtime(10, 15), dtime(10, 30), dtime(10, 45),
    dtime(11, 0), dtime(11, 15), dtime(13, 0), dtime(13, 15), dtime(13, 30), dtime(13, 45),
    dtime(14, 0), dtime(14, 15), dtime(14, 30), dtime(14, 45),
]


def _business_days(end: date, n: int) -> list[date]:
    """返回以 end 结尾（含）的最近 n 个营业日（周一~周五）。"""
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _make_instruments(n: int) -> list[Instrument]:
    """创建 n 个活跃 A 股 Instrument（6 位代码）。

    symbol 由 uuid 派生（mod 1e6），保证跨运行唯一，避免测试库残留数据触发
    instruments_symbol_key 唯一约束冲突。
    """
    out: list[Instrument] = []
    for i in range(n):
        iid = uuid.uuid4()
        symbol = f"{iid.int % 1_000_000:06d}"
        out.append(
            Instrument(
                id=iid,
                symbol=symbol,
                name=f"测试{i:03d}",
                market="SH",
                status="active",
            )
        )
    return out


async def _seed_market_data(
    db,
    instruments: list[Instrument],
    *,
    daily_count: int = 200,
    m15_days: int = 15,
    last_daily_adj: Decimal = Decimal("1.0"),
) -> None:
    """为每个 instrument 注入 daily_count 根日线 + m15_days 天 × 16 根 15m 线。

    日线最后日期必须等于 TRADE_DATE（保证 point-in-time 截断到交易日当天）。
    last_daily_adj 仅作用于最后一根日线，用于构造复权因子变化场景。
    db 为 AsyncSession，所有 IO 必须 await。
    """
    daily_rows: list[dict] = []
    m15_rows: list[dict] = []
    daily_dates = _business_days(TRADE_DATE, daily_count)
    m15_dates = _business_days(TRADE_DATE, m15_days)

    for inst in instruments:
        for j, d in enumerate(daily_dates):
            adj = last_daily_adj if d == TRADE_DATE else Decimal("1.0")
            close = Decimal(f"{10.0 + (j % 50) * 0.1:.2f}")
            daily_rows.append(
                {
                    "instrument_id": inst.id,
                    "trade_date": d,
                    "open": close - Decimal("0.1"),
                    "high": close + Decimal("0.2"),
                    "low": close - Decimal("0.2"),
                    "close": close,
                    "volume": Decimal("1000000.0"),
                    "amount": close * Decimal("1000000.0"),
                    "adj_factor": adj,
                }
            )
        for d in m15_dates:
            for slot in _M15_SLOTS:
                close = Decimal(f"{10.0 + (d.day % 30) * 0.1:.2f}")
                m15_rows.append(
                    {
                        "instrument_id": inst.id,
                        "trade_time": datetime.combine(d, slot),
                        "open": close - Decimal("0.05"),
                        "high": close + Decimal("0.1"),
                        "low": close - Decimal("0.1"),
                        "close": close,
                        "volume": Decimal("100000.0"),
                        "amount": close * Decimal("100000.0"),
                        "adj_factor": Decimal("1.0"),
                    }
                )

    if daily_rows:
        await db.execute(pg_insert(BarDaily), daily_rows)
    if m15_rows:
        await db.execute(pg_insert(Bar15Min), m15_rows)
    await db.flush()


# =============================================================================
# 冻结业务时钟 + 禁用外部行情/缓存（autouse）
# =============================================================================


@pytest_asyncio.fixture(autouse=True)
async def freeze_market_clock(monkeypatch):
    """冻结盘后快照计算所需的业务时钟，并禁止触达 pytdx / Redis。

    - now_shanghai / shanghai_business_date -> 冻结到 TRADE_DATE 收盘后；
    - _call_expected_last_completed_daily_bar -> 异步返回 TRADE_DATE（need_tail=False）；
    - fetch_daily_bars -> AsyncMock 立即抛 AssertionError（禁止任何 pytdx 回退）；
    - _cache_get / _cache_set -> 绕过 Redis 短缓存。
    """

    def _fake_now() -> datetime:
        return _FROZEN_NOW

    async def _fake_expected_last_completed(session, now: datetime) -> date:
        return TRADE_DATE

    async def _fail_pytdx(*args, **kwargs):
        raise AssertionError("test must not call pytdx fetch_daily_bars")

    def _cache_get_none(cache_key: str):
        return None

    def _cache_set_none(cache_key: str, result, ttl=None):
        return None

    monkeypatch.setattr(mdas, "now_shanghai", _fake_now)
    monkeypatch.setattr(mdas, "shanghai_business_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        mdas, "_call_expected_last_completed_daily_bar", _fake_expected_last_completed
    )
    monkeypatch.setattr(mdas, "fetch_daily_bars", AsyncMock(side_effect=_fail_pytdx))
    monkeypatch.setattr(mdas, "_cache_get", _cache_get_none)
    monkeypatch.setattr(mdas, "_cache_set", _cache_set_none)
    yield


# =============================================================================
# 比较/规范化工具
# =============================================================================


def _to_py(o):
    """递归把 numpy 类型转 python 原生，并把 NaN/inf 归一成 None（两侧一致）。"""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        v = float(o)
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return [_to_py(x) for x in o.tolist()]
    if isinstance(o, dict):
        return {k: _to_py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_py(v) for v in o]
    return o


def _dt_iso(v) -> str | None:
    """datetime 规范：naive 按 Asia/Shanghai 解释，随后转 UTC ISO。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, pd.Timestamp):
        dt = v.to_pydatetime()
    elif hasattr(v, "isoformat"):
        dt = pd.Timestamp(v).to_pydatetime()
    else:
        return str(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=svc._SHANGHAI_TZ)
    return dt.astimezone(UTC).isoformat()


def _canon(obj) -> str:
    """将 payload 规范化为可比较字符串（处理 datetime/float/None/numpy）。"""
    return json.dumps(_to_py(obj), sort_keys=True, ensure_ascii=False, default=str)


def _payload_signature(row_or_record) -> dict:
    """抽取可比较的特征字段签名（排除 id / created_at / updated_at 等 DB 生成字段）。"""
    if isinstance(row_or_record, StockFeatureSnapshot):
        return {
            "primary_timeframe": row_or_record.primary_timeframe,
            "secondary_timeframe": row_or_record.secondary_timeframe,
            "adj": row_or_record.adj,
            "schema_version": row_or_record.schema_version,
            "source_primary_bar_time": _dt_iso(row_or_record.source_primary_bar_time),
            "source_secondary_bar_time": _dt_iso(row_or_record.source_secondary_bar_time),
            "degraded_reasons": _to_py(row_or_record.degraded_reasons),
            "structural_payload": _canon(row_or_record.structural_payload),
            "temporal_payload": _canon(row_or_record.temporal_payload),
            "summary_payload": _canon(row_or_record.summary_payload),
        }
    # records 为 compute_records_for_trade_date 产出的 dict
    return {
        "primary_timeframe": row_or_record["primary_timeframe"],
        "secondary_timeframe": row_or_record["secondary_timeframe"],
        "adj": row_or_record["adj"],
        "schema_version": row_or_record["schema_version"],
        "source_primary_bar_time": _dt_iso(row_or_record["source_primary_bar_time"]),
        "source_secondary_bar_time": _dt_iso(row_or_record["source_secondary_bar_time"]),
        "degraded_reasons": _to_py(row_or_record["degraded_reasons"]),
        "structural_payload": _canon(row_or_record["structural_payload"]),
        "temporal_payload": _canon(row_or_record["temporal_payload"]),
        "summary_payload": _canon(row_or_record["summary_payload"]),
    }


async def _old_snapshots(db, instrument_ids: list[uuid.UUID]) -> dict[uuid.UUID, StockFeatureSnapshot]:
    """旧路径：compute_for_trade_date（DB-only 加载 -> 计算 -> upsert）后回查。"""
    await svc.compute_for_trade_date(db, TRADE_DATE, instrument_ids, failure_threshold=1.0)
    rows = (
        await db.execute(
            select(StockFeatureSnapshot).where(
                StockFeatureSnapshot.trade_date == TRADE_DATE,
                StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            )
        )
    ).scalars().all()
    return {r.instrument_id: r for r in rows}


async def _new_records(db, instrument_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """新路径：compute_records_for_trade_date 产出 records（不写库）。"""
    records, _stats = await svc.compute_records_for_trade_date(db, TRADE_DATE, instrument_ids)
    return {r["instrument_id"]: r for r in records}


async def _run_old_vs_new(db_session, instruments: list[Instrument], **seed_kwargs) -> None:
    """对给定 seed 配置，验证旧/新路径产出逐字段一致。"""
    db_session.add_all(instruments)
    await db_session.flush()
    await _seed_market_data(db_session, instruments, **seed_kwargs)
    ids = [i.id for i in instruments]

    old_map = await _old_snapshots(db_session, ids)
    new_map = await _new_records(db_session, ids)

    assert set(old_map.keys()) == set(new_map.keys()), (
        f"新旧路径覆盖的 instrument 不一致: old={len(old_map)} new={len(new_map)}"
    )
    mismatches: list[tuple] = []
    for iid in ids:
        osig = _payload_signature(old_map[iid])
        nsig = _payload_signature(new_map[iid])
        for field in osig:
            if osig[field] != nsig[field]:
                mismatches.append((str(iid)[:8], field, osig[field], nsig[field]))
    assert not mismatches, f"新旧实现存在字段不一致: {mismatches[:5]}"


# =============================================================================
# 1. 旧/新 10 股逐字段深比较（多场景）
# =============================================================================


@pytest.mark.asyncio
async def test_old_vs_new_normal_10(db_session) -> None:
    """正常数据：10 股，日线/15m 充足，逐字段一致。"""
    await _run_old_vs_new(db_session, _make_instruments(10), daily_count=200, m15_days=15)


@pytest.mark.asyncio
async def test_old_vs_new_insufficient_daily_10(db_session) -> None:
    """日线不足（<60）：两侧均产出 insufficient degraded_reasons 且字段一致。"""
    await _run_old_vs_new(db_session, _make_instruments(10), daily_count=50, m15_days=15)


@pytest.mark.asyncio
async def test_old_vs_new_missing_15m_10(db_session) -> None:
    """缺 15m：两侧均 degraded '15m: no bars'，字段一致。"""
    await _run_old_vs_new(db_session, _make_instruments(10), daily_count=200, m15_days=0)


@pytest.mark.asyncio
async def test_old_vs_new_adj_change_10(db_session) -> None:
    """复权因子变化（最后一根日线 adj_factor=2.0）：qfq 一致。"""
    await _run_old_vs_new(
        db_session, _make_instruments(10), daily_count=200, m15_days=15,
        last_daily_adj=Decimal("2.0"),
    )


# =============================================================================
# 2. compute 阶段只读（外部不可见 + write 函数守卫）
# =============================================================================


@pytest.mark.asyncio
async def test_compute_phase_external_invisible(db_session, monkeypatch) -> None:
    """compute 阶段只 SELECT：独立连接查询应看不到任何 snapshot 行；
    若 compute 误调用 upsert/bulk_upsert 立即失败（守护）。"""
    async def _guard(*a, **k):
        raise AssertionError("compute 阶段禁止调用写函数")

    monkeypatch.setattr(svc, "upsert_snapshot", _guard)
    monkeypatch.setattr(svc, "bulk_upsert_records", _guard)

    instruments = _make_instruments(20)
    db_session.add_all(instruments)
    await db_session.flush()
    await _seed_market_data(db_session, instruments)
    ids = [i.id for i in instruments]

    records, _ = await svc.compute_records_for_trade_date(db_session, TRADE_DATE, ids)
    assert len(records) == 20

    async with AsyncSessionLocal() as reader:
        cnt = (
            await reader.execute(
                select(StockFeatureSnapshot).where(
                    StockFeatureSnapshot.trade_date == TRADE_DATE,
                    StockFeatureSnapshot.instrument_id.in_(ids),
                )
            )
        ).scalars().all()
    assert len(cnt) == 0, "compute 阶段不应产生可见的 snapshot 行"


# =============================================================================
# 3. write 阶段原子性（成功一次性可见 / 失败 rollback 0 新数据）
# =============================================================================


async def _cleanup(ids: list[uuid.UUID]) -> None:
    """清理测试写入的 snapshot / bars / instruments（独立连接，真实删除）。"""
    async with AsyncSessionLocal() as cl:
        await cl.execute(
            delete(StockFeatureSnapshot).where(
                StockFeatureSnapshot.instrument_id.in_(ids)
            )
        )
        await cl.execute(delete(Bar15Min).where(Bar15Min.instrument_id.in_(ids)))
        await cl.execute(delete(BarDaily).where(BarDaily.instrument_id.in_(ids)))
        await cl.execute(delete(Instrument).where(Instrument.id.in_(ids)))
        await cl.commit()


@pytest.mark.asyncio
async def test_write_phase_commit_visible() -> None:
    """bulk_upsert + 单次 commit 后，独立连接外部可见全部写入行。"""
    ids: list[uuid.UUID] | None = None
    try:
        async with AsyncSessionLocal() as db:
            instruments = _make_instruments(20)
            db.add_all(instruments)
            await db.flush()
            await _seed_market_data(db, instruments)
            ids = [i.id for i in instruments]

            records, _ = await svc.compute_records_for_trade_date(db, TRADE_DATE, ids)
            written = await svc.bulk_upsert_records(db, records)
            assert written == 20
            await db.commit()

        async with AsyncSessionLocal() as reader:
            rows = (
                await reader.execute(
                    select(StockFeatureSnapshot).where(
                        StockFeatureSnapshot.trade_date == TRADE_DATE,
                        StockFeatureSnapshot.instrument_id.in_(ids),
                    )
                )
            ).scalars().all()
        assert len(rows) == 20, "commit 后应可见 20 行 snapshot"
    finally:
        if ids is not None:
            await _cleanup(ids)


@pytest.mark.asyncio
async def test_write_phase_failure_rollback(monkeypatch) -> None:
    """故障路径：第 2 批 bulk upsert 人为抛异常，rollback 后 0 条新数据，
    旧 published 数据（v1）仍完整可读。"""
    ids: list[uuid.UUID] | None = None
    try:
        async with AsyncSessionLocal() as db:
            instruments = _make_instruments(20)
            db.add_all(instruments)
            await db.flush()
            await _seed_market_data(db, instruments)
            ids = [i.id for i in instruments]

            # v1：原始数据 compute + upsert + commit（视为已发布）
            recs_v1, _ = await svc.compute_records_for_trade_date(db, TRADE_DATE, ids)
            await svc.bulk_upsert_records(db, recs_v1)
            await db.commit()

            # 篡改底层 bars（抬高收盘价），模拟新交易日数据变化
            for inst in instruments:
                await db.execute(
                    pg_insert(BarDaily)
                    .values(
                        instrument_id=inst.id,
                        trade_date=TRADE_DATE,
                        open=Decimal("20.0"), high=Decimal("21.0"),
                        low=Decimal("19.0"), close=Decimal("20.5"),
                        volume=Decimal("2000000.0"), amount=Decimal("41000000.0"),
                        adj_factor=Decimal("1.0"),
                    )
                    .on_conflict_do_update(
                        index_elements=["instrument_id", "trade_date"],
                        set_={
                            "open": Decimal("20.0"), "high": Decimal("21.0"),
                            "low": Decimal("19.0"), "close": Decimal("20.5"),
                            "volume": Decimal("2000000.0"), "amount": Decimal("41000000.0"),
                        },
                    )
                )
            await db.flush()

            recs_v2, _ = await svc.compute_records_for_trade_date(db, TRADE_DATE, ids)

            real_bulk = svc.bulk_upsert_records

            async def _fail_on_second_batch(session, records, *, write_batch_size=None):
                # 仅提交第 1 批（10 行，未 commit），随后人为失败
                await real_bulk(session, records[:10], write_batch_size=10)
                raise RuntimeError("injected 2nd-batch failure")

            monkeypatch.setattr(svc, "bulk_upsert_records", _fail_on_second_batch)

            with pytest.raises(RuntimeError):
                await svc.bulk_upsert_records(db, recs_v2)
            await db.rollback()

        # 独立连接读取：仅 v1 的 20 行，v2 因 rollback 不可见
        async with AsyncSessionLocal() as reader:
            rows = (
                await reader.execute(
                    select(StockFeatureSnapshot).where(
                        StockFeatureSnapshot.trade_date == TRADE_DATE,
                        StockFeatureSnapshot.instrument_id.in_(ids),
                    )
                )
            ).scalars().all()
        assert len(rows) == 20, f"rollback 后仅应保留 20 条 v1，实际 {len(rows)}"
    finally:
        if ids is not None:
            await _cleanup(ids)


# =============================================================================
# 4. 批量查询边界 + 50 股 compute-only smoke
# =============================================================================


@pytest.mark.asyncio
async def test_loader_batch_query_constant_calls(db_session, monkeypatch) -> None:
    """loader_batch_size=20：daily/15m/adj-factor 为批量查询，查询次数为常数，
    不逐股调用 MDAS.get_bars / pytdx。"""
    instruments = _make_instruments(20)
    db_session.add_all(instruments)
    await db_session.flush()
    await _seed_market_data(db_session, instruments)
    ids = [i.id for i in instruments]

    real_execute = db_session.execute
    counter = {"n": 0}

    def _wrap(counter):
        async def _counting(*a, **k):
            counter["n"] += 1
            return await real_execute(*a, **k)
        return _counting

    getbars_calls = {"n": 0}
    async def _fail_getbars(*a, **k):
        getbars_calls["n"] += 1
        raise AssertionError("compute 必须不调用 get_bars")

    pytdx_calls = {"n": 0}
    async def _fail_pytdx(*a, **k):
        pytdx_calls["n"] += 1
        raise AssertionError("compute 必须不调用 pytdx")

    monkeypatch.setattr(mdas.MarketDataAggregationService, "get_bars", _fail_getbars)
    monkeypatch.setattr(mdas, "fetch_daily_bars", _fail_pytdx)
    monkeypatch.setattr(db_session, "execute", _wrap(counter))

    await svc.compute_records_for_trade_date(db_session, TRADE_DATE, ids, loader_batch_size=20)

    assert getbars_calls["n"] == 0, "compute 不应调用 MDAS.get_bars"
    assert pytdx_calls["n"] == 0, "compute 不应调用 pytdx"
    # 20 股 / batch 20 = 1 批 -> 3 次批量查询（日线/15m/复权）
    assert counter["n"] == 3, f"应为 3 次批量查询，实际 {counter['n']}"

    # 40 股 / batch 20 = 2 批 -> 6 次批量查询，验证不随每支股票线性增长
    instruments2 = _make_instruments(40)
    db_session.add_all(instruments2)
    await db_session.flush()
    await _seed_market_data(db_session, instruments2)
    ids2 = [i.id for i in instruments2]
    counter2 = {"n": 0}
    monkeypatch.setattr(db_session, "execute", _wrap(counter2))
    await svc.compute_records_for_trade_date(db_session, TRADE_DATE, ids2, loader_batch_size=20)
    assert counter2["n"] == 6, f"40 股应为 6 次批量查询，实际 {counter2['n']}"


@pytest.mark.asyncio
async def test_compute_only_50_smoke() -> None:
    """50 股 compute-only smoke：记录耗时与 RSS 增量；
    仅要求 RSS<1800MB 且无失控增长，不做全市场外推硬门禁。"""
    ids: list[uuid.UUID] | None = None
    try:
        async with AsyncSessionLocal() as db:
            instruments = _make_instruments(50)
            db.add_all(instruments)
            await db.flush()
            await _seed_market_data(db, instruments)
            ids = [i.id for i in instruments]

            rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            t0 = time.perf_counter()
            records, stats = await svc.compute_records_for_trade_date(db, TRADE_DATE, ids)
            elapsed = time.perf_counter() - t0
            rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            await db.rollback()

        print(
            f"[SMOKE 50] elapsed={elapsed:.1f}s "
            f"rss_before={rss_before:.0f}MB rss_after={rss_after:.0f}MB "
            f"rss_delta={rss_after - rss_before:.0f}MB "
            f"records={len(records)} stats={stats}"
        )
        assert len(records) == 50
        assert rss_after < 1800, f"峰值 RSS {rss_after:.0f}MB 超过 1800MB"
        assert (rss_after - rss_before) < 1500, "RSS 增量疑似失控"
    finally:
        if ids is not None:
            await _cleanup(ids)
