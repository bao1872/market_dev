"""Canonical Observation Fact Persistence (Round 1C).

Minimal persistence owner for ``review_scope_observation_facts``.

Ownership is only: serialize + validate contract shape + idempotent upsert +
read-back (prompt §1 / §11).  This module NEVER recomputes facts — no ratio /
HHI / transition / percentile / readiness algorithm re-derivation, no NULL
interpretation, no unavailable->0 coercion, no score / opportunity / risk /
strength / recommendation derivation (prompt §8 / §9).

Activation (prompt §15): only ``industry_l1 / industry_l2 / industry_l3 /
concept`` are persisted.  Market is NOT ACTIVATED FOR HISTORICAL PERSISTENCE
(prompt §16) and major_index / style are NOT ACTIVATED (prompt §17): a generic
loop passing them in must be blocked here even if the prep layer already guards.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_prep_service import PreparedScope

# Activated scope families for daily objective-fact persistence (prompt §15).
ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES: frozenset[str] = frozenset(
    {"industry_l1", "industry_l2", "industry_l3", "concept"}
)

# === A 级概念排除清单（A 步观察持久化 scope 排除）===
# 这些 concept 是交易机制/资格/监管状态/时效性事件标签，不是持续性投资主题。
# 对它们做 Scope Observation 无分析意义（覆盖过泛或零主题信号），A 步双写
# ReviewScopeObservationFact 时直接排除（不写表、不参与七段事实消费）。
# 口径（2026-08-17 扫描 5293 只活跃股票、389 个 concept 后定案）：
#   - 机制/资格池：融资融券(68.0%)、深股通(35.4%)、沪股通(30.8%)、专精特新(20.3%)
#   - 时效性业绩事件：2026中报预增(9.4%)、2026一季报预增(1.9%)
#   - 事件/状态标签：股权转让(并购重组)(8.6%)、ST板块(3.8%)、摘帽(1.4%)
#   - 上市阶段标签：新股与次新股(1.6%)、注册制次新股(1.4%)、科创次新股(0.4%)
# 注意：这是显式确定性清单（SSOT），不使用易误伤的模糊子串匹配（如"重组"会命中
# "重组蛋白"等真实主题）。B/C 级（覆盖过泛但确属主题 / 地区政策类）按产品决策
# 明确不过滤。新增 A 级命名时在此追加。
CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES: frozenset[str] = frozenset(
    {
        # 机制 / 资格池
        "融资融券",
        "深股通",
        "沪股通",
        "专精特新",
        # 时效性业绩事件
        "2026中报预增",
        "2026一季报预增",
        # 事件 / 状态标签
        "股权转让(并购重组)",
        "ST板块",
        "摘帽",
        # 上市阶段标签
        "新股与次新股",
        "注册制次新股",
        "科创次新股",
    }
)


# 成员数下限：concept 成员数 <= 此值时样本过小，无统计意义，A 步不持久化。
CONCEPT_OBSERVATION_EXCLUDE_MIN_MEMBER_COUNT: int = 10


def is_scope_observation_persistence_excluded(
    *,
    scope_type: str,
    scope_name: str,
    member_count: int | None = None,
) -> bool:
    """判断 scope 是否应被 A 步观察持久化排除。

    仅对 concept scope 生效；industry_l1/l2/l3 永不排除（板块语义稳定）。
    排除依据（满足任一即排除）：
      1. 板块名为 A 级机制/资格/事件标签（显式清单，无主题语义）；
      2. member_count 非 None 且 <= CONCEPT_OBSERVATION_EXCLUDE_MIN_MEMBER_COUNT
         （成员样本过小，无统计意义）。
    member_count 由调用方在 prepare 后传入；prepare 前只做 name 过滤。
    """
    if scope_type != "concept":
        return False
    if scope_name in CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES:
        return True
    if (
        member_count is not None
        and member_count <= CONCEPT_OBSERVATION_EXCLUDE_MIN_MEMBER_COUNT
    ):
        return True
    return False

# Market is explicitly NOT activated for historical persistence: the current
# active universe cannot be used against a historical trade_date (prompt §16).
MARKET_PERSISTENCE_DIAGNOSTIC = (
    "market_not_activated_for_historical_persistence: "
    "market is current active universe, not historical PIT; fact not persisted"
)

# The ONLY legal top-level sections of a Canonical Observation payload.  Any
# extra key (e.g. a subjective opportunity_score / marker / ranking, or a legacy
# top-level "amount") or a missing canonical section must be rejected before
# persistence (Round 1C correction Blocker #1).  This is the contract shape, not
# a semantic recompute.  The canonical amount lives under ``price.amount``; a
# top-level ``amount`` is a legacy topology and is explicitly rejected (no silent
# compatibility fallback, no topology migration, no re-normalization here).
CANONICAL_TOP_LEVEL_SECTIONS: frozenset[str] = frozenset(
    {"scope", "price", "trend", "structure", "momentum", "participation", "chip"}
)


class ScopePersistenceNotActivatedError(Exception):
    """Raised when a non-activated scope type is passed to persistence."""


class ScopeObservationPayloadValidationError(Exception):
    """Raised when a payload is not a valid Canonical Observation payload.

    Used for both top-level contract-shape violations (missing / extra section,
    non-dict section) and scope identity mismatch (scope_type / scope_key /
    trade_date).  This is contract validation only — never a recompute of
    ratio / HHI / transition / percentile / breadth / readiness / state.
    """


def validate_scope_observation_payload(
    observation: dict[str, Any],
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date,
) -> None:
    """Contract-validate that a payload is a complete, identity-consistent
    Canonical Observation (Round 1C correction Blocker #1 / #3).

    Only checks:
    - top-level key set == the exact canonical section set (no extra subjective
      key, no missing canonical section);
    - every canonical section is a dict;
    - ``observation["scope"]`` identity (scope_type / scope_key / trade_date)
      matches the PreparedScope.

    It does NOT recompute any fact (save-only ownership, prompt §4): a legal
    partial axis (e.g. an empty denominator, an unavailable axis) is accepted as
    long as the full canonical structure and identity are intact.
    """
    if not isinstance(observation, dict):
        raise ScopeObservationPayloadValidationError(
            f"observation must be a dict, got {type(observation).__name__}"
        )
    actual = set(observation)
    if actual != CANONICAL_TOP_LEVEL_SECTIONS:
        missing = sorted(CANONICAL_TOP_LEVEL_SECTIONS - actual)
        extra = sorted(actual - CANONICAL_TOP_LEVEL_SECTIONS)
        raise ScopeObservationPayloadValidationError(
            "non-canonical top-level payload: "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    for section in CANONICAL_TOP_LEVEL_SECTIONS:
        if not isinstance(observation[section], dict):
            raise ScopeObservationPayloadValidationError(
                f"canonical section {section!r} must be a dict"
            )

    scope = observation["scope"]
    if not isinstance(scope, dict):
        raise ScopeObservationPayloadValidationError("scope section must be a dict")
    if scope.get("scope_type") != scope_type:
        raise ScopeObservationPayloadValidationError(
            f"scope_type mismatch: payload={scope.get('scope_type')!r} "
            f"expected={scope_type!r}"
        )
    if scope.get("scope_key") != scope_key:
        raise ScopeObservationPayloadValidationError(
            f"scope_key mismatch: payload={scope.get('scope_key')!r} expected={scope_key!r}"
        )
    if scope.get("trade_date") != trade_date.isoformat():
        raise ScopeObservationPayloadValidationError(
            f"trade_date mismatch: payload={scope.get('trade_date')!r} "
            f"expected={trade_date.isoformat()!r}"
        )


def _snapshot_readiness(prep: PreparedScope) -> str:
    """Snapshot-level readiness derived only from existing explicit states.

    No subjective coverage threshold is introduced (prompt §20).  ``unavailable``
    when PIT(T) is unresolvable; ``no_members`` when PIT(T) resolved but no member
    observation was provided; otherwise ``ready`` (a real observation snapshot
    was computed).  Partial axes inside the Core output never downgrade readiness.
    """
    if prep.pit_status_t == "unavailable":
        return "unavailable"
    if not prep.members:
        return "no_members"
    return "ready"


def _build_fact_values(
    prep: PreparedScope,
    observation: dict[str, Any],
    algorithm_version: str | None,
) -> dict[str, Any]:
    """Serialize PreparedScope metadata + Core observation result into a fact row.

    ``observation`` is stored as-is (same object, no copy / rename / recompute).
    This is the single serialize point and is kept pure for unit testing.
    """
    return {
        "trade_date": prep.trade_date,
        "scope_type": prep.scope_type,
        "scope_key": prep.scope_key,
        "scope_name": prep.scope_name or None,
        "canonical_t1": prep.canonical_t1,
        "pit_member_count": len(prep.pit_member_ids),
        "pit_member_count_t1": len(prep.pit_member_ids_t1),
        "provided_member_count": len(prep.members),
        "t1_membership_available": prep.t1_membership_available,
        "pit_status_t": prep.pit_status_t,
        "pit_status_t1": prep.pit_status_t1,
        "readiness": _snapshot_readiness(prep),
        "observation_payload": observation,
        "diagnostics": list(prep.diagnostics),
        "algorithm_version": algorithm_version,
    }


async def save_scope_observation_fact(
    db: AsyncSession,
    prep: PreparedScope,
    observation: dict[str, Any],
    *,
    algorithm_version: str | None = None,
) -> ReviewScopeObservationFact:
    """Idempotently persist one daily Canonical Observation Fact snapshot.

    Idempotent upsert on the business grain (trade_date, scope_type, scope_key):
    the first save inserts one row; a repeated save with a different payload
    updates that same row (row_count stays 1, payload replaced).

    Guards (in order):
    - activation: only industry_l1/l2/l3 + concept are persisted; market /
      major_index / style raise ``ScopePersistenceNotActivatedError`` even if a
      generic loop passes them in (prompt §16 / §17).
    - failure semantics: PIT(T) unavailable or no members -> not written, raises
      ``ValueError`` (never a fake ``observation_payload={}``) (prompt §19A).
    - contract validation: ``observation`` must be a complete, identity-consistent
      Canonical Observation payload (exact canonical top-level set + scope
      identity match); otherwise ``ScopeObservationPayloadValidationError``
      (Round 1C correction Blocker #1 / #3).
    """
    if prep.scope_type not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES:
        raise ScopePersistenceNotActivatedError(
            f"scope_type={prep.scope_type!r} not activated for observation persistence"
        )
    if prep.pit_status_t == "unavailable" or not prep.members:
        raise ValueError(
            "cannot persist fact for unavailable/incomplete scope: "
            f"pit_status_t={prep.pit_status_t!r}, provided_member_count={len(prep.members)}"
        )
    validate_scope_observation_payload(
        observation,
        scope_type=prep.scope_type,
        scope_key=prep.scope_key,
        trade_date=prep.trade_date,
    )

    values = _build_fact_values(prep, observation, algorithm_version)
    stmt = pg_insert(ReviewScopeObservationFact).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "scope_type", "scope_key"],
        set_={
            "scope_name": stmt.excluded.scope_name,
            "canonical_t1": stmt.excluded.canonical_t1,
            "pit_member_count": stmt.excluded.pit_member_count,
            "pit_member_count_t1": stmt.excluded.pit_member_count_t1,
            "provided_member_count": stmt.excluded.provided_member_count,
            "t1_membership_available": stmt.excluded.t1_membership_available,
            "pit_status_t": stmt.excluded.pit_status_t,
            "pit_status_t1": stmt.excluded.pit_status_t1,
            "readiness": stmt.excluded.readiness,
            "observation_payload": stmt.excluded.observation_payload,
            "diagnostics": stmt.excluded.diagnostics,
            "algorithm_version": stmt.excluded.algorithm_version,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.flush()
    fact = await get_scope_observation_fact(
        db, prep.trade_date, prep.scope_type, prep.scope_key
    )
    if fact is None:  # pragma: no cover - upsert always yields a row
        raise RuntimeError("scope observation fact missing after upsert")
    return fact


async def get_scope_observation_fact(
    db: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
) -> ReviewScopeObservationFact | None:
    """Read-back a single daily fact snapshot by its business grain."""
    stmt = select(ReviewScopeObservationFact).where(
        ReviewScopeObservationFact.trade_date == trade_date,
        ReviewScopeObservationFact.scope_type == scope_type,
        ReviewScopeObservationFact.scope_key == scope_key,
    )
    return (await db.execute(stmt)).scalars().first()


async def list_scope_observation_facts(
    db: AsyncSession,
    *,
    scope_type: str | None = None,
    scope_key: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[ReviewScopeObservationFact]:
    """List fact snapshots with optional filters, ordered by grain."""
    stmt = select(ReviewScopeObservationFact)
    if scope_type is not None:
        stmt = stmt.where(ReviewScopeObservationFact.scope_type == scope_type)
    if scope_key is not None:
        stmt = stmt.where(ReviewScopeObservationFact.scope_key == scope_key)
    if from_date is not None:
        stmt = stmt.where(ReviewScopeObservationFact.trade_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(ReviewScopeObservationFact.trade_date <= to_date)
    stmt = stmt.order_by(
        ReviewScopeObservationFact.trade_date,
        ReviewScopeObservationFact.scope_type,
        ReviewScopeObservationFact.scope_key,
    )
    return list((await db.execute(stmt)).scalars())


if __name__ == "__main__":
    # 自测：验证 activation set / readiness 派生逻辑。
    assert ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES == frozenset(
        {"industry_l1", "industry_l2", "industry_l3", "concept"}
    )
    assert "market" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "major_index" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "style" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    print("OK: review_observation_persistence_service imports verified")
