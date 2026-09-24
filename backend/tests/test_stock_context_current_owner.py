"""stock_context CURRENT consumer 收敛 + recentChanges lineage 单元测试。

[PANJI-CURRENT-CORE-OWNER-REGRESSION-01-R1] 这些测试锁定：
- A/B/C: /context 与 /admin/stocks/debug 的 CURRENT Core identity 统一走唯一 owner
  ``resolve_current_core_run``（canonical after-close CoreRun），不再有第二套
  legacy stock_core FactorPublication 查询；
- D: recentChanges（_find_recent_canonical_snapshots）与 owner 同一 canonical lineage，
  不再以 published_at 作为 CURRENT readiness gate，因此不会因 published_at=NULL 停在 08-26。

均为纯单元（mock session / 打桩 owner 与下游 helper），不连库；PG 真实行为见
test_current_core_run_resolver_pg.py。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy.dialects.postgresql import dialect as pg_dialect

from app.api import stock_context as sc


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def _make_canonical_run(
    trade_date: date,
    run_id: str = "canonical-run",
    finished_at: datetime | None = None,
    published_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        trade_date=trade_date,
        run_type="after_close",
        status="succeeded",
        schema_version=6,
        finished_at=finished_at or datetime(2026, 9, 24, 15, 0, tzinfo=UTC),
        published_at=published_at,
        started_at=None,
    )


def _make_instrument() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="active")


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.degraded_reasons = []
    snap.structural_payload = {}
    snap.temporal_payload = {}
    snap.summary_payload = {}
    snap.source_primary_bar_time = None
    snap.source_secondary_bar_time = None
    return snap


# --------------------------------------------------------------------------- #
# 打桩 fixture：屏蔽 owner 之外的所有 DB / 下游 helper
# （真实 resolve_current_core_run 由 PG 测试覆盖；此处只验证 consumer 是否委托它）
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def patched_helpers():
    snap = _make_snapshot()
    instrument = _make_instrument()
    with patch.object(sc, "resolve_current_core_run", new=AsyncMock()) as m_run, \
         patch.object(
             sc, "_get_instrument_by_symbol", new=AsyncMock(return_value=instrument)
         ) as m_inst, \
         patch.object(
             sc, "_get_snapshot_for_instrument", new=AsyncMock(return_value=(snap, None))
         ) as m_snap, \
         patch.object(
             sc, "_find_recent_canonical_snapshots", new=AsyncMock(return_value=[])
         ) as m_recent, \
         patch.object(
             sc, "compute_atomic_facts",
             new=MagicMock(return_value={"core": {}, "auxiliary": [], "availability": {"warnings": []}}),
         ) as m_af, \
         patch.object(
             sc, "compute_product_observations",
             new=MagicMock(return_value={"structure": []}),
         ) as m_po:
        yield {
            "run": m_run, "inst": m_inst, "snap": m_snap, "recent": m_recent,
            "af": m_af, "po": m_po, "snapshot": snap, "instrument": instrument,
        }


# --------------------------------------------------------------------------- #
# A. /context 消费 canonical run（非 stale pointer）
# --------------------------------------------------------------------------- #
async def test_current_owner_consumes_canonical_run(patched_helpers) -> None:
    run = _make_canonical_run(date(2026, 9, 24), run_id="r-0924")
    patched_helpers["run"].return_value = run

    session = AsyncMock()
    result = await sc._build_stock_context(session, "600519", as_of=None, include_raw=False)

    # 1) 消费的是 owner 返回的 canonical run（09-24），而非 legacy 08-26 pointer
    assert result["asOf"] == "2026-09-24"
    # 2) consumer 确实委托了唯一 owner（统一入口），而非内联第二套查询
    patched_helpers["run"].assert_awaited_once_with(session, as_of=None)
    # 3) 无 canonical recent snapshot 时 latestChangesAsOf 为 None
    assert result["latestChangesAsOf"] is None


# --------------------------------------------------------------------------- #
# B. as_of PIT：consumer 把 as_of 透传给 owner，由 owner 严格 PIT
# --------------------------------------------------------------------------- #
async def test_asof_pit(patched_helpers) -> None:
    def _side(session, as_of):
        return _make_canonical_run(as_of, run_id=f"r-{as_of}")

    patched_helpers["run"].side_effect = _side

    session = AsyncMock()
    result = await sc._build_stock_context(
        session, "600519", as_of=date(2026, 9, 23), include_raw=False
    )

    assert result["asOf"] == "2026-09-23"
    patched_helpers["run"].assert_awaited_once_with(session, as_of=date(2026, 9, 23))


# --------------------------------------------------------------------------- #
# C. admin debug 的 rawDebug.runId 必须与同一 owner 返回的 run 一致
# --------------------------------------------------------------------------- #
async def test_admin_debug_runid_matches_owner(patched_helpers) -> None:
    run = _make_canonical_run(date(2026, 9, 24), run_id="fp-run-0924")
    patched_helpers["run"].return_value = run

    session = AsyncMock()
    result = await sc._build_stock_context(
        session, "600519", as_of=None, include_raw=True
    )

    assert result["rawDebug"]["runId"] == "fp-run-0924"
    assert result["asOf"] == "2026-09-24"


# --------------------------------------------------------------------------- #
# D1. recentChanges 查询：移除 published_at gate，使用 canonical lineage
# --------------------------------------------------------------------------- #
async def test_recent_changes_no_published_at_gate() -> None:
    snap23 = SimpleNamespace(
        trade_date=date(2026, 9, 23), structural_payload={}, temporal_payload={}
    )
    snap24 = SimpleNamespace(
        trade_date=date(2026, 9, 24), structural_payload={}, temporal_payload={}
    )

    captured: dict = {}
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [snap24, snap23]

    session = AsyncMock()

    async def _exec(stmt):
        captured["stmt"] = stmt
        return mock_result

    session.execute = AsyncMock(side_effect=_exec)

    rows = await sc._find_recent_canonical_snapshots(
        session, uuid.uuid4(), limit=2, as_of=None
    )

    sql = str(captured["stmt"].compile(dialect=pg_dialect()))
    # RC3 核心：不得再以 published_at 作为 CURRENT readiness gate
    assert "published_at" not in sql, (
        "recentChanges 不得再以 published_at 作为 CURRENT readiness gate"
    )
    # canonical lineage 谓词必须存在
    assert "finished_at" in sql, "缺少 finished_at IS NOT NULL 谓词"
    assert "run_type" in sql, "缺少 after_close run_type 谓词"
    # 返回升序（compute_recent_changes 要求）
    assert [r["trade_date"] for r in rows] == ["2026-09-23", "2026-09-24"]


# --------------------------------------------------------------------------- #
# D2. consumer 端 recentChanges 跟随 canonical snapshots（不被 08-26 published 卡住）
# --------------------------------------------------------------------------- #
async def test_recent_changes_consumer_follows_canonical(patched_helpers) -> None:
    run = _make_canonical_run(date(2026, 9, 24), run_id="r-0924")
    patched_helpers["run"].return_value = run
    patched_helpers["recent"].return_value = [
        {"trade_date": "2026-09-23", "structural_payload": {}, "temporal_payload": {}},
        {"trade_date": "2026-09-24", "structural_payload": {}, "temporal_payload": {}},
    ]

    session = AsyncMock()
    result = await sc._build_stock_context(
        session, "600519", as_of=None, include_raw=False
    )

    # 主 run 是 canonical 09-24
    assert result["asOf"] == "2026-09-24"
    # recentChanges 跟随 canonical 09-23/09-24，而非 legacy published 08-26
    assert result["latestChangesAsOf"] == "2026-09-24"
    assert result["latestChangesFrom"] == "2026-09-23"
