"""Modified-scope pure/unit tests for Canonical Observation Fact Persistence (Round 1C).

Covers the persistence owner (``review_observation_persistence_service``):
activation checks, Market / major_index / style exclusion, payload-not-modified
validation, partial-facts saveability, and PIT-unavailable non-entry into the
save path.  No DB, no network, no CI (pure unit mode).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import MemberObservation, compute_scope_observation
from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
    CANONICAL_TOP_LEVEL_SECTIONS,
    CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES,
    MARKET_PERSISTENCE_DIAGNOSTIC,
    ScopeObservationPayloadValidationError,
    ScopePersistenceNotActivatedError,
    _build_fact_values,
    _snapshot_readiness,
    is_scope_observation_persistence_excluded,
    save_scope_observation_fact,
    validate_scope_observation_payload,
)
from app.services.review_observation_prep_service import PreparedScope

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


def _canonical_obs(
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    trade_date: date = T,
    price_candidate: bool = True,
) -> dict:
    """Build a minimal but legal Canonical Observation payload via the real Core.

    Uses ``compute_scope_observation`` so the payload is a genuine canonical
    shape (exact top-level section set), never an invented second semantics.
    ``price_candidate=False`` yields a legal partial axis (empty price universe)
    while keeping the complete canonical structure.
    """
    members = [
        MemberObservation(
            member_id="m1",
            price_candidate=price_candidate,
            return_1d=0.01 if price_candidate else None,
            amount=100.0,
            trend=Direction.UP,
            swing=Direction.SIDEWAYS,
            internal=Direction.DOWN,
            momentum=MomentumDirection.FLAT,
        )
    ]
    return compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=trade_date,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],
        members=members,
        event_coverage_member_ids=None,
    )


class _FakeSession:
    """Dummy session: any execute/commit would fail the test (must never be reached)."""

    async def execute(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("save reached DB despite a guard that should have blocked")

    async def flush(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("save reached DB despite a guard that should have blocked")


def _prep(
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    pit_status_t: str = "historical_pit",
    members: tuple = ("m1", "m2"),
) -> PreparedScope:
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
        trade_date=T,
        canonical_t1=T1,
        pit_member_ids=("m1", "m2"),
        pit_member_ids_t1=("m1",),
        members=members,
        t1_membership_available=True,
        pit_status_t=pit_status_t,
        pit_status_t1="historical_pit",
        diagnostics=("ok",),
        event_coverage_member_ids=None,
    )


def test_activation_set_exact() -> None:
    assert ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES == frozenset(
        {"industry_l1", "industry_l2", "industry_l3", "concept"}
    )
    assert "market" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "major_index" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "style" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES


# ── A 级机制/资格/事件标签概念过滤（A 步观察持久化 scope 排除）──

def test_concept_exclude_names_exact() -> None:
    # 排除清单必须精确锁定 12 个 A 级概念（与 DB market_boards.name 一一对应）。
    assert CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES == frozenset(
        {
            "融资融券", "深股通", "沪股通", "专精特新",
            "2026中报预增", "2026一季报预增",
            "股权转让(并购重组)", "ST板块", "摘帽",
            "新股与次新股", "注册制次新股", "科创次新股",
        }
    )


def test_excluded_concept_is_filtered() -> None:
    # A 级机制/资格/事件标签概念应被 A 步持久化排除。
    for name in CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES:
        assert is_scope_observation_persistence_excluded(
            scope_type="concept", scope_name=name,
        ), name


def test_non_excluded_concept_is_kept() -> None:
    # 非 A 级概念（真实主题）保留。
    for name in ("锂电池概念", "光伏概念", "低空经济", "商业航天", "人形机器人"):
        assert not is_scope_observation_persistence_excluded(
            scope_type="concept", scope_name=name,
        ), name


def test_industry_scope_never_excluded() -> None:
    # industry_l1/l2/l3 永不排除；即使板块名恰好同名。
    for scope_type in ("industry_l1", "industry_l2", "industry_l3"):
        assert not is_scope_observation_persistence_excluded(
            scope_type=scope_type, scope_name="融资融券",
        ), scope_type


def test_b_c_scope_not_filtered_by_exclude_list() -> None:
    # B/C 级（覆盖过泛但确属主题 / 地区政策类）按产品决策不过滤。
    # 显式验证避免未来误把覆盖度阈值塞进排除清单。
    for name in (
        "国企改革", "机器人概念", "人工智能", "新能源汽车", "储能", "芯片概念",
        "一带一路", "西部大开发", "粤港澳大湾区", "乡村振兴", "数字经济",
    ):
        assert not is_scope_observation_persistence_excluded(
            scope_type="concept", scope_name=name,
        ), name


def test_member_count_below_or_equal_threshold_is_excluded() -> None:
    # 成员数 <= 10 的 concept（样本过小）排除；>10 保留。
    for n in (0, 1, 4, 8, 10):
        assert is_scope_observation_persistence_excluded(
            scope_type="concept", scope_name="某个小概念", member_count=n,
        ), n
    assert not is_scope_observation_persistence_excluded(
        scope_type="concept", scope_name="某个小概念", member_count=11,
    )
    assert not is_scope_observation_persistence_excluded(
        scope_type="concept", scope_name="某个小概念", member_count=100,
    )


def test_member_count_without_count_keeps_theme() -> None:
    # member_count=None（prepare 前）时只按 name 过滤，真实主题小样本保留到 prepare 后判定。
    assert not is_scope_observation_persistence_excluded(
        scope_type="concept", scope_name="中船系",
    )
    # 带 member_count 才触发成员数排除。
    assert is_scope_observation_persistence_excluded(
        scope_type="concept", scope_name="中船系", member_count=10,
    )


def test_member_count_threshold_not_applied_to_industry() -> None:
    # industry scope 永不受成员数阈值影响。
    assert not is_scope_observation_persistence_excluded(
        scope_type="industry_l1", scope_name="银行", member_count=1,
    )


def test_canonical_top_level_sections_exact() -> None:
    # Blocker #1: the canonical table only accepts the exact canonical set.
    # ``amount`` is legacy topology (canonical amount lives under ``price.amount``)
    # and is explicitly rejected at the top level.
    assert CANONICAL_TOP_LEVEL_SECTIONS == frozenset(
        {"scope", "price", "trend", "structure", "momentum", "participation", "chip"}
    )


# ── Round 1C correction Blocker #1 — canonical payload contract validator ──
# Prompt §6 tests A..I are mapped to lowercase function names below.

def test_validator_accepts_full_canonical_payload() -> None:  # prompt §6-A
    obs = _canonical_obs()
    validate_scope_observation_payload(
        obs, scope_type="concept", scope_key="A", trade_date=T
    )


def test_validator_rejects_missing_price() -> None:  # prompt §6-B
    obs = _canonical_obs()
    del obs["price"]
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T
        )


def test_validator_rejects_missing_trend() -> None:  # prompt §6-C
    obs = _canonical_obs()
    del obs["trend"]
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T
        )


def test_validator_rejects_arbitrary_payload() -> None:  # prompt §6-D
    # {"scope": {...}, "marker": "x"} — non-canonical top-level keys.
    obs: dict = {"scope": {"scope_type": "concept", "scope_key": "A"}, "marker": "x"}
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T
        )


def test_validator_rejects_extra_subjective_key() -> None:  # prompt §6-E
    obs = _canonical_obs()
    obs["opportunity_score"] = 0.9
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T
        )


def test_validator_rejects_scope_type_mismatch() -> None:  # prompt §6-F
    obs = _canonical_obs(scope_type="concept", scope_key="A")
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="industry_l1", scope_key="A", trade_date=T
        )


def test_validator_rejects_scope_key_mismatch() -> None:  # prompt §6-G
    obs = _canonical_obs(scope_type="concept", scope_key="A")
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="B", trade_date=T
        )


def test_validator_rejects_trade_date_mismatch() -> None:  # prompt §6-H
    obs = _canonical_obs(scope_type="concept", scope_key="A", trade_date=T)
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T1
        )


def test_validator_accepts_legal_partial_axis_full_structure() -> None:  # prompt §6-I
    # A legal partial axis (empty price universe) keeps the full canonical
    # structure and passes; partialness is not an invariant failure here.
    obs = _canonical_obs(price_candidate=False)
    validate_scope_observation_payload(
        obs, scope_type="concept", scope_key="A", trade_date=T
    )


def test_validator_rejects_non_dict_section() -> None:
    obs = _canonical_obs()
    obs["price"] = "not-a-dict"
    with pytest.raises(ScopeObservationPayloadValidationError):
        validate_scope_observation_payload(
            obs, scope_type="concept", scope_key="A", trade_date=T
        )


@pytest.mark.parametrize("scope_type", ["market", "major_index", "style"])
@pytest.mark.asyncio
async def test_non_activated_scope_types_blocked(scope_type: str) -> None:
    prep = _prep(scope_type=scope_type)
    with pytest.raises(ScopePersistenceNotActivatedError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_market_excluded_even_when_generic_loop_passes_members() -> None:
    # Double safety: even if a generic loop fed market with resolved members, the
    # persistence activation guard must block it (prompt §16).
    prep = _prep(scope_type="market", pit_status_t="historical_pit", members=("m1",))
    with pytest.raises(ScopePersistenceNotActivatedError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_pit_unavailable_does_not_enter_save_path() -> None:
    # Activated scope but PIT(T) unavailable -> no fact row is written (prompt §19A).
    prep = _prep(pit_status_t="unavailable", members=())
    with pytest.raises(ValueError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_no_members_does_not_enter_save_path() -> None:
    prep = _prep(pit_status_t="historical_pit", members=())
    with pytest.raises(ValueError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


def test_build_fact_values_does_not_modify_core_output() -> None:
    prep = _prep()
    obs: dict = {"scope": {"scope_type": "concept"}, "price": {"return": {"mean": 0.01}}}
    values = _build_fact_values(prep, obs, "review-obs-1.0.0")
    # Same object reference stored (no copy / rename / recompute).
    assert values["observation_payload"] is obs
    assert values["observation_payload"]["price"]["return"]["mean"] == 0.01
    assert values["trade_date"] == T
    assert values["scope_type"] == "concept"
    assert values["scope_key"] == "A"
    assert values["pit_member_count"] == 2
    assert values["pit_member_count_t1"] == 1
    assert values["provided_member_count"] == 2
    assert values["t1_membership_available"] is True
    assert values["pit_status_t"] == "historical_pit"
    assert values["pit_status_t1"] == "historical_pit"
    assert values["readiness"] == "ready"
    assert values["diagnostics"] == ["ok"]
    assert values["algorithm_version"] == "review-obs-1.0.0"


def test_partial_facts_can_be_saved() -> None:
    # Core returned normally but some axis is unavailable/partial -> still saved
    # as-is, readiness stays "ready" (persistence never judges completeness,
    # prompt §19C / §20).  No threshold-derived downgrade.  A legal partial axis
    # keeps the FULL canonical structure (Round 1C correction test I).
    prep = _prep()
    partial_obs = _canonical_obs(price_candidate=False)
    values = _build_fact_values(prep, partial_obs, None)
    assert values["observation_payload"] is partial_obs
    assert values["readiness"] == "ready"


@pytest.mark.asyncio
async def test_save_rejects_invalid_payload_before_persist() -> None:
    # Blocker #1: an arbitrary (non-canonical) payload must be rejected at the
    # save path before any DB write (FakeSession would raise if reached).
    prep = _prep()
    with pytest.raises(ScopeObservationPayloadValidationError):
        await save_scope_observation_fact(
            _FakeSession(), prep, {"scope": {"scope_type": "concept"}, "marker": "x"}
        )


@pytest.mark.asyncio
async def test_save_rejects_identity_mismatch_payload() -> None:
    # Blocker #3: prep=concept/A but payload scope=concept/B -> must not persist.
    prep = _prep(scope_type="concept", scope_key="A")
    obs = _canonical_obs(scope_type="concept", scope_key="B")
    with pytest.raises(ScopeObservationPayloadValidationError):
        await save_scope_observation_fact(_FakeSession(), prep, obs)


def test_snapshot_readiness_mapping() -> None:
    assert _snapshot_readiness(_prep(pit_status_t="unavailable", members=())) == "unavailable"
    assert _snapshot_readiness(_prep(pit_status_t="historical_pit", members=())) == "no_members"
    assert _snapshot_readiness(_prep(pit_status_t="historical_pit", members=("m1",))) == "ready"


def test_market_persistence_diagnostic_text() -> None:
    assert "market_not_activated_for_historical_persistence" in MARKET_PERSISTENCE_DIAGNOSTIC


# ── Slice 4A1R — migrated Board current-state facts survive the persistence boundary ──

def test_migrated_board_capabilities_preserved_through_persistence() -> None:
    """Lock (not change) persistence behaviour for the migrated Board facts.

    ``_build_fact_values`` stores the WHOLE canonical observation under
    ``observation_payload`` without field selection, so the Slice 4A1R nested
    facts must reach persistence untouched.  This test asserts the nested paths
    explicitly so a future field-picking regression in the persistence owner
    cannot silently drop the migrated capability.
    """
    prep = _prep()
    obs = _canonical_obs()
    values = _build_fact_values(prep, obs, "review-obs-1.0.0")

    # Whole-payload identity (no copy / rename / field selection).
    assert values["observation_payload"] is obs

    payload = values["observation_payload"]
    # Every migrated Board current-state capability is present in the payload.
    trend = payload["trend"]
    assert "board_ready_member_count" in trend
    assert "trend_strength_distribution" in trend
    assert "dsa_vwap_dev_pct_distribution" in trend
    current_state = payload["structure"]["current_state"]
    assert "board_ready_member_count" in current_state
    assert "mean_active_orderblock_count" in current_state
    assert "latest_events" in current_state
    momentum = payload["momentum"]
    assert "change" in momentum and "denominator" in momentum["change"]
    assert "sqzmom" in momentum and "mean" in momentum["sqzmom"]
    volume = payload["participation"]["volume"]
    for key in (
        "badge",
        "ratio20_mean",
        "ratio200_mean",
        "percentile20_histogram",
        "percentile200_histogram",
    ):
        assert key in volume, key
