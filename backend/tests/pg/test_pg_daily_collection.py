"""PG formal-verify：日常采集生产合同（需注册运行时 scripts/ops/panji-verify）。

本地无 PG 时整体 skip（AGENTS.md：Local/CI DB 测试禁止）。
这些测试必须由注册运行时对 bz_stock_verify_<40-char-sha> 执行。

[fixture 生产代表性] 本文件 session 必须与生产 ``app/db.py::AsyncSessionLocal`` 同配置
（``expire_on_commit=False`` / ``autoflush=False``）：默认 ``AsyncSession(engine)`` 会在
commit 后 expire ORM 属性，异步上下文再访问 ``inst.id`` 会触发 ``MissingGreenlet``，
这不是生产行为。另：``Instrument.id`` 由 PG ``server_default=gen_random_uuid()`` 生成，
**必须 flush 后才能取得 scalar UUID**，否则用未落库的 id 构造 BarDaily 会 IntegrityError。

[fixture 事务本地化] 本文件三个测试一律 ``await session.flush()`` **不 commit**：测试内的 ``_query_daily_bars`` / ``_get_symbol`` 与测试处于同一 PG
transaction，flush 后即可真实读到；测试结束 session 关闭时未提交事务自动回滚，
fixture 不会残留到 full-closure 的 seed / E2E phase（synthetic seed universe 为
``600000..605199`` 且 symbol 唯一，残留的 60000x 测试行会撞 ``instruments_symbol_key``）。

覆盖：
- 5.1 DB complete：DB 目标范围完整 → provider 不被调用（provider=0, heavy write=0）。
      合同 owner 是 bar_repository.fetch_daily_bars（DB 优先读：_query_daily_bars 非空即返回）。
- 5.2 only D missing：仅 D 缺失 → provider 请求窗口严格 start=D, end=D（非全历史）。
      走服务真实 dispatch 的 refresh_daily_bars（_REFRESH_FUNCS["d"]），spy fetch_daily_provider_inputs 参数。
- 6. server rotation service-level：pytdx 单 host 失败对服务不可见；服务层（legacy 单标的路径）
      只调 pytdx 边界 fetch_daily_provider_inputs，从不切 Eastmoney/THS。server failure != provider failure。
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.repositories.bar_repository import fetch_daily_bars, refresh_daily_bars
from app.services.bars_fetch_worker import DailyProviderPayload

_PG_URL = os.environ.get("PANJI_VERIFY_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _PG_URL, reason="requires registered PG verify runtime (scripts/ops/panji-verify)"
)


@pytest.fixture(scope="module")
async def engine():
    eng = create_async_engine(_PG_URL, future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    # [fixture 生产代表性] 与生产 AsyncSessionLocal 完全同配置（app/db.py:42-47）：
    # expire_on_commit=False + autoflush=False。
    async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as s:
        yield s


class _RecordingPytdx:
    """记录是否被调用；被调用即 fail（用于 DB-complete 证明 provider=0）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_daily_bars(self, symbol, start, end):
        self.calls.append(("get_daily_bars", symbol, start, end))
        raise AssertionError("provider must NOT be called when DB is complete")

    def get_xdxr_info(self, symbol):
        self.calls.append(("get_xdxr_info", symbol))
        return pd.DataFrame()


async def _seed_instrument(session: AsyncSession, symbol: str) -> uuid.UUID:
    """seed active instrument 并返回 **scalar UUID**。

    ``Instrument.id`` 由 PG ``server_default=gen_random_uuid()`` 生成；未 flush 前 Python
    侧为 None。必须 flush 让 PG 回填 ID，之后只用 scalar UUID（不再依赖 commit 后的 ORM 对象）。
    """
    inst = Instrument(symbol=symbol, name="verify", market="SH", status="active")
    session.add(inst)
    await session.flush()
    inst_id = inst.id
    assert inst_id is not None, "Instrument.id 必须由 flush 从 PG server_default 取回"
    return inst_id


def _seed_bar_daily(session: AsyncSession, instrument_id: uuid.UUID, d: date) -> None:
    session.add(
        BarDaily(
            instrument_id=instrument_id, trade_date=d, open=Decimal("1"), high=Decimal("1"),
            low=Decimal("1"), close=Decimal("1"), volume=Decimal("1"),
            amount=Decimal("1"), adj_factor=Decimal("1"),
        )
    )


# ---------------------------------------------------------------------------
# 5.1 DB complete → provider 0
# ---------------------------------------------------------------------------

