"""REVIEW-CANONICAL-RUNTIME-REPLACEMENT — canonical composition + capability tests.

Covers the Scope Capability matrix (7 parallel Scope Families) and the single
CanonicalReviewComposition owner (deterministic 6-key composition + fail-closed
readiness).  Pure unit tests — no DB, no IO.

    PURE_UNIT_TEST=1 backend/.venv/bin/python -m pytest \\
        tests/test_review_canonical_composition.py
"""
from __future__ import annotations

import pytest

from app.domain.review.canonical_composition import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    ReviewCompositionError,
    compose_canonical_review_scope,
)
from app.domain.review.review_capability import (
    ALL_SCOPE_FAMILIES,
    SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES,
    capability_to_json,
    is_scope_observation_persistence_activated,
    resolve_scope_capability,
)

# ---------------------------------------------------------------------------
# Scope Capability matrix
# ---------------------------------------------------------------------------
ACTIVATED = SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES


def _cap(scope_type: str, scope_name: str = "x"):
    return resolve_scope_capability(scope_type=scope_type, scope_name=scope_name)


def test_all_scope_families_are_the_7_parallel_families() -> None:
    assert ALL_SCOPE_FAMILIES == frozenset(
        {"market", "major_index", "style", "industry_l1", "industry_l2",
         "industry_l3", "concept"}
    )


def test_activation_set_is_the_frozen_4() -> None:
    assert ACTIVATED == frozenset(
        {"industry_l1", "industry_l2", "industry_l3", "concept"}
    )
    # market / major_index / style are NOT activated.
    assert not is_scope_observation_persistence_activated("market")
    assert not is_scope_observation_persistence_activated("major_index")
    assert not is_scope_observation_persistence_activated("style")


@pytest.mark.parametrize("family", ["industry_l1", "industry_l2", "industry_l3", "concept"])
def test_activated_families_persist_but_dynamics_not_yet_runtime_wired(family: str) -> None:
    """activated 家族现状：persistence/membership/attribution 均可用。

    ``historical_dynamics_available`` 本轮为 False —— orchestrator runtime 尚未
    接入 ObservationSeries -> Scope Dynamics 链（SCALE GATE deferred）；这是诚实的
    探索期状态，不是永久架构，也不回退 legacy。
    """
    cap = _cap(family)
    assert cap.persistence_activated is True
    assert cap.current_membership_available is True
    assert cap.historical_membership_available is True
    assert cap.canonical_observation_available is True
    assert cap.member_attribution_available is True
    # runtime wiring 未落地 -> dynamics 明确 unavailable_current（结构化 reason）
    assert cap.historical_dynamics_available is False
    assert cap.historical_dynamics_runtime_wired is False
    assert cap.reason is not None
    assert "not_runtime_wired" in cap.reason


def test_market_is_not_activated_and_historical_pit_is_a_gap_not_architecture() -> None:
    cap = _cap("market")
    assert cap.persistence_activated is False
    assert cap.current_membership_available is True
    # historical membership is an IMPLEMENTATION GAP (not a permanent architecture
    # switch) — the family stays parallel, it just cannot resolve exact-T market
    # membership today.
    assert cap.historical_membership_available is False
    assert cap.historical_dynamics_available is False
    assert cap.reason is not None
    assert "pit_gap" in cap.reason
    j = capability_to_json(cap)
    assert j["reason"] == cap.reason


def test_major_index_and_style_resolve_historical_membership_but_do_not_persist() -> None:
    """major_index/style：历史 membership 可解析（board PIT path），但不激活
    persistence -> 无 attribution；dynamics 亦未 runtime wired。"""
    for family in ("major_index", "style"):
        cap = _cap(family)
        assert cap.persistence_activated is False
        assert cap.historical_membership_available is True  # board-family PIT path
        assert cap.member_attribution_available is False  # no persistence -> no attribution
        assert cap.historical_dynamics_available is False  # runtime 未 wired


def test_non_activated_guard_never_falls_back_to_legacy() -> None:
    """The reason must explicitly declare a legal skip, never a legacy fallback."""
    for family in ("market", "major_index", "style"):
        joined = " ".join(_cap(family).reasons)
        assert "legal skip" in joined
        assert "fallback" in joined  # the 'NOT a ... fallback' negation is present


def test_unknown_scope_family_fails_fast() -> None:
    with pytest.raises(ValueError):
        _cap("not_a_family")


# ---------------------------------------------------------------------------
# CanonicalReviewComposition owner
# ---------------------------------------------------------------------------
def _layers():
    ob = {"status": STATUS_READY}
    dyn = {"status": STATUS_READY}
    struct = {"status": STATUS_READY}
    lead = {"status": STATUS_READY}
    attr = {"status": STATUS_READY}
    return ob, dyn, struct, lead, attr


