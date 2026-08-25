"""PERF-OOM-M5D2 — orchestrator Historical Dynamics shadow wiring tests.

Prove that ``_compute_family_dynamics_maps`` can route its canonical
``scope_type`` / ``scope_keys`` / ``trade_dates`` (axis) / ``analysis_asof_date``
into the M5-D1 ``columnar_ew`` owner as a SHADOW-ONLY evidence path, WITHOUT
changing the production-consumed legacy (``"reconstruction"``) Dynamics result.

Wiring contracts covered (M5-D2 requirement list):

  1. default mode — only the legacy reconstruction owner is called;
  2. shadow mode — legacy called without ``historical_source`` (default
     "reconstruction") AND shadow called with ``historical_source="columnar_ew"``;
  3. both calls receive EXACT same db/session, scope_type, ordered scope_keys,
     trade_dates (axis, no rebuild/truncate/resort), analysis_asof_date;
  4. orchestrator-consumed result is the legacy result even when shadow differs;
  5. columnar returned scope reorder  -> shadow evidence FAIL;
  6. missing scope                   -> shadow evidence FAIL;
  7. extra scope                     -> shadow evidence FAIL;
  8. crosswired scope                -> shadow evidence FAIL;
  9. columnar shadow exception       -> legacy unchanged, report has error,
                                        no fallback/retry (exception isolation);
 10. no shadow output enters composition/persistence owner;
 11. shadow disabled                 -> zero new call / zero behaviour change;
 12. no DB writer introduced (static source contract).

Pure-unit: the batch owner and ``_build_dynamics_trading_axis`` are mocked; no
DB, no network.  Shadow mode is reachable ONLY via the explicit
``dynamics_shadow=True`` opt-in — ``compute_run`` / ``resume_run`` never enable
it today.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.pure_unit

ORCH = "app.services.review_orchestrator_service"

_LOGGER = "review_orchestrator_service"

TRADE_DATE = date(2026, 8, 19)
FAKE_AXIS = [date(2026, 5, 19), date(2026, 6, 19), date(2026, 7, 19), TRADE_DATE]


def _item(scope_key: str, tag: str = "legacy") -> dict[str, Any]:
    """One minimal dynamics batch result item (orchestrator consumes only
    ``scope.scope_key`` for mapping plus the payload for composition)."""
    return {
        "scope": {"scope_type": "industry_l1", "scope_key": scope_key},
        "scope_dynamics": {"tag": tag},
        "metrics": {"historical_source": tag},
    }


def _items(keys: list[str], tag: str = "legacy") -> list[dict[str, Any]]:
    return [_item(k, tag) for k in keys]


class _Run:
    trade_date = TRADE_DATE


def _scopes(keys: tuple[str, ...] = ("a", "b")):
    from app.services.review_scope_service import ScopeDefinition

    return [ScopeDefinition("industry_l1", k, k) for k in keys]


def _make_batch_recorder(
    legacy_out=None,
    shadow_out=None,
    shadow_error: Exception | None = None,
):
    """Async batch-owner recorder: captures exact call signatures and returns
    scripted legacy / columnar outputs (or raises for the shadow owner)."""
    calls: list[dict[str, Any]] = []

    async def _record(session, scope_type, scope_keys, axis, *, analysis_asof_date, historical_source="reconstruction"):
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
        if historical_source == "columnar_ew":
            if shadow_error is not None:
                raise shadow_error
            return shadow_out(scope_keys) if callable(shadow_out) else shadow_out
        return legacy_out(scope_keys) if callable(legacy_out) else legacy_out

    return _record, calls


def _patch_env(monkeypatch: pytest.MonkeyPatch, recorder) -> None:
    monkeypatch.setattr(f"{ORCH}.compute_current_static_scope_dynamics_batch", recorder)

    async def _fake_axis(session, asof_date):
        # return the module-level axis object itself so tests can prove the
        # orchestrator boundary forwards the EXACT object the axis builder made.
        return FAKE_AXIS

    monkeypatch.setattr(f"{ORCH}._build_dynamics_trading_axis", _fake_axis)


def _shadow_reports(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for record in caplog.records:
        msg = record.getMessage()
        if not msg.startswith("[Review-dynamics-shadow]"):
            continue
        reports.append(json.loads(msg.split("report=", 1)[1]))
    return reports


def _run_family(recorder, *, shadow: bool = False, session: Any = None) -> dict[str, Any]:
    from app.services.review_orchestrator_service import _compute_family_dynamics_maps

    return asyncio.run(
        _compute_family_dynamics_maps(
            session if session is not None else object(),
            _Run(),
            _scopes(),
            dynamics_shadow=shadow,
        )
    )


# ---------------------------------------------------------------------------
# 1/11. default mode: legacy only, zero shadow calls, zero behaviour change
# ---------------------------------------------------------------------------


def test_d2_default_mode_only_legacy_called(monkeypatch, caplog) -> None:
    """D2-1/D2-11. Without dynamics_shadow the legacy owner is called exactly
    once per family and no columnar_ew call / report exists."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    legacy = _items(["a", "b"], "legacy")
    recorder, calls = _make_batch_recorder(legacy_out=legacy)
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder)

    assert [c["historical_source"] for c in calls] == ["reconstruction"]
    assert result == {"a": legacy[0], "b": legacy[1]}
    assert _shadow_reports(caplog) == []


