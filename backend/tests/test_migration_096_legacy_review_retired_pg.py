"""Migration 096 contract — retire legacy Market Review tables（真实 PostgreSQL 部分）。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- 096 执行后 11 张 legacy market_review_* / review_scope_* 表全部消失；
- factor_publications 表保留，且 market_review publication 行 = 0；
- Market Dashboard / Core / History / scheduler / board / bars / instruments 等
  共享表全部保留；
- 096 → 095 downgrade 精确重建 11 张表（含 PK / FK / CHECK / partial unique），
  再 upgrade head → 096 再次消失（完整回滚）。

============================================================================
锁安全约束（与 095 contract 一致）：
conftest 的 db_session 是 savepoint 模式；本文件所有真实 PG 操作使用
TestAsyncSessionLocal 短事务，且每次 _run_alembic() 前必须无打开连接/事务。
禁止在 db_session 内执行 Alembic DDL。
============================================================================
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = "/app/alembic.ini"

_LEGACY_11 = (
    "market_review_runs",
    "market_review_run_items",
    "market_review_scope_snapshots",
    "market_review_metric_observations",
    "market_review_signals",
    "market_review_signal_attributions",
    "market_review_signal_instruments",
    "market_review_trackings",
    "market_review_tracking_evaluations",
    "review_scope_observation_facts",
    "review_scope_composition_snapshots",
)

# 必须保留的共享 / 当前表（不得被 096 误删）
_PRESERVED = (
    "factor_publications",
    "scheduler_job_runs",
    "job_run_events",
    "market_dashboard_market_daily",
    "market_dashboard_scope_daily",
    "market_boards",
    "market_board_memberships",
    "bars_daily",
    "instruments",
    "stock_feature_snapshot_runs",
    "first_pyramid_history_runs",
    "chip_consensus_runs",
    "board_analysis_snapshots",
    "users",
)


def _run_alembic(args: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr}")


async def _fetch_all(sql: str, params: dict | None = None) -> list[tuple]:
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        rows = list(result.fetchall())
        await session.commit()
        return rows


def _pg_char_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


async def _table_names() -> set[str]:
    rows = await _fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    return {r[0] for r in rows}


async def _fk_defs(table: str) -> dict[tuple[str, str], str]:
    """{(ref_table, ondelete_code): constraint_name} for table's FKs."""
    rows = await _fetch_all(
        """
        SELECT confrelid::regclass::text AS ref_table, confdeltype
        FROM pg_constraint
        WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'f'
        """,
        {"tbl": table},
    )
    return {(r[0], _pg_char_text(r[1])) for r in rows}


async def _check_defs(table: str) -> dict[str, str]:
    rows = await _fetch_all(
        """
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'c'
        """,
        {"tbl": table},
    )
    return {r[0]: r[1] for r in rows}


# ============================================================
# A. 096 → 11 legacy 表全部消失
# ============================================================
@pytest.mark.asyncio
async def test_096_legacy_11_tables_absent_at_head() -> None:
    _run_alembic(["upgrade", "head"])
    tables = await _table_names()
    leaked = {t for t in _LEGACY_11 if t in tables}
    assert not leaked, f"096 后 11 legacy 表必须消失，实际仍存在: {leaked}"


# ============================================================
# B. factor_publications 保留且 market_review 指针 = 0
# ============================================================
@pytest.mark.asyncio
async def test_096_factor_publications_intact_no_market_review_rows() -> None:
    _run_alembic(["upgrade", "head"])
    tables = await _table_names()
    assert "factor_publications" in tables, "factor_publications 不得被删除"

    rows = await _fetch_all(
        "SELECT count(*) FROM factor_publications WHERE publication_kind = 'market_review'"
    )
    count = rows[0][0]
    assert count == 0, f"market_review publication 指针必须已清零，实际 {count}"


# ============================================================
# C. 共享 / 当前表全部保留
# ============================================================
@pytest.mark.asyncio
async def test_096_shared_tables_preserved() -> None:
    _run_alembic(["upgrade", "head"])
    tables = await _table_names()
    missing = {t for t in _PRESERVED if t not in tables}
    assert not missing, f"096 不得误删共享/当前表，缺失: {missing}"