async def test_pg_daily_db_complete_provider_not_called(session: AsyncSession) -> None:
    """DB 目标窗口完整 → fetch_daily_bars 直接返回，pytdx provider 与 heavy write 均不被触发。

    目标窗口就取 D 本身（start=end=D，DB 确有该日）——这才是真正的「DB complete」，
    而不是「窗口内只有一根 bar」的弱断言。
    """
    inst_id = await _seed_instrument(session, "600001")
    d_complete = date(2026, 9, 1)
    _seed_bar_daily(session, inst_id, d_complete)
    await session.flush()  # fixture 事务本地：不 commit，避免残留到 seed/E2E phase

    adapter = _RecordingPytdx()
    df = await fetch_daily_bars(session, inst_id, d_complete, d_complete, adapter=adapter)
    assert not df.empty, "DB 完整时应返回既有日线"
    assert adapter.calls == [], "DB 完整时 provider 绝不应被调用"


# ---------------------------------------------------------------------------
# 5.2 only D missing → narrow request start=D, end=D
# ---------------------------------------------------------------------------

async def test_pg_daily_only_one_day_missing_narrow_request(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仅 D 缺失 → provider 请求窗口严格为 start=D, end=D（非 D-N..D 全历史）。"""
    inst_id = await _seed_instrument(session, "600002")
    d_missing = date(2026, 9, 1)
    # 种子 D-5..D-1 完整（缺的只有 D）
    for offset in range(1, 6):
        _seed_bar_daily(session, inst_id, date(2026, 8, 31) - timedelta(days=offset - 1))
    await session.flush()  # fixture 事务本地：不 commit，避免残留到 seed/E2E phase

    captured: list[tuple] = []

    def _fake_fetch(instrument_id, symbol, start_date, end_date, adapter=None):
        captured.append((instrument_id, symbol, start_date, end_date))
        # 返回空 → refresh_daily_bars 不再 persist（避免脆弱 DataFrame 构造）
        return DailyProviderPayload(
            instrument_id=instrument_id, symbol=symbol, pid=0, raw_df=pd.DataFrame(),
            xdxr_df=None, xdxr_status="empty", provider_elapsed_seconds=0.0,
            provider_calls=["get_daily_bars"],
        )

    # 服务真实 dispatch 的 _REFRESH_FUNCS["d"] 内部调用的就是 bar_repository.fetch_daily_provider_inputs
    monkeypatch.setattr(
        "app.repositories.bar_repository.fetch_daily_provider_inputs", _fake_fetch
    )

    fake_adapter = _RecordingPytdx()
    df = await refresh_daily_bars(session, inst_id, d_missing, d_missing, adapter=fake_adapter)
    assert df.empty  # 空 provider 返回 → 空（重点在请求窗口，不在落库）
    assert len(captured) == 1, "应仅请求一次"
    _inst_id, _symbol, _start, _end = captured[0]
    assert _start == d_missing and _end == d_missing, (
        f"provider 请求必须严格 start=end={d_missing}，实际 {_start}..{_end}"
    )


# ---------------------------------------------------------------------------
# 6. server rotation service-level：server failure != provider failure
# ---------------------------------------------------------------------------

async def test_pg_daily_server_failure_does_not_switch_provider(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """legacy 单标的日线路径只调 pytdx 边界 fetch_daily_provider_inputs，从不切 Eastmoney/THS。

    pytdx 单 host 失败被封装在 PytdxAdapter 内部（纯单测已证 A→B 轮转），对服务不可见；
    因此服务层不得捕获后降级 Eastmoney。spy Eastmoney 两个入口必须为 0。
    """
    inst_id = await _seed_instrument(session, "600003")
    await session.flush()  # fixture 事务本地：不 commit，避免残留到 seed/E2E phase
    d = date(2026, 9, 1)

    captured: list[tuple] = []

    def _fake_fetch(instrument_id, symbol, start_date, end_date, adapter=None):
        captured.append((instrument_id, symbol, start_date, end_date))
        return DailyProviderPayload(
            instrument_id=instrument_id, symbol=symbol, pid=0, raw_df=pd.DataFrame(),
            xdxr_df=None, xdxr_status="empty", provider_elapsed_seconds=0.0,
            provider_calls=["get_daily_bars"],
        )

    em_full: list[int] = []
    em_kline: list[int] = []

    monkeypatch.setattr(
        "app.repositories.bar_repository.fetch_daily_provider_inputs", _fake_fetch
    )
    monkeypatch.setattr(
        "app.services.eod_market_snapshot_provider.fetch_full_a_share_snapshot",
        lambda *a, **k: em_full.append(1),
    )
    monkeypatch.setattr(
        "app.services.eod_market_snapshot_provider.fetch_eastmoney_daily_kline",
        lambda *a, **k: em_kline.append(1),
    )

    fake_adapter = _RecordingPytdx()
    await refresh_daily_bars(session, inst_id, d, d, adapter=fake_adapter)

    # pytdx 边界被调用（server 路径走 pytdx）
    assert len(captured) == 1, "日线路径应走 pytdx 边界一次"
    # Eastmoney / THS 不在日线路径（server failure != provider failure）
    assert em_full == [], "正常日线路径不得调用 Eastmoney 全市场快照"
    assert em_kline == [], "正常日线路径不得调用 Eastmoney 日线 kline"
