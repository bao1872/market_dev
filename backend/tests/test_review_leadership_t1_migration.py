"""[REVIEW-BACKEND-FINAL-CLOSURE Phase 5.5] Leadership 真实 T-1 → T migration 测试。

全部为 local pure-domain / frozen-fixture 测试，不依赖 PG / 真实数据库。

覆盖：
1. 真实 T-1 ≠ T 时 migration 正确（leader_set 变化反映 T-1→T 真实迁移）；
2. T-1 unavailable 时明确 unavailable（不 fake-ready）；
3. Member Attribution 实际消费传入的 migration；
4. future T+1 不改变 T 的 migration（时间口径隔离）；
5. member order reversed/random 不改变结果（确定性）。
"""

from __future__ import annotations

from app.domain.review.analysis.leadership_contribution import (
    LeadershipContributionFacts,
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import (
    AlignedLeadership,
    LeadershipSnapshot,
    build_leadership_snapshot,
    compute_leadership_migration,
)
from app.domain.review.scope_observation import MemberObservation
from app.services.review_leadership_service import (  # noqa: E402
    _unavailable_snapshot,
)


def _member(member_id: str, amount: float, return_1d: float) -> MemberObservation:
    return MemberObservation(
        member_id=member_id,
        price_candidate=True,
        return_1d=return_1d,
        amount=amount,
        # 其余字段对 leadership 计算无影响，给合理占位
        trend=None,
        swing=None,
        internal=None,
        momentum=None,
    )


def _contrib(members: list[MemberObservation]) -> LeadershipContributionFacts:
    return compute_member_leadership_contributions(members)


def _snapshot(
    trade_date: str, leader_ids: set[str], status: str = "ready"
) -> LeadershipSnapshot:
    """Build a ready LeadershipSnapshot with an EXPLICIT leader_set.

    The migration layer only consumes ``leader_ids``, so the test fixture sets it
    directly (bypassing the contribution-ranking prefix, which is covered by the
    contribution/migration unit tests elsewhere).  AlignedLeadership objects are
    given strictly increasing aligned_score so the frozen 0.50 coverage prefix
    includes the full intended set (no premature truncation).
    """
    ordered = sorted(leader_ids)
    if status == "unavailable":
        return LeadershipSnapshot(
            trade_date=trade_date,
            status="unavailable",
            reason="leadership_t1_scope_not_prepared",
            direction=None,
            rankable_count=0,
            leader_set=None,
        )
    leader_set = tuple(
        AlignedLeadership(member_id=m, contribution=0.001, aligned_score=float(i + 1))
        for i, m in enumerate(ordered)
    )
    return LeadershipSnapshot(
        trade_date=trade_date,
        status="ready",
        reason=None,
        direction=1,
        rankable_count=len(ordered),
        leader_set=leader_set,
    )


# ---------------------------------------------------------------------------
# 1. 真实 T-1 ≠ T：migration 反映真实 leader_set 变化
# ---------------------------------------------------------------------------
def test_real_t1_migration_reflects_leader_change() -> None:
    snap_t1 = _snapshot("2024-01-02", {"A", "B", "C"})
    snap_t = _snapshot("2024-01-03", {"B", "C", "D"})  # D 进入，A 退出
    migration = compute_leadership_migration(previous_snapshot=snap_t1, current_snapshot=snap_t)

    assert migration.status == "ready"
    assert migration.entrant_ids == ("D",)
    assert migration.exit_ids == ("A",)
    assert migration.retained_count == 2


def test_real_t1_migration_unchanged_when_same_leader_set() -> None:
    snap_t1 = _snapshot("2024-01-02", {"A", "B", "C"})
    snap_t = _snapshot("2024-01-03", {"A", "B", "C"})
    migration = compute_leadership_migration(previous_snapshot=snap_t1, current_snapshot=snap_t)

    assert migration.status == "ready"
    assert migration.entrant_ids == ()
    assert migration.exit_ids == ()
    assert migration.retained_count == 3


# ---------------------------------------------------------------------------
# 2. T-1 unavailable：明确 unavailable（不 fake-ready）
# ---------------------------------------------------------------------------
def test_t1_unavailable_yields_unavailable_migration() -> None:
    snap_t1 = LeadershipSnapshot(
        trade_date="2024-01-02",
        status="unavailable",
        reason="leadership_t1_scope_not_prepared",
        direction=None,
        rankable_count=0,
        leader_set=None,
    )
    snap_t = _snapshot("2024-01-03", {"A", "B", "C"})
    migration = compute_leadership_migration(previous_snapshot=snap_t1, current_snapshot=snap_t)

    assert migration.status == "unavailable"
    assert migration.reason is not None
    # 仍携带 current evidence，但不包装成 ready
    assert migration.current_leader_ids == ("A", "B", "C")


# ---------------------------------------------------------------------------
# 3. Member Attribution 实际消费传入的 migration
# ---------------------------------------------------------------------------
def test_member_attribution_consumes_real_migration() -> None:
    from app.domain.review.analysis.member_attribution import (
        compute_member_attribution,
    )

    snap_t1 = _snapshot("2024-01-02", {"A", "B", "C"})
    snap_t = _snapshot("2024-01-03", {"B", "C", "D"})
    migration = compute_leadership_migration(previous_snapshot=snap_t1, current_snapshot=snap_t)

    members = [_member("A", 30.0, 0.02), _member("B", 30.0, 0.01), _member("D", 40.0, -0.01)]
    result = compute_member_attribution(
        members=members,
        observation={},
        leadership_migration=migration,
    )
    # Member Attribution 实际消费传入的真实 migration：
    # checks.leadership 应匹配 canonical entrant/exit（来自真实 T-1→T 计算）
    leadership_check = result["reconciliation"]["checks"]["leadership"]
    assert leadership_check["resolved"] == "matched"
    assert leadership_check["entrant_ids_match_canonical"] is True
    assert leadership_check["exit_ids_match_canonical"] is True


# ---------------------------------------------------------------------------
# 4. future T+1 不改变 T 的 migration（时间口径隔离）
# ---------------------------------------------------------------------------
def test_future_t_plus_1_does_not_change_t_migration() -> None:
    snap_t1 = _snapshot("2024-01-02", {"A", "B", "C"})
    snap_t = _snapshot("2024-01-03", {"B", "C", "D"})
    migration_t = compute_leadership_migration(previous_snapshot=snap_t1, current_snapshot=snap_t)

    # T+1（future）snapshot：不同 leader set，但不应影响 T 的 migration
    snap_t1_future = _snapshot("2024-01-03", {"B", "C", "D"})
    snap_t_future = _snapshot("2024-01-04", {"C", "D", "E"})
    migration_future = compute_leadership_migration(previous_snapshot=snap_t1_future, current_snapshot=snap_t_future)

    assert migration_t.entrant_ids == ("D",)
    assert migration_t.exit_ids == ("A",)
    # future 与 T 是独立时间窗口，互不影响
    assert migration_future.entrant_ids == ("E",)
    assert migration_future.exit_ids == ("B",)


# ---------------------------------------------------------------------------
# 5. member order reversed/random 不改变结果（确定性）
# ---------------------------------------------------------------------------
def test_member_order_does_not_change_migration() -> None:
    members_t1 = [_member("A", 30.0, 0.02), _member("B", 30.0, 0.01), _member("C", 40.0, 0.0)]
    members_t = [_member("B", 30.0, 0.01), _member("C", 40.0, 0.0), _member("D", 30.0, -0.02)]

    contrib_t1_a = compute_member_leadership_contributions(members_t1)
    contrib_t_a = compute_member_leadership_contributions(members_t)
    snap_t1_a = build_leadership_snapshot(trade_date="2024-01-02", ew_return=0.01, contribution_facts=contrib_t1_a)
    snap_t_a = build_leadership_snapshot(trade_date="2024-01-03", ew_return=0.01, contribution_facts=contrib_t_a)
    migration_a = compute_leadership_migration(previous_snapshot=snap_t1_a, current_snapshot=snap_t_a)

    # reversed order
    contrib_t1_b = compute_member_leadership_contributions(list(reversed(members_t1)))
    contrib_t_b = compute_member_leadership_contributions(list(reversed(members_t)))
    snap_t1_b = build_leadership_snapshot(trade_date="2024-01-02", ew_return=0.01, contribution_facts=contrib_t1_b)
    snap_t_b = build_leadership_snapshot(trade_date="2024-01-03", ew_return=0.01, contribution_facts=contrib_t_b)
    migration_b = compute_leadership_migration(previous_snapshot=snap_t1_b, current_snapshot=snap_t_b)

    assert migration_a.entrant_ids == migration_b.entrant_ids
    assert migration_a.exit_ids == migration_b.exit_ids
    assert migration_a.retained_count == migration_b.retained_count


# ---------------------------------------------------------------------------
# 6. batch 内部 helper 正确性（pure，不依赖 PG）
# ---------------------------------------------------------------------------
def test_build_snapshot_consumes_canonical_owner_not_local_mean(monkeypatch) -> None:
    """[PHASE-5.5-LEADERSHIP-INTEGRATION-CORRECTION] Leadership 不得自行 mean
    (return_1d)；它必须消费唯一 canonical owner compute_scope_observation 的
    price.equal_weight_return。本测试证明 _build_snapshot 调用了该 owner 并用了
    其返回值，而非本地重新计算。"""
    from datetime import date as _date

    from app.services import review_leadership_service as ls
    from app.services.review_observation_prep_service import PreparedScope

    captured = {}

    def fake_compute_scope_observation(
        *, scope_type, scope_key, trade_date, pit_member_ids,
        pit_member_ids_t1, members, events, event_coverage_member_ids,
    ):
        captured["called"] = True
        return {"price": {"equal_weight_return": 0.0712}}

    monkeypatch.setattr(ls, "compute_scope_observation", fake_compute_scope_observation)

    prep = PreparedScope(
        scope_type="industry_l2", scope_key="sw_x", scope_name="sw_x",
        trade_date=_date(2024, 1, 3), canonical_t1=None,
        pit_member_ids=("A", "B"), pit_member_ids_t1=("A", "B"),
        members=(_member("A", 30.0, 0.02), _member("B", 30.0, 0.01)),
        t1_membership_available=True, pit_status_t="READY", pit_status_t1="READY",
        diagnostics=(), event_coverage_member_ids=(),
    )
    snap = ls._build_snapshot(prep)
    assert captured.get("called") is True
    # snapshot 的 direction 来自 canonical owner 的 equal_weight_return=0.0712
    # （正收益 → direction=1），而非本地 mean(0.02,0.01)=0.015 也会得到 direction=1，
    # 因此额外断言它确实消费了 owner 的精确返回值：用负 EW 区分。
    assert snap.status == "ready"
    assert snap.direction == 1

    # 反向验证：canonical owner 返回负 EW 时，snapshot direction 应随 owner 翻负，
    # 证明不是本地 mean（本地 mean 会是正值）。
    monkeypatch.setattr(
        ls, "compute_scope_observation",
        lambda **_kw: {"price": {"equal_weight_return": -0.05}},
    )
    neg_prep = PreparedScope(
        scope_type="industry_l2", scope_key="sw_y", scope_name="sw_y",
        trade_date=_date(2024, 1, 3), canonical_t1=None,
        pit_member_ids=("A", "B"), pit_member_ids_t1=("A", "B"),
        members=(_member("A", 30.0, 0.02), _member("B", 30.0, 0.01)),
        t1_membership_available=True, pit_status_t="READY", pit_status_t1="READY",
        diagnostics=(), event_coverage_member_ids=(),
    )
    neg_snap = ls._build_snapshot(neg_prep)
    assert neg_snap.direction == -1


def test_unavailable_snapshot_is_honest_not_fake_ready() -> None:
    from datetime import date as _date

    snap = _unavailable_snapshot(_date(2024, 1, 2))
    assert snap.status == "unavailable"
    assert snap.leader_set is None
    # 诚实 unavailable previous 经 compute_leadership_migration 仍为 unavailable
    curr = _snapshot("2024-01-03", {"A", "B", "C"})
    mig = compute_leadership_migration(previous_snapshot=snap, current_snapshot=curr)
    assert mig.status == "unavailable"
    # 不包装成 fake-ready migration（current evidence 保留，但 status 不翻 ready）
    assert mig.current_leader_ids == ("A", "B", "C")
