"""PERF-OOM-M5D3 — production Historical Dynamics owner switch tests.

Prove that ``_compute_family_dynamics_maps`` — the orchestrator boundary
consumed by BOTH ``compute_run`` and ``resume_run`` — now makes EXACTLY ONE
AUTHORITATIVE Historical Dynamics batch call per activated family, routed to
the proven ``historical_source="columnar_ew"`` owner (M5-D1), with the
canonical inputs (axis / scope_type / ordered scope_keys / analysis_asof_date /
current-static membership) forwarded unchanged and NO fallback / retry to the
legacy reconstruction (OOM) owner.

The M5-D2 shadow wiring (``dynamics_shadow`` / shadow report) is REMOVED: after
the owner switch a "columnar shadow" would duplicate the same authoritative
owner, and the production orchestrator must never automatically re-run the
legacy reconstruction owner as a shadow.  The legacy ``"reconstruction"`` owner
stays explicitly reachable at the SERVICE level for debug / rollback / parity /
probes; the service default is unchanged.

Pure-unit: the batch owner and ``_build_dynamics_trading_axis`` are mocked; no
DB, no network.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.pure_unit

ORCH = "app.services.review_orchestrator_service"
SERVICE = "app.services.review_scope_dynamics_service"

TRADE_DATE = date(2026, 8, 19)
FAKE_AXIS = [date(2026, 5, 19), date(2026, 6, 19), date(2026, 7, 19), TRADE_DATE]


def _item(scope_key: str, tag: str = "columnar") -> dict[str, Any]:
    """One minimal dynamics batch result item (orchestrator consumes only
    ``scope.scope_key`` for mapping plus the payload for composition)."""
    return {
        "scope": {"scope_type": "industry_l1", "scope_key": scope_key},
        "scope_dynamics": {"tag": tag},
        "metrics": {"historical_source": tag},
    }


def _items(keys: list[str], tag: str = "columnar") -> list[dict[str, Any]]:
    return [_item(k, tag) for k in keys]


class _Run:
    trade_date = TRADE_DATE


def _scopes(keys: tuple[str, ...] = ("a", "b")):
    from app.services.review_scope_service import ScopeDefinition

    return [ScopeDefinition("industry_l1", k, k) for k in keys]


def _make_batch_recorder(out=None, error: Exception | None = None):
    """Async batch-owner recorder: captures the exact call signature and either
    returns the scripted columnar output or raises for the fail-closed test."""
    calls: list[dict[str, Any]] = []

    async def _record(
        session,
        scope_type,
        scope_keys,
        axis,
        *,
        analysis_asof_date,
        historical_source="reconstruction",
    ):
        calls.append(
            {
                "session": session,
                "scope_type": scope_type,
                "scope_keys": list(scope_keys),
                "scope_keys_ref": scope_keys,
                "axis": list(axis),
                "axis_ref": axis,
                "analysis_asof_date": analysis_asof_date,
                "historical_source": historical_source,
            }
        )
        if error is not None:
            raise error
        return out(scope_keys) if callable(out) else out

    return _record, calls


def _patch_env(monkeypatch: pytest.MonkeyPatch, recorder) -> None:
    monkeypatch.setattr(f"{ORCH}.compute_current_static_scope_dynamics_batch", recorder)

    async def _fake_axis(session, asof_date):
        # return the module-level axis object itself so tests can prove the
        # orchestrator boundary forwards the EXACT object the axis builder made.
        return FAKE_AXIS

    monkeypatch.setattr(f"{ORCH}._build_dynamics_trading_axis", _fake_axis)


def _run_family(recorder, *, keys: tuple[str, ...] = ("a", "b"), session: Any = None) -> dict[str, Any]:
    from app.services.review_orchestrator_service import _compute_family_dynamics_maps

    return asyncio.run(
        _compute_family_dynamics_maps(
            session if session is not None else object(),
            _Run(),
            _scopes(keys),
        )
    )


def _orchestrator_src() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "review_orchestrator_service.py"
    ).read_text(encoding="utf-8")


def _service_src() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "review_scope_dynamics_service.py"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D3-1/2/3. authoritative call: columnar_ew, ONE call per family, no legacy
# ---------------------------------------------------------------------------


def test_d3_authoritative_call_uses_columnar_ew(monkeypatch) -> None:
    """D3-1. The authoritative orchestrator call routes historical_source to
    the proven columnar_ew owner."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder)

    assert len(calls) == 1
    assert calls[0]["historical_source"] == "columnar_ew"


def test_d3_exactly_one_batch_call_per_activated_family(monkeypatch) -> None:
    """D3-2. The production/default path makes exactly ONE batch call for the
    whole activated family — never per-scope, never a second (shadow) call."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b", "c"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, keys=("a", "b", "c"))

    assert len(calls) == 1
    assert calls[0]["scope_keys"] == ["a", "b", "c"]


def test_d3_production_default_never_calls_reconstruction(monkeypatch) -> None:
    """D3-3. The production/default orchestrator path never invokes the legacy
    reconstruction owner — the only source seen is columnar_ew."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder)

    assert calls
    assert all(c["historical_source"] == "columnar_ew" for c in calls)


# ---------------------------------------------------------------------------
# D3-4. fail-closed: columnar exception propagates, no fallback / retry
# ---------------------------------------------------------------------------