# ---------------------------------------------------------------------------
# 2/3. shadow mode: both sources called with exact same forwarded args
# ---------------------------------------------------------------------------


def test_d2_shadow_mode_both_sources_called(monkeypatch, caplog) -> None:
    """D2-2. Shadow mode invokes legacy (default "reconstruction") then the
    columnar_ew owner, in exactly that order, per activated family."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["a", "b"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, shadow=True)

    assert [c["historical_source"] for c in calls] == [
        "reconstruction",
        "columnar_ew",
    ]


def test_d2_both_receive_exact_same_forwarded_args(monkeypatch, caplog) -> None:
    """D2-3. Both owners receive the EXACT same db/session object, scope_type,
    ordered scope_keys, full axis (no rebuild/truncation/resort/inference) and
    analysis_asof_date."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    session = object()
    recorder, calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["a", "b"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, shadow=True, session=session)

    legacy_call = next(c for c in calls if c["historical_source"] == "reconstruction")
    shadow_call = next(c for c in calls if c["historical_source"] == "columnar_ew")
    # OBJECT identity: the orchestrator boundary forwards the SAME objects to
    # both owners — session, ordered scope_keys and the canonical axis.
    assert shadow_call["session"] is legacy_call["session"] is session
    assert shadow_call["scope_keys_ref"] is legacy_call["scope_keys_ref"]
    assert shadow_call["axis_ref"] is legacy_call["axis_ref"]
    assert legacy_call["axis_ref"] is FAKE_AXIS
    # exact scope_type / ordered scope_keys / analysis_asof_date forwarding
    assert legacy_call["scope_type"] == shadow_call["scope_type"] == "industry_l1"
    assert legacy_call["scope_keys"] == shadow_call["scope_keys"] == ["a", "b"]
    assert (
        legacy_call["analysis_asof_date"]
        == shadow_call["analysis_asof_date"]
        == TRADE_DATE
    )
    # axis: exact values in exact order —— no rebuild / truncation / resort.
    assert legacy_call["axis"] == FAKE_AXIS
    assert shadow_call["axis"] == FAKE_AXIS
    assert legacy_call["axis"] == sorted(legacy_call["axis"])
    assert len(legacy_call["axis"]) == len(FAKE_AXIS)


# ---------------------------------------------------------------------------
# 4/10. legacy consumed result stays authoritative; shadow stays out
# ---------------------------------------------------------------------------


def test_d2_consumed_result_is_legacy_even_when_shadow_differs(
    monkeypatch, caplog
) -> None:
    """D2-4. When the columnar shadow payload differs from legacy, the returned
    map consumed by composition is still exactly the legacy result."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    legacy = _items(["a", "b"], "legacy")
    shadow = _items(["a", "b"], "SHADOW-DIFFERENT")
    recorder, _calls = _make_batch_recorder(legacy_out=legacy, shadow_out=shadow)
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder, shadow=True)

    assert result == {"a": legacy[0], "b": legacy[1]}


def test_d2_shadow_output_never_enters_composition_owner(monkeypatch, caplog) -> None:
    """D2-10. No shadow payload survives into the returned ``dynamics_map``: the
    map holds the legacy objects by identity, and no columnar tag appears."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    legacy = _items(["a", "b"], "legacy")
    shadow = _items(["a", "b"], "columnar")
    recorder, _calls = _make_batch_recorder(legacy_out=legacy, shadow_out=shadow)
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder, shadow=True)

    assert set(result) == {"a", "b"}
    assert result["a"] is legacy[0] and result["b"] is legacy[1]
    for item in result.values():
        payload = json.dumps(item)
        assert "columnar" not in payload


# ---------------------------------------------------------------------------
# 5-8. shadow scope-mapping failures -> evidence FAIL
# ---------------------------------------------------------------------------


