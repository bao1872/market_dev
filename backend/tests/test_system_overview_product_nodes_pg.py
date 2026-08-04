"""system_overview 数据生产六节点真实行为测试（P0-1，共享开发库目标测试）。

这些测试使用 `db_session`，属于真实 PostgreSQL 行为测试，必须经 SSH 隧道以
`PANJI_SHARED_DEV_DB_TEST=1` 目标模式运行（本文件整体带 `shared_dev_db` marker，
满足 conftest 对"目标测试文件全部用例带 marker"的强制要求）。

覆盖（与 `test_system_overview_service.py` 中 7 个行为测试一一对应）：
- 板块节点：同一 trade_date 多 run 选最新（created_at）、批次不完整不标 ok；
- 第一金字塔：读取 stock_core 发布指针、覆盖率 <0.98 不标 ok、无指针 pending；
- 正式发布：非 dsa_selector 的 published run 不得冒充、dsa_selector 才 ok。

用法：
    PANJI_SHARED_DEV_DB_TEST=1 PANJI_SHARED_DEV_DB_TARGET=tests/test_system_overview_product_nodes_pg.py \
        APP_ENV=development backend/.venv/bin/python -m pytest \
        backend/tests/test_system_overview_product_nodes_pg.py -q -p no:cacheprovider
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import date as _date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_overview_service import _compute_product_nodes

pytestmark = pytest.mark.shared_dev_db


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


@pytest.mark.asyncio
async def test_product_nodes_board_selects_latest_run(db_session) -> None:
    """板块节点：同一 trade_date 多个 run 时按 created_at 选最新一条。"""
    d = _date(2026, 8, 4)
    older = await _add_board_run(
        db_session, d, expected=10, succeeded=10, coverage=0.5,
        created_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    newer = await _add_board_run(
        db_session, d, expected=10, succeeded=10, coverage=1.0,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )
    nodes = await _compute_product_nodes(db_session, d)
    board = next(n for n in nodes if n["key"] == "board")
    assert board["run_id"] == str(newer.id)
    assert board["status"] == "ok"
    assert board["run_id"] != str(older.id)


@pytest.mark.asyncio
async def test_product_nodes_board_incomplete_batch_not_ok(db_session) -> None:
    """板块节点：批次不完整（expected>succeeded，覆盖率<0.95）→ 不能标 ok。"""
    d = _date(2026, 8, 4)
    await _add_board_run(db_session, d, expected=10, succeeded=5, coverage=0.5)
    nodes = await _compute_product_nodes(db_session, d)
    board = next(n for n in nodes if n["key"] == "board")
    assert board["status"] != "ok"
    assert board["quality_gate"] == "failed"


@pytest.mark.asyncio
async def test_product_nodes_first_pyramid_reads_stock_core(db_session) -> None:
    """第一金字塔节点：必须读取 stock_core 发布指针（覆盖率合格 → ok/passed）。"""
    from app.models.factor_publication import FactorPublication

    d = _date(2026, 8, 4)
    db_session.add(FactorPublication(
        scope_type="market",
        scope_key="A",
        trade_date=d,
        publication_kind="stock_core",
        algorithm_version="v1",
        data_run_id=uuid.uuid4(),
        coverage_ratio=0.99,
    ))
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, d)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["status"] == "ok"
    assert fp["quality_gate"] == "passed"
    assert fp["publication_status"] == "published"


@pytest.mark.asyncio
async def test_product_nodes_first_pyramid_low_coverage_not_ok(db_session) -> None:
    """第一金字塔节点：stock_core 覆盖率 <0.98 → attention/failed，不标 ok。"""
    from app.models.factor_publication import FactorPublication

    d = _date(2026, 8, 4)
    db_session.add(FactorPublication(
        scope_type="market",
        scope_key="A",
        trade_date=d,
        publication_kind="stock_core",
        algorithm_version="v1",
        data_run_id=uuid.uuid4(),
        coverage_ratio=0.9,
    ))
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, d)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["status"] != "ok"
    assert fp["quality_gate"] == "failed"


@pytest.mark.asyncio
async def test_product_nodes_no_stock_core_pointer_is_pending(db_session) -> None:
    """第一金字塔节点：无 stock_core 发布指针 → pending，不误读历史回补 run 为今日状态。"""
    d = _date(2026, 8, 4)
    nodes = await _compute_product_nodes(db_session, d)
    fp = next(n for n in nodes if n["key"] == "first_pyramid")
    assert fp["status"] == "pending"
    assert fp["publication_status"] == "pending"


@pytest.mark.asyncio
async def test_product_nodes_publish_ignores_non_dsa_selector(
    db_session, test_selector_strategy
) -> None:
    """正式发布节点：其他策略（非 dsa_selector）的 published run 不得冒充正式发布。"""
    from app.models.strategy_run import StrategyRun

    d = _date(2026, 8, 4)
    db_session.add(StrategyRun(
        strategy_version_id=test_selector_strategy["version"].id,
        run_type="scheduled",
        trade_date=d,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
    ))
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, d)
    publish = next(n for n in nodes if n["key"] == "publish")
    assert publish["status"] == "pending"
    assert publish["publication_status"] == "pending"


@pytest.mark.asyncio
async def test_product_nodes_publish_uses_dsa_selector_run(
    db_session, dsa_selector_strategy
) -> None:
    """正式发布节点：dsa_selector 的 published run → ok/published。"""
    from app.models.strategy_run import StrategyRun

    d = _date(2026, 8, 4)
    db_session.add(StrategyRun(
        strategy_version_id=dsa_selector_strategy["version"].id,
        run_type="scheduled",
        trade_date=d,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
    ))
    await db_session.flush()
    nodes = await _compute_product_nodes(db_session, d)
    publish = next(n for n in nodes if n["key"] == "publish")
    assert publish["status"] == "ok"
    assert publish["publication_status"] == "published"