def test_d3_columnar_exception_propagates_fail_closed(monkeypatch) -> None:
    """D3-4. If the authoritative columnar owner raises, the exception
    propagates to the caller (existing run failure semantics); there is NO
    reconstruction fallback and NO retry."""
    recorder, calls = _make_batch_recorder(error=RuntimeError("columnar boom"))
    _patch_env(monkeypatch, recorder)

    with pytest.raises(RuntimeError, match="columnar boom"):
        _run_family(recorder)

    # exactly one attempt (the authoritative one) — no fallback, no retry
    assert len(calls) == 1
    assert calls[0]["historical_source"] == "columnar_ew"


# ---------------------------------------------------------------------------
# D3-5. result mapping uses the columnar authoritative result exactly
# ---------------------------------------------------------------------------


def test_d3_result_mapping_uses_columnar_result_exactly(monkeypatch) -> None:
    """D3-5. The scope_key -> dynamics map is built from the columnar
    authoritative payload alone (same object identity, no re-derivation)."""
    items = _items(["a", "b"], tag="columnar")
    recorder, _calls = _make_batch_recorder(out=items)
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder)

    assert set(result) == {"a", "b"}
    assert result["a"] is items[0]
    assert result["b"] is items[1]
    assert result["a"]["metrics"]["historical_source"] == "columnar"


# ---------------------------------------------------------------------------
# D3-6. compute_run AND resume_run both consume the single switched boundary
# ---------------------------------------------------------------------------


def test_d3_compute_run_and_resume_run_share_switched_owner() -> None:
    """D3-6. Static source contract: the orchestrator contains EXACTLY ONE
    production call of the batch owner (the authoritative columnar call — no
    legacy call, no duplicate shadow call), and both compute_run and resume_run
    funnel through the shared ``_compute_family_dynamics_maps`` boundary."""
    src = _orchestrator_src()
    # exactly one authoritative batch call in the whole orchestrator
    assert src.count("await compute_current_static_scope_dynamics_batch(\n") == 1
    # that single call site selects the columnar_ew owner
    assert src.count("historical_source=\"columnar_ew\",") == 1
    # compute_run (line A) and resume_run (line B) both route through it
    assert src.count("await _compute_family_dynamics_maps(") == 2


# ---------------------------------------------------------------------------
# D3-7/8/9. canonical inputs forwarded unchanged
# ---------------------------------------------------------------------------


def test_d3_canonical_axis_forwarded_unchanged(monkeypatch) -> None:
    """D3-7. The canonical axis object from ``_build_dynamics_trading_axis`` is
    forwarded verbatim — same object identity, same values, same order, no
    truncation / rebuild / resort / second axis."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder)

    call = calls[0]
    assert call["axis_ref"] is FAKE_AXIS  # same canonical object, no rebuild
    assert call["axis"] == FAKE_AXIS  # values + order exact
    assert len(call["axis"]) == len(FAKE_AXIS)  # no truncation


def test_d3_ordered_scope_keys_forwarded_unchanged(monkeypatch) -> None:
    """D3-8. The ordered scope_keys computed from the family scopes are
    forwarded unchanged (no resort / no per-scope iteration)."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder)

    call = calls[0]
    assert call["scope_type"] == "industry_l1"
    assert call["scope_keys"] == ["a", "b"]


def test_d3_analysis_asof_date_unchanged(monkeypatch) -> None:
    """D3-9. ``analysis_asof_date`` is the run's trade_date, unchanged."""
    recorder, calls = _make_batch_recorder(out=_items(["a", "b"]))
    _patch_env(monkeypatch, recorder)

    _run_family(recorder)

    assert calls[0]["analysis_asof_date"] == TRADE_DATE == _Run.trade_date


# ---------------------------------------------------------------------------
# D3-10/11. legacy service owner stays available (service-level)
# ---------------------------------------------------------------------------


def test_d3_service_default_remains_reconstruction() -> None:
    """D3-10. The SERVICE-level ``historical_source`` default remains
    ``"reconstruction"`` — the orchestrator now overrides it explicitly."""
    from app.services.review_scope_dynamics_service import (
        compute_current_static_scope_dynamics_batch,
    )

    sig = inspect.signature(compute_current_static_scope_dynamics_batch)
    assert sig.parameters["historical_source"].default == "reconstruction"


def test_d3_explicit_reconstruction_still_reachable() -> None:
    """D3-11. The legacy reconstruction owner is not deleted: the batch service
    keeps its reconstruction routing branch and still imports / exposes
    ``reconstruct_scope_series_batch`` for debug / rollback / parity / probes."""
    src = _service_src()
    assert 'historical_source == "columnar_ew"' in src  # routing branch retained
    assert "reconstruct_scope_series_batch" in src  # legacy owner symbol alive


# ---------------------------------------------------------------------------
# D3-12. no writer / schema / migration wiring introduced
# ---------------------------------------------------------------------------


def test_d3_no_writer_schema_migration_change() -> None:
    """D3-12. Static contract: the switched ``_compute_family_dynamics_maps``
    contains no persistence / write symbols — the switch is read-only routing."""
    src = _orchestrator_src()
    fn_start = src.index("async def _compute_family_dynamics_maps(")
    fn_body = src[fn_start : src.index("\ndef ", fn_start)]

    for forbidden in [
        ".commit(",
        ".add(",
        "session.execute",
        "INSERT INTO",
        "to_sql(",
        "save_scope_observation_fact",
        "save_scope_composition_snapshot",
    ]:
        assert forbidden not in fn_body, f"DB writer symbol in dynamics switch: {forbidden!r}"