# ============================================================
# D. 095 downgrade 精确重建 11 表（含约束）→ 096 upgrade 再次消失
# ============================================================
@pytest.mark.asyncio
async def test_096_downgrade_recreates_11_then_upgrade_removes_them() -> None:
    try:
        # 1) downgrade 到 095：11 张 legacy 表必须精确重建
        _run_alembic(["downgrade", "095_market_dashboard_projection"])
        tables = await _table_names()
        missing = {t for t in _LEGACY_11 if t not in tables}
        assert not missing, f"downgrade 095 后 11 legacy 表必须存在，缺失: {missing}"

        # 关键 FK / CHECK 约束必须随表重建
        runs_fks = await _fk_defs("market_review_runs")
        assert ("chip_consensus_runs", "n") in runs_fks, (
            f"market_review_runs 必须重建 source_chip_run_id -> chip_consensus_runs SET NULL，实际 {runs_fks}"
        )

        signals_fks = await _fk_defs("market_review_signals")
        assert ("market_review_runs", "c") in signals_fks, (
            f"market_review_signals.review_run_id -> market_review_runs 必须 CASCADE，实际 {signals_fks}"
        )
        signals_checks = await _check_defs("market_review_signals")
        # PG 会把 IN (...) 规范化为 "= ANY (ARRAY[...])"，故按 family 取值断言而非字面 IN 形式。
        assert any(
            "filter_family" in d and all(v in d for v in ("'A'", "'B'", "'C'", "'D'"))
            for d in signals_checks.values()
        ), f"market_review_signals 必须重建 filter_family A/B/C/D CHECK，实际 {signals_checks}"

        items_fks = await _fk_defs("market_review_run_items")
        assert ("market_review_runs", "c") in items_fks, (
            f"market_review_run_items.review_run_id -> market_review_runs 必须 CASCADE，实际 {items_fks}"
        )

        track_eval_fks = await _fk_defs("market_review_tracking_evaluations")
        assert ("market_review_trackings", "c") in track_eval_fks, (
            f"market_review_tracking_evaluations.tracking_id -> market_review_trackings 必须 CASCADE，实际 {track_eval_fks}"
        )
        assert ("market_review_runs", "c") in track_eval_fks, (
            f"market_review_tracking_evaluations.review_run_id -> market_review_runs 必须 CASCADE，实际 {track_eval_fks}"
        )

        obs_facts_fks = await _fk_defs("review_scope_observation_facts")
        assert ("market_review_runs", "c") in obs_facts_fks, (
            f"review_scope_observation_facts.review_run_id -> market_review_runs 必须 CASCADE，实际 {obs_facts_fks}"
        )

        comp_fks = await _fk_defs("review_scope_composition_snapshots")
        assert ("market_review_runs", "c") in comp_fks, (
            f"review_scope_composition_snapshots.review_run_id -> market_review_runs 必须 CASCADE，实际 {comp_fks}"
        )

        # market_review_metric_observations 的两个 partial unique + CHECK 必须存在
        obs_rows = await _fetch_all(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename = 'market_review_metric_observations'"
        )
        obs_index_defs = " ".join(r[1] for r in obs_rows)
        assert "source_kind = 'live'" in obs_index_defs, "缺少 live partial unique index"
        assert "source_kind = 'history_replay'" in obs_index_defs, "缺少 history_replay partial unique index"
        obs_checks = await _check_defs("market_review_metric_observations")
        # PG 会把 IN (...) 规范化为 "= ANY (ARRAY[...])"，故按取值断言。
        assert any(
            "source_kind" in d and "'live'" in d and "'history_replay'" in d
            for d in obs_checks.values()
        ), f"market_review_metric_observations 必须重建 dual_lineage CHECK，实际 {obs_checks}"

        # 2) upgrade head → 096：11 张 legacy 表再次消失
        _run_alembic(["upgrade", "head"])
        tables = await _table_names()
        leaked = {t for t in _LEGACY_11 if t in tables}
        assert not leaked, f"upgrade head 后 11 legacy 表必须再次消失，实际: {leaked}"
    finally:
        # 恢复正式 head（此刻无打开事务）
        _run_alembic(["upgrade", "head"])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
