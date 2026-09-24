"""system_overview 数据生产六节点真实行为测试（P0-1）。

这些测试使用 `db_session`，只在远程 `bz_stock_verify_<sha>` 验证库运行。

设计约束：共享开发库 bz_stock 含真实业务数据（近期交易日均有正式 run / 发布指针）。
`_compute_product_nodes` 的板块/第一金字塔/正式发布节点读取全局最新状态
（板块取 max trade_date、第一金字塔取最新 canonical after-close CoreRun、正式发布取 dsa_selector
最新 published run），因此本文件统一使用一个"未来合成交易日" `_FUTURE` 作为被测
trade_date：该日期在共享库中不存在任何真实数据，测试自己插入的记录即为该日期的唯一
结果，从而保证断言确定性与幂等（savepoint rollback，无残留）。

覆盖（与 `test_system_overview_service.py` 中 7 个行为测试一一对应）：
- 板块节点：同一 trade_date 多 run 选最新（created_at）、批次不完整不标 ok；
- 第一金字塔：跟随最新 canonical after-close CoreRun、publication_status=not_applicable、无 canonical run → pending；
- 正式发布：非 dsa_selector 的 published run 不得冒充、dsa_selector 才 ok。

用法：
    PANJI_REMOTE_VERIFY_DB_TEST=1 APP_ENV=verification backend/.venv/bin/python -m pytest \
        backend/tests/test_system_overview_product_nodes_pg.py -q -p no:cacheprovider
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import date as _date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_overview_service import _compute_product_nodes

pytestmark = pytest.mark.postgres

# 未来合成交易日：共享开发库中不存在真实数据，保证被测节点结果由本文件插入记录唯一决定。
_FUTURE = _date(2099, 12, 31)


async def _add_board_run(
    db: AsyncSession,
    trade_date,
    *,
    expected: int,
    succeeded: int,
    coverage: float,
    status: str = "succeeded",
    created_at: datetime | None = None,
):
    """构造一条 BoardAnalysisRun 记录。"""
    from app.models.board_analysis_snapshot import BoardAnalysisRun

    run = BoardAnalysisRun(
        trade_date=trade_date,
        source_core_run_id=uuid.uuid4(),
        taxonomy_version="tax-v1",
        taxonomy_compatibility_key="qstock-board-v1",
        membership_version="mem-v1",
        algorithm_version="alg-v1",
        expected_count=expected,
        succeeded_count=succeeded,
        failed_count=max(expected - succeeded, 0),
        coverage_ratio=coverage,
        status=status,
        blockers=[],
    )
    if created_at is not None:
        run.created_at = created_at
    db.add(run)
    await db.flush()
    return run


async def _add_canonical_core_run(
    db: AsyncSession, *, trade_date, finished_at, snapshot_count=None, expected_count=None,
    schema_version=None,
):
    """构造一条 canonical after-close CoreRun（CURRENT 第一金字塔的唯一事实源）。"""
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.services.feature_snapshot_service import _SCHEMA_VERSION as _CUR

    run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type="after_close",
        status="succeeded",
        schema_version=schema_version if schema_version is not None else _CUR,
        finished_at=finished_at,
        metadata_={"scope": "full"},
        snapshot_count=snapshot_count,
        expected_count=expected_count,
        created_at=finished_at,
    )
    db.add(run)
    await db.flush()
    return run


@pytest.mark.asyncio
async def test_product_nodes_board_selects_latest_run(db_session) -> None:
    """板块节点：同一 trade_date 多个 run 时按 created_at 选最新一条。"""
    older = await _add_board_run(
        db_session, _FUTURE, expected=10, succeeded=10, coverage=0.5,
        created_at=datetime(2099, 12, 31, 10, 0, tzinfo=UTC),
    )
    newer = await _add_board_run(
        db_session, _FUTURE, expected=10, succeeded=10, coverage=1.0,
        created_at=datetime(2099, 12, 31, 12, 0, tzinfo=UTC),
    )
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    board = next(n for n in nodes if n["key"] == "board")
    assert board["run_id"] == str(newer.id)
    assert board["status"] == "ok"
    assert board["run_id"] != str(older.id)


@pytest.mark.asyncio
async def test_product_nodes_board_incomplete_batch_not_ok(db_session) -> None:
    """板块节点：批次不完整（expected>succeeded，覆盖率<0.95）→ 不能标 ok。"""
    await _add_board_run(db_session, _FUTURE, expected=10, succeeded=5, coverage=0.5)
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    board = next(n for n in nodes if n["key"] == "board")
    assert board["status"] != "ok"
    assert board["quality_gate"] == "failed"


@pytest.mark.asyncio
async def test_product_nodes_first_pyramid_reads_canonical_core(db_session) -> None:
    """第一金字塔节点：必须跟随最新 canonical after-close CoreRun（ok/passed/not_applicable）。

    同时验证 legacy stock_core FactorPublication 即使存在也不再决定 CURRENT 身份。
    """
    # 残留 legacy 发布指针（不应被消费）
    from app.models.factor_publication import FactorPublication

    stale_pub = FactorPublication(
        scope_type="market",
        scope_key="legacy",
        trade_date=_date(2026, 8, 26),
        publication_kind="stock_core",
        algorithm_version="v1",
        data_run_id=uuid.uuid4(),
        coverage_ratio=0.99,
        published_at=datetime(2026, 8, 26, 11, 0, tzinfo=UTC),
    )
    db_session.add(stale_pub)
    await db_session.flush()

    run = await _add_canonical_core_run(
        db_session, trade_date=_FUTURE,
        finished_at=datetime(2099, 12, 31, 11, 0, tzinfo=UTC),
        snapshot_count=1000, expected_count=1000,
    )
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["status"] == "ok"
    assert fp["quality_gate"] == "passed"
    assert fp["publication_status"] == "not_applicable"
    assert fp["run_id"] == str(run.id)
    assert fp["run_id"] != str(stale_pub.data_run_id), "不得消费 legacy stock_core 指针"


@pytest.mark.asyncio
async def test_product_nodes_first_pyramid_no_coverage_gate(db_session) -> None:
    """第一金字塔节点：canonical CoreRun 不论覆盖率一律 ok/passed，publication_status=not_applicable。

    覆盖率 <0.98 不再导致 attention/failed（新节点已无覆盖率门禁，也不 published）。
    """
    await _add_canonical_core_run(
        db_session, trade_date=_FUTURE,
        finished_at=datetime(2099, 12, 31, 11, 0, tzinfo=UTC),
        snapshot_count=500, expected_count=1000,  # 覆盖率 0.5
    )
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["status"] == "ok"
    assert fp["quality_gate"] == "passed"
    assert fp["publication_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_product_nodes_first_pyramid_selects_latest_canonical_run(db_session) -> None:
    """第一金字塔节点：按 trade_date DESC 取最新 canonical CoreRun（不误读较旧 run）。

    说明：shared/verify 库可能含更早的 canonical run，这里插入两条不同 trade_date 的
    canonical run，断言节点取 trade_date 更新者（order_by trade_date DESC 收敛）。
    """
    older = await _add_canonical_core_run(
        db_session, trade_date=_date(2099, 12, 30),
        finished_at=datetime(2099, 12, 30, 11, 0, tzinfo=UTC),
    )
    newer = await _add_canonical_core_run(
        db_session, trade_date=_FUTURE,
        finished_at=datetime(2099, 12, 31, 11, 0, tzinfo=UTC),
    )
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["run_id"] == str(newer.id)
    assert fp["run_id"] != str(older.id)
    assert fp["trade_date"] == _FUTURE
    assert fp["publication_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_product_nodes_publish_ignores_non_dsa_selector(
    db_session, test_selector_strategy
) -> None:
    """正式发布节点：其他策略（非 dsa_selector）的 published run 不得冒充正式发布。

    说明：共享开发库含真实 dsa_selector published run，故"无正式发布 → pending"的空态
    无法在共享库稳定断言（该空态由纯单元空库测试覆盖）。这里断言真实库里可稳定验证的
    确定性行为：插入一条非 dsa_selector 的 published run 后，publish 节点不得指向它
    （run_id 必须不是该非 dsa run 的 id），即非 dsa_selector 不参与正式发布判定。
    """
    from app.models.strategy_run import StrategyRun

    non_dsa_run = StrategyRun(
        strategy_version_id=test_selector_strategy["version"].id,
        run_type="scheduled",
        trade_date=_FUTURE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )
    db_session.add(non_dsa_run)
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    publish = next(n for n in nodes if n["key"] == "publish")
    # 非 dsa_selector 的 published run 不得被当作正式发布节点。
    assert publish["run_id"] != str(non_dsa_run.id)


@pytest.mark.asyncio
async def test_product_nodes_publish_uses_dsa_selector_run(
    db_session, dsa_selector_strategy
) -> None:
    """正式发布节点：dsa_selector 的 published run → ok/published。"""
    from app.models.strategy_run import StrategyRun

    db_session.add(StrategyRun(
        strategy_version_id=dsa_selector_strategy["version"].id,
        run_type="scheduled",
        trade_date=_FUTURE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
    ))
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, _FUTURE)
    publish = next(n for n in nodes if n["key"] == "publish")
    assert publish["status"] == "ok"
    assert publish["publication_status"] == "published"
