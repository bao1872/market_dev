"""Shadow-only execution runner for Canonical Scope Observation (Round 1B).

Proves that real canonical data can reliably drive
``compute_scope_observation`` per the PRD contract.  Results are written as
experimental evidence only and are never wired into Filter / Discovery /
publication / API / frontend.

Usage (CLI, run remotely with real DB):
    python -m app.services.review_observation_shadow \
        --end-date 2026-08-11 --days 5 --out-dir /tmp/obs_shadow

The CLI runs MARKET for the N most recent complete trading days, then discovers
and runs industry_l1 / concept scopes whose historical PIT membership is
available at those dates.  Explicit keys may be supplied via --industry / --concept.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.scope_observation import compute_scope_observation
from app.services.observation_prep import check_observation_invariants
from app.services.review_observation_persistence_service import (
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import (
    PreparedScope,
    list_recent_trading_days,
    prepare_scope,
)


@dataclass(frozen=True)
class ShadowScopeSpec:
    scope_type: str
    scope_key: str
    scope_name: str


def _evidence(prep: PreparedScope, obs: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scope": {
            "scope_type": prep.scope_type,
            "scope_key": prep.scope_key,
            "scope_name": prep.scope_name,
            "trade_date": prep.trade_date.isoformat(),
            "canonical_t1": prep.canonical_t1.isoformat() if prep.canonical_t1 else None,
            "pit_status_t": prep.pit_status_t,
            "pit_status_t1": prep.pit_status_t1,
            "t1_membership_available": prep.t1_membership_available,
            "pit_member_count": len(prep.pit_member_ids),
            "pit_member_count_t1": len(prep.pit_member_ids_t1),
            "provided_member_count": len(prep.members),
            "diagnostics": list(prep.diagnostics),
        },
        "observation": obs,
        "sanity_checks": checks,
        "sanity_all_pass": all(c["ok"] for c in checks),
    }


async def run_shadow_scope(
    session: AsyncSession,
    spec: ShadowScopeSpec,
    trade_date: date,
    out_dir: Path,
    *,
    write_session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Prepare + compute one scope/date and write evidence JSON.

    ``session`` is the READ session (real canonical data, read-only).  When
    ``write_session`` is provided (e.g. the isolated verification DB), the
    canonical observation result is also persisted via
    ``save_scope_observation_fact`` (prompt §14 chain:
    prepare_scope -> compute_scope_observation -> save_scope_observation_fact).
    When ``write_session`` is None, persistence is skipped (shadow evidence only,
    never writes production data).
    """
    prep = await prepare_scope(session, spec.scope_type, spec.scope_key, trade_date)
    if prep.pit_status_t == "unavailable" or not prep.members:
        evidence = _evidence(prep, {"status": "skipped_no_members"}, [])
        return _write(evidence, out_dir, spec, trade_date)

    # Transition uses the T-1 membership set only when it is a truthfully
    # available historical PIT; otherwise empty -> transitions zero-eligible.
    pit_t1 = prep.pit_member_ids_t1 if prep.t1_membership_available else ()
    obs = compute_scope_observation(
        scope_type=spec.scope_type,
        scope_key=spec.scope_key,
        trade_date=trade_date,
        pit_member_ids=prep.pit_member_ids,
        pit_member_ids_t1=pit_t1,
        members=prep.members,
    )
    checks = check_observation_invariants(obs)
    evidence = _evidence(prep, obs, checks)
    if write_session is not None:
        # Persist into the isolated write session (never production bz_stock).
        saved = await save_scope_observation_fact(write_session, prep, obs)
        evidence["_persisted"] = {
            "fact_id": str(saved.id),
            "readiness": saved.readiness,
        }
    return _write(evidence, out_dir, spec, trade_date)


def _write(
    evidence: dict[str, Any],
    out_dir: Path,
    spec: ShadowScopeSpec,
    trade_date: date,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{spec.scope_type}_{spec.scope_key}_{trade_date.isoformat()}.json"
    path = out_dir / slug
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    evidence["_evidence_path"] = str(path)
    return evidence


async def discover_pit_available_boards(
    session: AsyncSession,
    board_type: str,
    hierarchy_level: str | None,
    trade_date: date,
) -> list[ShadowScopeSpec]:
    """Boards of a type that have a PIT definition version valid on trade_date."""
    from app.models.board_taxonomy import BoardDefinitionVersion
    from app.models.market_board import MarketBoard

    # board_type/hierarchy -> canonical scope_type (industry_l1, concept, ...).
    if board_type == "concept":
        scope_type = "concept"
    else:
        if hierarchy_level is None:
            raise ValueError("industry board discovery requires a hierarchy_level")
        scope_type = f"industry_{hierarchy_level.lower()}"

    stmt = (
        select(MarketBoard.id, MarketBoard.name)
        .join(BoardDefinitionVersion, BoardDefinitionVersion.board_id == MarketBoard.id)
        .where(
            MarketBoard.type == board_type,
            BoardDefinitionVersion.effective_from <= trade_date,
            or_(
                BoardDefinitionVersion.effective_to.is_(None),
                BoardDefinitionVersion.effective_to > trade_date,
            ),
        )
        .distinct()
        .order_by(MarketBoard.name)
    )
    if hierarchy_level is not None:
        stmt = stmt.where(MarketBoard.hierarchyLevel == hierarchy_level)
    return [
        ShadowScopeSpec(scope_type=scope_type, scope_key=str(row[0]), scope_name=row[1])
        for row in (await session.execute(stmt))
    ]


async def run_shadow_plan(
    session: AsyncSession,
    specs: list[ShadowScopeSpec],
    trade_dates: list[date],
    out_dir: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trade_date in trade_dates:
        for spec in specs:
            results.append(await run_shadow_scope(session, spec, trade_date, out_dir))
    return results


async def _main() -> None:
    import argparse

    from app.db import AsyncSessionLocal

    parser = argparse.ArgumentParser(description="Canonical Scope Observation shadow run")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp/obs_shadow"))
    parser.add_argument("--industry", nargs="*", default=[])
    parser.add_argument("--concept", nargs="*", default=[])
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        trade_dates = await list_recent_trading_days(session, args.end_date, args.days)
        print(f"trade_dates={[d.isoformat() for d in trade_dates]}")

        specs: list[ShadowScopeSpec] = [
            ShadowScopeSpec("market", "market", "全市场"),
        ]
        for key in args.industry:
            specs.append(ShadowScopeSpec("industry_l1", key, key))
        for key in args.concept:
            specs.append(ShadowScopeSpec("concept", key, key))

        if not args.industry:
            latest = trade_dates[-1] if trade_dates else args.end_date
            industry = await discover_pit_available_boards(session, "industry", "L1", latest)
            specs.extend(industry[:2])
        if not args.concept:
            latest = trade_dates[-1] if trade_dates else args.end_date
            concept = await discover_pit_available_boards(session, "concept", None, latest)
            specs.extend(concept[:2])

        results = await run_shadow_plan(session, specs, trade_dates, args.out_dir)
        ok = sum(1 for r in results if r.get("sanity_all_pass"))
        print(f"shadow results={len(results)} all_pass={ok}")
        for r in results:
            s = r["scope"]
            print(
                f"  {s['scope_type']}/{s['scope_key']} {s['trade_date']} "
                f"status={s['pit_status_t']} pit={s['pit_member_count']} "
                f"provided={s['provided_member_count']} all_pass={r['sanity_all_pass']}"
            )


if __name__ == "__main__":
    asyncio.run(_main())