def test_d2_shadow_scope_reorder_evidence_fail(monkeypatch, caplog) -> None:
    """D2-5. Columnar returns the same set but in a different order -> the
    positional comparison marks cross-wiring evidence and shadow fails."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, _calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["b", "a"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    assert report["shadow_status"] == "fail"
    assert report["scope_order_exact"] is False
    assert report["crosswired_scope_keys"] == ["b", "a"]
    assert report["missing_scope_keys"] == []
    assert report["extra_scope_keys"] == []
    # legacy consumed result unaffected
    assert set(result) == {"a", "b"}


def test_d2_shadow_missing_scope_evidence_fail(monkeypatch, caplog) -> None:
    """D2-6. A requested scope absent from the columnar result -> fail evidence."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, _calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["a"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    assert report["shadow_status"] == "fail"
    assert report["missing_scope_keys"] == ["b"]


def test_d2_shadow_extra_scope_evidence_fail(monkeypatch, caplog) -> None:
    """D2-7. A surplus un-requested scope in the columnar result -> fail evidence."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, _calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["a", "b", "c"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    assert report["shadow_status"] == "fail"
    assert report["extra_scope_keys"] == ["c"]


def test_d2_shadow_crosswired_scope_evidence_fail(monkeypatch, caplog) -> None:
    """D2-8. A foreign scope key lands in a requested position -> cross-wiring
    evidence, legacy consumed result untouched."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, _calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["c", "b"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    assert report["shadow_status"] == "fail"
    assert report["crosswired_scope_keys"] == ["c"]
    assert report["missing_scope_keys"] == ["a"]
    assert set(result) == {"a", "b"}


# ---------------------------------------------------------------------------
# 9. exception isolation: legacy unchanged, no fallback/retry
# ---------------------------------------------------------------------------


def test_d2_shadow_exception_legacy_unchanged_no_retry(monkeypatch, caplog) -> None:
    """D2-9. A columnar shadow exception is recorded into the report; the legacy
    result is untouched, NO fallback/retry (legacy called exactly once)."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    legacy = _items(["a", "b"], "legacy")
    recorder, calls = _make_batch_recorder(
        legacy_out=legacy,
        shadow_error=RuntimeError("columnar boom"),
    )
    _patch_env(monkeypatch, recorder)

    result = _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    # exception isolation evidence
    assert report["shadow_status"] == "fail"
    assert report["shadow_error"] is not None
    assert "columnar boom" in report["shadow_error"]
    assert report["columnar_result_count"] is None
    # P1-2: no result -> no right to claim order exact; but the canonical axis
    # still was used to initiate the call -> axis_exact_forwarded stays True.
    assert report["scope_order_exact"] is False
    assert report["axis_exact_forwarded"] is True
    # legacy untouched + still authoritative
    assert result == {"a": legacy[0], "b": legacy[1]}
    # no fallback / no retry: exactly one legacy + one shadow attempt
    assert [c["historical_source"] for c in calls] == [
        "reconstruction",
        "columnar_ew",
    ]


# ---------------------------------------------------------------------------
# 12. no DB writer introduced (static source contract)
# ---------------------------------------------------------------------------


def test_d2_no_db_writer_introduced() -> None:
    """D2-12. Static contract: the D2 shadow wiring source contains no write
    primitives / persistence symbols — evidence reads only."""
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "review_orchestrator_service.py"
    )
    src = src_path.read_text(encoding="utf-8")
    shadow_fn_start = src.index("async def _build_dynamics_shadow_report(")
    # from the shadow helper to the next top-level def
    shadow_src = src[shadow_fn_start : src.index("\ndef ", shadow_fn_start)]

    for forbidden in [
        ".commit(",
        ".add(",
        "session.execute",
        "pg_insert",
        "INSERT INTO",
        "save_scope_observation_fact",
        "save_scope_composition_snapshot",
        "to_sql(",
    ]:
        assert forbidden not in shadow_src, f"DB writer symbol in shadow path: {forbidden!r}"

    # the shadow helper never retains the columnar payloads beyond the report
    assert "ew_values" not in shadow_src
    assert "return shadow_results" not in shadow_src


def test_d2_shadow_pass_evidence(monkeypatch, caplog) -> None:
    """Shadow with an exact-mapped columnar result -> clean pass report."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    recorder, _calls = _make_batch_recorder(
        legacy_out=_items(["a", "b"], "legacy"),
        shadow_out=_items(["a", "b"], "columnar"),
    )
    _patch_env(monkeypatch, recorder)

    _run_family(recorder, shadow=True)
    report = _shadow_reports(caplog)[0]

    assert report["shadow_status"] == "pass"
    assert report["scope_order_exact"] is True
    assert report["missing_scope_keys"] == []
    assert report["extra_scope_keys"] == []
    assert report["crosswired_scope_keys"] == []
    assert report["shadow_error"] is None
    assert report["legacy_result_count"] == 2
    assert report["columnar_result_count"] == 2
