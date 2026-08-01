"""Phase 5 contracts for core summary and canonical DSA segments."""
from __future__ import annotations

import ast
import inspect

import numpy as np
import pandas as pd
import pytest

from app.domain.review import metric_engine
from app.schemas.first_pyramid import ChipConsensusResult
from app.services.first_pyramid_flatten import flatten_first_pyramid
from app.services.first_pyramid_service import (
    assemble_first_pyramid_view,
    compute_first_pyramid_core_snapshot,
)
from app.strategy.selectors.dsa_selector import compute_dsa_bundle


def _bars(count: int = 400) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=count, freq="B")
    close = np.linspace(10.0, 20.0, count) + np.sin(np.arange(count) / 8.0) * 0.3
    return pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.linspace(1_000_000.0, 1_400_000.0, count),
            "amount": np.linspace(10_000_000.0, 20_000_000.0, count),
        },
        index=index,
    )


def test_core_only_summary_uses_shared_aggregate_builder() -> None:
    core = compute_first_pyramid_core_snapshot(_bars(), "000001")
    flat = flatten_first_pyramid(core.model_dump())

    expected = " | ".join(
        (core.trend.statusText, core.structure.statusText, core.momentum.statusText)
    )
    assert core.statusText == expected
    assert flat["fp_summary"] == expected
    assert expected


def test_chip_view_only_appends_status_without_mutating_core_summary() -> None:
    core = compute_first_pyramid_core_snapshot(_bars(), "000001")
    original = core.model_dump()
    chip_dimension = core.momentum.model_copy(update={"statusText": "筹码测试状态"})
    view = assemble_first_pyramid_view(
        core,
        ChipConsensusResult(chip=chip_dimension, chipHash="chip-test"),
    )

    assert core.model_dump() == original
    assert view.trend == core.trend
    assert view.structure == core.structure
    assert view.momentum == core.momentum
    assert view.statusText == f"{core.statusText} | 筹码测试状态"


def test_dsa_segment_producer_exposes_complete_cumulative_contract() -> None:
    bars = _bars()
    bundle = compute_dsa_bundle(bars, {})
    metrics = bundle["last_row_metrics"]
    required = {
        "segment_id",
        "segment_start_time",
        "segment_end_time",
        "segment_start_price",
        "segment_end_price",
        "segment_bars",
        "segment_change_pct",
        "segment_slope",
        "current_segment_volume_mean",
        "prev_segment_volume_mean",
        "current_vs_prev_volume_mean_ratio",
    }
    assert required <= metrics.keys()
    assert metrics["segment_end_time"] == bars.index[-1].date().isoformat()
    assert metrics["segment_bars"] == (
        metrics["segment_end_bar_index"] - metrics["segment_start_bar_index"] + 1
    )
    expected_change = (
        metrics["segment_end_price"] / metrics["segment_start_price"] - 1.0
    ) * 100.0
    assert metrics["segment_change_pct"] == pytest.approx(expected_change)
    assert metrics["segment_slope"] == pytest.approx(
        expected_change / metrics["segment_bars"]
    )
    assert metrics["segment_change_pct"] != pytest.approx(
        bundle["factor_per_bar"].iloc[-1]["change_pct"]
    )


def test_visual_segments_are_not_segment_fact_ownership() -> None:
    bundle = compute_dsa_bundle(_bars(), {})
    before = dict(bundle["last_row_metrics"])
    bundle["visual_segments"] = []
    assert bundle["last_row_metrics"] == before
    assert before["segment_start_bar_index"] is not None


@pytest.mark.xfail(
    strict=True,
    reason="Phase 8 replaces legacy Review P with typed ReviewMemberFact",
)
def test_review_daily_return_derivers_do_not_read_segment_change() -> None:
    forbidden_functions = {
        "_derive_scope_return_1d",
        "_derive_advance_ratio",
        "_derive_non_head_participation_ratio",
        "_derive_leader_follower_common_confirm_ratio",
    }
    tree = ast.parse(inspect.getsource(metric_engine))
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_functions:
            if "fp_segment_change_pct" in ast.unparse(node):
                violations.append(node.name)
    assert not violations, f"daily-return consumers read segment change: {violations}"
