"""Round 2.1 AUDIT-FIX-01 — Snapshot run gate & Event cell contract tests.

SNAPSHOT-GATE: ``_accepted_exact_t_snapshot_run_ids`` is the SINGLE owner for
"consumable Current snapshot run at exact-T".  An exact-date row that is failed /
unpublished must NOT be consumable (audit #3: no contract drift with Integrity
Gate / Replay Selection).

EVENT-RTM: the probe inspects the FORMAL production event cells
(``structure.events.cells.leveled.<EVENT>_<dir>_<level>``) and does NOT sum
member_ratio across cells (audit #4: no second aggregation invented by the probe).
A member firing BOS_up_Swing AND BOS_up_Internal stays in two distinct cells.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import date, datetime

import pytest

pytestmark = pytest.mark.pure_unit

from scripts.review_scope_dynamics_probe import (
    _accepted_exact_t_snapshot_run_ids,
    _extract_event_cells,
)

# ---------------------------------------------------------------------------
# SNAPSHOT-GATE
# ---------------------------------------------------------------------------


def _write_snapshot_runs(dataset_dir: str, rows: list[dict]) -> None:
    import os
    from pathlib import Path

    p = Path(dataset_dir) / "lineage"
    p.mkdir(parents=True, exist_ok=True)
    with gzip.open(str(p / "stock_feature_snapshot_runs.jsonl.gz"), "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_snapshot_gate_exact_date_failed_unpublished_not_consumable(tmp_path) -> None:
    """SNAPSHOT-GATE: exact-date row that is failed / unpublished is NOT consumable."""
    asof = date(2026, 8, 17)
    rows = [
        # exact-T, succeeded, published -> consumable
        {"id": "run-ok", "trade_date": "2026-08-17", "status": "succeeded",
         "published_at": "2026-08-17T00:10:00"},
        # exact-T but FAILED -> not consumable
        {"id": "run-fail", "trade_date": "2026-08-17", "status": "failed",
         "published_at": "2026-08-17T00:10:00"},
        # exact-T, succeeded but UNPUBLISHED -> not consumable
        {"id": "run-unpub", "trade_date": "2026-08-17", "status": "succeeded",
         "published_at": None},
        # different date -> not consumable for this asof
        {"id": "run-other-date", "trade_date": "2026-08-14", "status": "succeeded",
         "published_at": "2026-08-14T00:10:00"},
    ]
    _write_snapshot_runs(str(tmp_path), rows)
    accepted = _accepted_exact_t_snapshot_run_ids(str(tmp_path), asof)
    assert accepted == frozenset({"run-ok"})


def test_snapshot_gate_no_consumable_run_returns_empty(tmp_path) -> None:
    """SNAPSHOT-GATE: exact-date present but all failed/unpublished -> empty (no fake ok)."""
    asof = date(2026, 8, 17)
    rows = [
        {"id": "r1", "trade_date": "2026-08-17", "status": "failed",
         "published_at": "2026-08-17T00:10:00"},
        {"id": "r2", "trade_date": "2026-08-17", "status": "succeeded",
         "published_at": None},
    ]
    _write_snapshot_runs(str(tmp_path), rows)
    accepted = _accepted_exact_t_snapshot_run_ids(str(tmp_path), asof)
    assert accepted == frozenset()


# ---------------------------------------------------------------------------
# EVENT-RTM
# ---------------------------------------------------------------------------


def _obs_with_event_cells(cells: dict) -> dict:
    return {
        "structure": {
            "events": {
                "denominator": 100,
                "cells": {"leveled": cells},
            }
        }
    }


def test_event_rtm_cell_evidence_no_sum_across_cells() -> None:
    """EVENT-RTM: probe inspects formal cells; a member in two cells is NOT summed.

    One member fires BOS_up_Swing AND BOS_up_Internal on the same day.  The probe
    returns BOTH cells verbatim (cell evidence) — it must NOT add their
    member_ratio into a single "BOS ratio" (that would double-count the member).
    """
    cells = {
        "BOS_up_Swing": {"event_count": 1, "member_count": 1, "member_ratio": 0.01},
        "BOS_up_Internal": {"event_count": 1, "member_count": 1, "member_ratio": 0.01},
        "CHoCH_down_Swing": {"event_count": 1, "member_count": 1, "member_ratio": 0.01},
    }
    obs = _obs_with_event_cells(cells)
    bos = _extract_event_cells(obs, "BOS")
    # cell evidence only — the two BOS cells are returned as-is, not summed.
    assert set(bos.keys()) == {"BOS_up_Swing", "BOS_up_Internal"}
    assert bos["BOS_up_Swing"]["member_ratio"] == 0.01
    assert bos["BOS_up_Internal"]["member_ratio"] == 0.01
    # no "overall BOS ratio" key invented by the probe
    assert "BOS_overall_ratio" not in bos
    # CHoCH is a distinct event type, not merged into BOS
    assert "CHoCH_down_Swing" not in bos


def test_event_rtm_absent_events_returns_empty() -> None:
    """EVENT-RTM: no events subtree -> empty cells (no fake 0, no crash)."""
    assert _extract_event_cells({}, "BOS") == {}
    assert _extract_event_cells({"structure": {}}, "BOS") == {}