def _compose(family, *, cap=None, **kwargs):
    from app.domain.review.review_capability import resolve_scope_capability
    c = cap or resolve_scope_capability(scope_type=family, scope_name="x")
    return compose_canonical_review_scope(
        scope_type=family,
        scope_key="k",
        trade_date="2026-08-19",
        capability=c,
        **kwargs,
    )


def test_composition_fixed_key_contract_for_activated_scope() -> None:
    ob, dyn, struct, lead, attr = _layers()
    r = _compose(
        "industry_l1", scope_observation=ob, historical_dynamics=dyn,
        internal_structure_facts=struct, leadership=lead, member_attribution=attr,
    )
    assert set(r.keys()) == {
        "scope", "trade_date", "capability", "scope_observation",
        "historical_dynamics", "internal_structure_facts", "leadership",
        "member_attribution", "composition_readiness",
    }
    assert r["composition_readiness"] == STATUS_READY


def test_composition_unavailable_precedence_wins() -> None:
    """unavailable 在 required 层上优先于 ready（冻结 precedence 契约）。

    ``member_attribution`` 是 industry_l1 的 required 层（activated 家族），
    其 status=unavailable_current 必须让 composition 整体 unavailable——绝不
    静默 ready（绝不回退 legacy）。
    """
    ob, dyn, struct, lead, _attr = _layers()
    attr = {"status": STATUS_UNAVAILABLE}
    r = _compose(
        "industry_l1", scope_observation=ob, historical_dynamics=dyn,
        internal_structure_facts=struct, leadership=lead, member_attribution=attr,
    )
    assert r["composition_readiness"] == STATUS_UNAVAILABLE


def test_present_only_unavailable_dynamics_does_not_gate_readiness() -> None:
    """本轮 historical_dynamics 未 runtime-wired → 结构化 unavailable 层存在但
    不 gating（``_required_layers`` 只在 ``capability.historical_dynamics_available``
    为 True 时才 required）。orchestrator runtime 正是依赖这一点：leadership /
    dynamics 恒为 structured unavailable，但 observation + attribution ready 时
    composition 仍 ready —— 否则 activated scope 永远无法发布。
    """
    ob, _dyn, struct, lead, attr = _layers()
    dyn = {"status": STATUS_UNAVAILABLE}
    r = _compose(
        "industry_l1", scope_observation=ob, historical_dynamics=dyn,
        internal_structure_facts=struct, leadership=lead, member_attribution=attr,
    )
    assert r["composition_readiness"] == STATUS_READY


def test_composition_insufficient_wins_over_ready() -> None:
    ob, dyn, struct, lead, _attr = _layers()
    attr = {"status": STATUS_INSUFFICIENT}
    r = _compose(
        "industry_l1", scope_observation=ob, historical_dynamics=dyn,
        internal_structure_facts=struct, leadership=lead, member_attribution=attr,
    )
    assert r["composition_readiness"] == STATUS_INSUFFICIENT


def test_activated_scope_fail_closed_on_missing_required_layer() -> None:
    # member_attribution is REQUIRED for an activated, attribution-capable scope.
    ob, dyn, _struct, lead, _attr = _layers()
    with pytest.raises(ReviewCompositionError):
        _compose(
            "industry_l1", scope_observation=ob, historical_dynamics=dyn,
            internal_structure_facts=_struct, leadership=lead,
        )


def test_activated_scope_fail_closed_on_missing_scope_observation() -> None:
    _, dyn, struct, lead, attr = _layers()
    with pytest.raises(ReviewCompositionError):
        _compose(
            "concept", historical_dynamics=dyn, internal_structure_facts=struct,
            leadership=lead, member_attribution=attr,
        )


def test_market_capability_restricted_required_set() -> None:
    """market is not activated -> only scope_observation is required; no dynamics
    / attribution gate, and never a legacy fallback (it just composes what it can)."""
    ob = {"status": STATUS_READY}
    r = _compose("market", scope_observation=ob)
    assert r["composition_readiness"] == STATUS_READY
    # Even though scope_observation is ready, market still carries a capability gap.
    assert r["capability"]["persistence_activated"] is False
    assert r["capability"]["historical_dynamics_available"] is False
    assert r["capability"]["reason"] is not None


def test_unknown_layer_status_fails_fast() -> None:
    ob, dyn, struct, lead, _attr = _layers()
    attr = {"status": "stale"}  # a forbidden/invented status must be rejected
    with pytest.raises(ReviewCompositionError):
        _compose(
            "industry_l1", scope_observation=ob, historical_dynamics=dyn,
            internal_structure_facts=struct, leadership=lead, member_attribution=attr,
        )
