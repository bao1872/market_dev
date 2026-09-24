"""resolve_current_core_run 真实 PostgreSQL 契约测试（CURRENT-CORE-OWNER-REGRESSION-01-R1）。

这些测试用 ``db_session`` 在远程 ``bz_stock_verify_<sha>`` 验证库运行（pytest.mark.postgres），
覆盖合约 RC5-E 要求的真实 DB 行为：
- 同时插入 legacy stock_core pointer（08-26）+ manual/backfill/after_close-sample/
  after_close-wrong-schema/after_close-failed/canonical-T/同日 finished_at 更晚 的 run；
- 验证：stale pointer 不参与、noncanonical（manual/backfill/wrong-schema/failed）不抢占、
  同日取 finished_at 更晚者、as_of 严格 PIT。
- 不依赖 Python simulator（与 test_first_pyramid_current_core_resolution.py 的 _simulate_db 互补）。

用法（仅远程验证库）：
    PANJI_REMOTE_VERIFY_DB_TEST=1 APP_ENV=verification \\
        backend/.venv/bin/python -m pytest backend/tests/test_current_core_run_resolver_pg.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
    FactorPublication,
)
from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_AFTER_CLOSE,
    RUN_TYPE_BACKFILL,
    RUN_TYPE_MANUAL,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.current_core_run_service import resolve_current_core_run
from app.services.feature_snapshot_service import _SCHEMA_VERSION

pytestmark = pytest.mark.postgres

# 未来合成交易日：共享验证库中不存在真实数据，保证被测 run 结果由本文件插入记录唯一决定。
_STALE_DATE = date(2026, 8, 26)  # legacy stock_core pointer 停在此（生产证据）
_MANUAL = date(2099, 6, 20)
_BACKFILL = date(2099, 6, 21)
_AC_SAMPLE = date(2099, 6, 22)  # canonical after_close（as_of PIT 命中）
_AC_WRONG_SCHEMA = date(2099, 6, 23)
_AC_FAILED = date(2099, 6, 24)
_AC_T = date(2099, 6, 25)  # canonical，finished_at 15:00
_AC_T_LATER = date(2099, 6, 25)  # 同日 canonical，finished_at 16:00（期望胜出）


def _add_run(
    db,
    *,
    trade_date: date,
    run_type: str,
    status: str,
    schema_version: int,
    finished_at: datetime | None,
    scope: str = "full",
    published_at: datetime | None = None,
) -> StockFeatureSnapshotRun:
    run = StockFeatureSnapshotRun(
        id=uuid.uuid4(),
        trade_date=trade_date,
        schema_version=schema_version,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type=run_type,
        status=status,
        expected_count=10,
        snapshot_count=10,
        finished_at=finished_at,
        published_at=published_at,
        metadata_={"scope": scope},
    )
    db.add(run)
    return run


async def _seed_scenario(db) -> dict[str, StockFeatureSnapshotRun]:
    """插入 RC5-E 全部 run + legacy pointer，返回关键 run 引用。"""
    # legacy stock_core pointer 指向一个 stale run（run_type=manual，owner 不会选它）
    stale_run = _add_run(
        db, trade_date=_STALE_DATE, run_type=RUN_TYPE_MANUAL, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
    )
    await db.flush()

    # legacy pointer（owner 不读它；仅用于文档化“stale pointer 不参与”）
    db.add(
        FactorPublication(
            id=uuid.uuid4(),
            scope_type=SCOPE_TYPE_MARKET,
            scope_key="market",
            trade_date=_STALE_DATE,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
            algorithm_version="v1",
            data_run_id=stale_run.id,
            published_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
            superseded_by=None,
        )
    )

    _add_run(
        db, trade_date=_MANUAL, run_type=RUN_TYPE_MANUAL, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 20, 15, 0, tzinfo=UTC),
    )
    _add_run(
        db, trade_date=_BACKFILL, run_type=RUN_TYPE_BACKFILL, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 21, 15, 0, tzinfo=UTC),
    )
    ac_sample = _add_run(
        db, trade_date=_AC_SAMPLE, run_type=RUN_TYPE_AFTER_CLOSE, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 22, 15, 0, tzinfo=UTC),
    )
    _add_run(
        db, trade_date=_AC_WRONG_SCHEMA, run_type=RUN_TYPE_AFTER_CLOSE, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION + 1, finished_at=datetime(2099, 6, 23, 15, 0, tzinfo=UTC),
    )
    _add_run(
        db, trade_date=_AC_FAILED, run_type=RUN_TYPE_AFTER_CLOSE, status=STATUS_FAILED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 24, 15, 0, tzinfo=UTC),
    )
    ac_t = _add_run(
        db, trade_date=_AC_T, run_type=RUN_TYPE_AFTER_CLOSE, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 25, 15, 0, tzinfo=UTC),
    )
    ac_t_later = _add_run(
        db, trade_date=_AC_T_LATER, run_type=RUN_TYPE_AFTER_CLOSE, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 25, 16, 0, tzinfo=UTC),
    )
    await db.flush()
    return {
        "stale": stale_run, "ac_sample": ac_sample, "ac_t": ac_t, "ac_t_later": ac_t_later,
    }


async def test_resolver_ignores_stale_pointer_and_picks_canonical(db_session) -> None:
    refs = await _seed_scenario(db_session)

    result = await resolve_current_core_run(db_session, as_of=None)

    assert result is not None
    # 1) stale pointer（08-26）不参与
    assert result.trade_date != _STALE_DATE
    assert result.id != refs["stale"].id
    # 2) 非 canonical（manual / backfill / wrong-schema / failed）不抢占
    assert result.run_type == RUN_TYPE_AFTER_CLOSE
    assert result.status == STATUS_SUCCEEDED
    assert result.schema_version == _SCHEMA_VERSION
    # 3) 同日取 finished_at 更晚者（16:00 胜出，而非 15:00）
    assert result.trade_date == _AC_T
    assert result.id == refs["ac_t_later"].id
    assert result.finished_at == datetime(2099, 6, 25, 16, 0, tzinfo=UTC)


async def test_resolver_as_of_strict_pit(db_session) -> None:
    refs = await _seed_scenario(db_session)

    result = await resolve_current_core_run(db_session, as_of=_AC_SAMPLE)

    assert result is not None
    # PIT：只返回 trade_date <= as_of 的最新 canonical run（即 06-22 sample）
    assert result.trade_date == _AC_SAMPLE
    assert result.run_type == RUN_TYPE_AFTER_CLOSE
    # 未来的 06-25 canonical 被严格排除
    assert result.id != refs["ac_t_later"].id
    assert result.id != refs["ac_t"].id


async def test_resolver_no_canonical_returns_none(db_session) -> None:
    # 仅插入一个 noncanonical（manual）run，owner 应返回 None（fail-closed）
    _add_run(
        db_session, trade_date=_MANUAL, run_type=RUN_TYPE_MANUAL, status=STATUS_SUCCEEDED,
        schema_version=_SCHEMA_VERSION, finished_at=datetime(2099, 6, 20, 15, 0, tzinfo=UTC),
    )
    await db_session.flush()

    result = await resolve_current_core_run(db_session, as_of=None)
    assert result is None
