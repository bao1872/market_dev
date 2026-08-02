from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.review.member_fact import DailyBarFact, ReviewMemberFact
from app.domain.review.metric_engine import (
    _cross_section_percentile,
    _derive_advance_ratio,
    _derive_amount_expansion_ratio,
    _derive_member_change_hhi,
    _derive_momentum_enhancing_coverage,
    _derive_multi_dim_improving_ratio,
    _derive_scope_return_1d,
    _derive_trend_segment_volume_improvement,
    _derive_volume_expansion_ratio,
    compute_all_metrics,
)
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.services import review_orchestrator_service as orchestrator
from app.services.review_scope_service import ScopeDefinition


def _bars(*, last_close: float = 110.0) -> list[DailyBarFact]:
    start = date(2026, 1, 1)
    rows: list[DailyBarFact] = []
    for index in range(201):
        close = 100.0 if index < 200 else last_close
        rows.append(
            DailyBarFact(
                trade_date=start + timedelta(days=index),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100.0 if index < 200 else 200.0,
                amount=1_000.0 if index < 200 else 3_000.0,
            )
        )
    return rows


def _fact() -> dict[str, object]:
    rows = _bars()
    fact = ReviewMemberFact.build(
        instrument_id=uuid.uuid4(),
        symbol="600000",
        name="浦发银行",
        snapshot_id=uuid.uuid4(),
        trade_date=rows[-1].trade_date,
        first_pyramid={
            "fp_trend_direction": "up",
            "fp_swing_direction": "up",
            "fp_internal_direction": "up",
            "fp_momentum_direction": "expanding",
            "fp_momentum_change": "enhancing",
            "fp_segment_change_pct": -99.0,
            "fp_segment_volume_ratio": 1.5,
        },
        bars=rows,
        previous_state={
            "regime_value": 0,
            "swing_bias": "sideways",
            "internal_bias": "sideways",
            "momentum_direction": "flat",
            "momentum_change": "flat",
        },
    )
    return fact.to_metric_input()


def test_member_fact_uses_daily_price_and_separates_volume_from_amount() -> None:
    fact = _fact()
    assert fact["review_return_1d"] == pytest.approx(10.0)
    assert fact["review_volume_ratio20"] == pytest.approx(2.0)
    assert fact["review_amount_ratio20"] == pytest.approx(3.0)
    assert fact["review_volume_percentile20"] == 100.0
    assert fact["review_amount_percentile200"] == 100.0
    assert fact["fp_segment_change_pct"] == -99.0


def test_p_and_c_ignore_dsa_segment_change() -> None:
    positive = _fact()
    negative_segment = {**positive, "fp_segment_change_pct": 99.0}
    members = [positive, negative_segment]
    assert _derive_scope_return_1d(members) == pytest.approx(10.0)
    assert _derive_advance_ratio(members) == 1.0
    assert _derive_member_change_hhi(members) == pytest.approx(0.5)


def test_u_requires_day_over_day_improvement() -> None:
    improved = _fact()
    unchanged = {
        **improved,
        "review_previous_first_pyramid": {
            "fp_trend_direction": "up",
            "fp_swing_direction": "up",
            "fp_internal_direction": "up",
            "fp_momentum_direction": "expanding",
            "fp_momentum_change": "enhancing",
        },
    }
    assert _derive_multi_dim_improving_ratio([improved, unchanged]) == 0.5
    assert _derive_momentum_enhancing_coverage([improved, unchanged]) == 0.5


def test_v_uses_correct_dimensions_and_segment_mean_ratio() -> None:
    fact = _fact()
    assert _derive_volume_expansion_ratio([fact]) == 1.0
    assert _derive_amount_expansion_ratio([fact]) == 1.0
    assert _derive_trend_segment_volume_improvement([fact]) == 1.5


def test_cross_section_is_not_subject_to_sixty_day_history_gate() -> None:
    assert _cross_section_percentile(20.0, [10.0, 20.0, 30.0]) == pytest.approx(200 / 3)


def test_component_evidence_includes_weight_mode_and_registry_has_no_segment_return() -> None:
    fact = _fact()
    history = {
        code: {
            component.name: [float(index) for index in range(60)]
            for component in DEFAULT_REGISTRY.get_metric(code).components
        }
        for code in DEFAULT_REGISTRY.metric_codes
    }
    payloads = compute_all_metrics([fact], history_maps=history)
    assert all(
        component["weightMode"] == "equal_weight"
        for payload in payloads.values()
        for component in payload["components"]
    )
    assert all(
        "fp_segment_change_pct" not in component.extra_fields
        for code in DEFAULT_REGISTRY.metric_codes
        for component in DEFAULT_REGISTRY.get_metric(code).components
    )


@pytest.mark.asyncio
async def test_orchestrator_finishes_cross_section_before_any_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    scopes = [
        ScopeDefinition("style", "one", "风格一"),
        ScopeDefinition("style", "two", "风格二"),
    ]
    snapshots = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

    async def _scopes(*_args: Any, **_kwargs: Any) -> list[ScopeDefinition]:
        return scopes

    async def _metrics(_session: Any, _run: Any, scope: ScopeDefinition) -> Any:
        calls.append(f"metrics:{scope.scope_key}")
        return snapshots[len(calls) - 1]

    async def _cross(*_args: Any, **_kwargs: Any) -> int:
        calls.append("cross")
        return 2

    async def _signals(_session: Any, _run: Any, scope: ScopeDefinition, _snapshot: Any) -> int:
        calls.append(f"signals:{scope.scope_key}")
        return 1

    async def _zero(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(orchestrator, "_resolve_level1_scopes", _scopes)
    monkeypatch.setattr(orchestrator, "_compute_scope_metrics_phase", _metrics)
    monkeypatch.setattr(orchestrator, "apply_cross_section_percentiles", _cross)
    monkeypatch.setattr(orchestrator, "_compute_scope_signal_pipeline", _signals)
    monkeypatch.setattr(orchestrator, "evaluate_all_active_trackings", _zero)
    monkeypatch.setattr(orchestrator, "update_run_signal_count", _zero)

    class Session:
        async def flush(self) -> None:
            return None

    run = SimpleNamespace(
        id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        expected_scope_count=0,
        succeeded_scope_count=0,
        failed_scope_count=0,
        coverage_ratio=0,
        status="created",
        started_at=None,
        completed_at=None,
    )
    await orchestrator.compute_run(Session(), run)
    assert calls == [
        "metrics:one",
        "metrics:two",
        "cross",
        "signals:one",
        "signals:two",
    ]
