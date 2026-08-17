"""Auction 并行 launcher 单测 — 审查报告 ref/竞价.md §4（global bar_index）收口。

覆盖 V3 计划阶段 3.4 的 5 项：
1. test_bar_range_parsing — parse_bar_range 解析正确 + 越界 fail fast
2. test_bar_range_global_index — resolve_bar_range 切片 + offset 正确；
   并通过 run_backfill 验证 _bars_loop 的 global bar_index 接线（worker2 第一根 = 31）
3. test_launcher_slice — 120 bar 切 N 区间：连续、不重叠、首尾相接、覆盖全量
4. test_launcher_run_id — 独立 run-id 生成
5. test_launcher_subprocess_cmd — 子进程参数拼装含 global --bar-range / --write-db 门

PURE 单测（不连库/不联网）：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_auction_backfill_launcher.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import pytest

_EXP_DIR = Path(__file__).resolve().parents[2] / "experiments" / "pytdx_auction_history"
if str(_EXP_DIR) not in sys.path:
    sys.path.insert(0, str(_EXP_DIR))

from auction_history_semantics_validation import SampleInstrument  # noqa: E402
import full_market_member_fact_backfill as runner_mod  # noqa: E402
from full_market_member_fact_backfill import (  # noqa: E402
    AS_OF,
    BAR_COUNT,
    PARTITION_STATUS_COMPLETED,
    _board_for_symbol,
    parse_bar_range,
    resolve_bar_range,
    run_backfill,
)
from run_backfill_parallel_launcher import (  # noqa: E402
    aggregate_worker,
    build_worker_command,
    collect_worker_outputs,
    slice_into_ranges,
    worker_run_id,
)

_TEST_SHA = "test-sha-000000000000000000000000000000000000"


def _sample(symbol, market, instrument_id=None):
    return SampleInstrument(
        symbol=symbol, market=market,
        instrument_id=instrument_id or UUID("00000000-0000-0000-0000-000000000001"),
        board=_board_for_symbol(symbol, market),
        coverage_tag="all_a_share", cohort="routine",
    )


def _obs_canon_computed():
    """正式 production row 词表（对齐 _project_member_row / project_row_to_fact）。"""
    return {
        "auction_price_raw": 10.5,
        "previous_close_raw": 10.0,
        "auction_volume_shares": 1000,
        "auction_amount": 10500.0,
        "auction_amount_source_type": "DERIVED_PRICE_X_NORMALIZED_VOLUME",
        "source_status": "TARGET_WINDOW_COMPLETE",
        "canonicalization_status": "CANONICAL",
    }


def _make_fake_run_symbol_obs(instruments):
    async def _fake(inst, trade_date):
        return _obs_canon_computed()
    return _fake


def _fixed_calendar(bar_count: int, as_of: date = AS_OF) -> list[date]:
    seq: list[date] = []
    cur = as_of
    while len(seq) < bar_count:
        if cur.weekday() < 5:
            seq.append(cur)
        cur -= timedelta(days=1)
    return sorted(seq)


def _partition_manifest(tmp_path, run_id, t):
    p = tmp_path / run_id / "bars" / t.isoformat() / "partition_manifest.json"
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. parse_bar_range：解析 + 越界 fail fast
# ---------------------------------------------------------------------------
def test_bar_range_parsing():
    assert parse_bar_range("1:30", 120) == (1, 30)
    assert parse_bar_range("31:60", 120) == (31, 60)
    assert parse_bar_range("120:120", 120) == (120, 120)


@pytest.mark.parametrize("spec", [
    "abc", "1", "1:", ":30", "a:b", "", None, 30,
])
def test_bar_range_parsing_invalid_format(spec):
    with pytest.raises(ValueError):
        parse_bar_range(spec, 120)  # type: ignore[arg-type]


def test_bar_range_parsing_fail_fast():
    with pytest.raises(ValueError):  # START < 1
        parse_bar_range("0:30", 120)
    with pytest.raises(ValueError):  # END > bar_count
        parse_bar_range("1:121", 120)
    with pytest.raises(ValueError):  # START > END
        parse_bar_range("60:30", 120)


# ---------------------------------------------------------------------------
# 2. global bar_index：resolve_bar_range 切片 + _bars_loop 接线
# ---------------------------------------------------------------------------
def test_resolve_bar_range_slice_and_offset(monkeypatch):
    fixed = _fixed_calendar(120)
    async def _fake_calendar(session, T, n):
        return fixed
    monkeypatch.setattr(runner_mod, "previous_trading_dates", _fake_calendar)

    async def _go():
        bar_dates, offset = await resolve_bar_range(
            object(), AS_OF, "31:60", BAR_COUNT)
        return bar_dates, offset

    bar_dates, offset = asyncio_run(_go())
    assert offset == 31
    assert bar_dates[0] == fixed[30]
    assert bar_dates[-1] == fixed[59]
    assert len(bar_dates) == 30


def test_resolve_bar_range_default_offset_one(monkeypatch):
    fixed = _fixed_calendar(120)
    async def _fake_calendar(session, T, n):
        return fixed
    monkeypatch.setattr(runner_mod, "previous_trading_dates", _fake_calendar)

    async def _go():
        bar_dates, offset = await resolve_bar_range(object(), AS_OF, None, BAR_COUNT)
        return bar_dates, offset

    bar_dates, offset = asyncio_run(_go())
    assert offset == 1
    assert len(bar_dates) == 120


def test_bars_loop_uses_global_bar_index(tmp_path):
    """worker2 视角：bar_index_offset=31 → 第一根 partition bar_index == 31。

    直接验证 _bars_loop 的 `enumerate(bar_dates, start=bar_index_offset)`
    接线（审查 P0 #3 死代码收口），不依赖 DB / 网络。
    """
    insts = [_sample("600000", "SH")]
    fake = _make_fake_run_symbol_obs(insts)
    t1 = date(2026, 8, 13)
    t2 = date(2026, 8, 14)

    manifest = asyncio_run(run_backfill(
        run_id="launcher_global_idx",
        bar_dates=[t1, t2],
        dry_run=False,
        population=insts,
        run_symbol_obs=fake,
        adapter=object(),  # 注入路径 sentinel
        output_root=tmp_path,
        code_sha=_TEST_SHA,
        as_of=t2,
        bar_index_offset=31,
    ))

    assert manifest["status"] == "DONE"
    assert manifest["completed_bar_count"] == 2
    # worker2 第一根 bar_index 必须是全局 31（而非重置为 1）
    assert _partition_manifest(tmp_path, "launcher_global_idx", t1)["bar_index"] == 31
    assert _partition_manifest(tmp_path, "launcher_global_idx", t2)["bar_index"] == 32
    assert _partition_manifest(tmp_path, "launcher_global_idx", t1)["status"] == \
        PARTITION_STATUS_COMPLETED


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 3. slice_into_ranges：连续、不重叠、首尾相接、覆盖全量
# ---------------------------------------------------------------------------
def test_launcher_slice_120_4():
    ranges = slice_into_ranges(120, 4)
    assert ranges == [(1, 30), (31, 60), (61, 90), (91, 120)]


def test_launcher_slice_120_3():
    ranges = slice_into_ranges(120, 3)
    assert ranges == [(1, 40), (41, 80), (81, 120)]


def test_launcher_slice_coverage_and_no_overlap():
    for total, workers in [(120, 1), (120, 2), (120, 5), (30, 7), (8, 8)]:
        ranges = slice_into_ranges(total, workers)
        assert len(ranges) == workers
        assert ranges[0][0] == 1
        assert ranges[-1][1] == total
        for (_, e1), (s2, _) in zip(ranges, ranges[1:]):
            assert e1 + 1 == s2  # 首尾相接、无重叠、无空洞


@pytest.mark.parametrize("total,workers", [(0, 1), (120, 0), (10, 11), (120, -1)])
def test_launcher_slice_fail_fast(total, workers):
    with pytest.raises(ValueError):
        slice_into_ranges(total, workers)


# ---------------------------------------------------------------------------
# 4. worker_run_id：独立 run-id
# ---------------------------------------------------------------------------
def test_launcher_run_id():
    assert worker_run_id("member_fact_120bar_pg", 1, 4) == "member_fact_120bar_pg_worker_01"
    assert worker_run_id("member_fact_120bar_pg", 4, 4) == "member_fact_120bar_pg_worker_04"


def test_launcher_run_id_fail_fast():
    with pytest.raises(ValueError):
        worker_run_id("prefix", 0, 4)
    with pytest.raises(ValueError):
        worker_run_id("prefix", 5, 4)


# ---------------------------------------------------------------------------
# 5. build_worker_command：子进程参数拼装含 global --bar-range / --write-db 门
# ---------------------------------------------------------------------------
def test_launcher_subprocess_cmd_global_bar_range():
    cmd = build_worker_command(
        "/x/runner.py", mode="live", start=31, end=60,
        as_of=date(2026, 8, 14), run_id="p_worker_02",
        output_root="/out/p", write_db=True, mdas_chunk_size=512,
        python="/venv/bin/python",
    )
    joined = " ".join(cmd)
    assert "--mode live" in joined
    assert "--bar-range 31:60" in joined      # global bar_index 连续
    assert "--run-id p_worker_02" in joined
    assert "--output-root /out/p" in joined
    assert "--as-of 2026-08-14" in joined
    assert "--write-db" in joined              # 显式授权写库


def test_launcher_subprocess_cmd_without_write_db():
    cmd = build_worker_command(
        "/x/runner.py", mode="live", start=1, end=30,
        as_of=date(2026, 8, 14), run_id="p_worker_01",
        output_root="/out/p", write_db=False, mdas_chunk_size=512,
        python="/venv/bin/python",
    )
    joined = " ".join(cmd)
    assert "--bar-range 1:30" in joined
    assert "--write-db" not in joined          # 无显式授权不写库


# ---------------------------------------------------------------------------
# 6. aggregate_worker：Σ db metrics（审查 §10：root manifest 不累计 DB metrics）
# ---------------------------------------------------------------------------
def test_aggregate_worker_sums_db_metrics():
    partitions = [
        {"status": "COMPLETED", "trade_date": "2026-08-01",
         "db_written_rows": 300, "db_failed_rows": 0},
        {"status": "COMPLETED", "trade_date": "2026-08-02",
         "db_written_rows": 250, "db_failed_rows": 0},
        {"status": "FAILED", "trade_date": "2026-08-03",
         "db_written_rows": 0, "db_failed_rows": 12},
    ]
    agg = aggregate_worker({"status": "DONE"}, partitions)
    assert agg["completed_bar_count"] == 2
    assert agg["failed_bar_count"] == 1
    assert agg["db_written_rows"] == 550
    assert agg["db_failed_rows"] == 12
    assert agg["failed_bars"] == ["2026-08-03"]


def test_collect_worker_outputs_bars_depth(tmp_path):
    """回归护栏：worker 结构为 run_id/bars/<trade_date>/partition_manifest.json（两级深）。

    glob("*/partition_manifest.json") 只匹配一级深，会漏掉全部 partition，
    导致 launcher 聚合 manifest 出现 completed_bar_count=0 / db_written_rows=0
    （Canary N=2 现场复现）。修复后应能读取到并正确聚合。
    """
    run_id = "canary_n2_4bars_worker_01"
    (tmp_path / run_id / "bars").mkdir(parents=True)
    (tmp_path / run_id / "manifest.json").write_text(
        json.dumps({"status": "DONE", "run_id": run_id}), encoding="utf-8")
    for td, written in [("2026-02-13", 5162), ("2026-02-24", 5162)]:
        d = tmp_path / run_id / "bars" / td
        d.mkdir(parents=True)
        (d / "partition_manifest.json").write_text(
            json.dumps({"status": "COMPLETED", "trade_date": td,
                        "db_written_rows": written, "db_failed_rows": 0}),
            encoding="utf-8")

    root, partitions = collect_worker_outputs(tmp_path, run_id)
    assert (root or {}).get("status") == "DONE"
    assert len(partitions) == 2
    agg = aggregate_worker(root, partitions)
    assert agg["completed_bar_count"] == 2
    assert agg["db_written_rows"] == 10324
    assert agg["db_failed_rows"] == 0
    assert agg["failed_bars"] == []
