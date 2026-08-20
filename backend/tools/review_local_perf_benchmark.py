"""Review canonical runtime — LOCAL Frozen Dataset performance benchmark.

[REVIEW-BACKEND-FINAL-CLOSURE Phase 6] Local-only profiling harness.

NO database, NO PG, NO remote. Exercises the PURE canonical owners that form
the in-memory hot path of ``compute_run``:

    compute_scope_observation   (per scope, member facts -> observation)
    compute_internal_structure  (observation -> internal structure)
    compute_member_attribution  (members + observation + leadership -> attribution)
    leadership owners           (contributions -> snapshot -> migration)
    compose_canonical_review_scope (six-key composition)

DB-bound stages (``prepare_current_scope_observations_batch``,
``compute_current_static_scope_dynamics_batch``) require a live PostgreSQL and
are explicitly OUT OF SCOPE for this local run; they are noted but not timed
here. The pure owners are where CPU hotspots live.

Usage:
    PYTHONPATH=. python tools/review_local_perf_benchmark.py

Produces a per-stage timing + memory report for N in {32,128,512,1024,4096}
and a cProfile top-N dump for the largest N. Also runs a determinism check
(original / reversed / random member order) at a fixed N.
"""
from __future__ import annotations

import cProfile
import io
import json
import pstats
import random
import time
import tracemalloc
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.review.analysis.internal_structure import compute_internal_structure
from app.domain.review.analysis.leadership_contribution import (
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import (
    build_leadership_snapshot,
    compute_leadership_migration,
    serialize_leadership_migration,
)
from app.domain.review.analysis.member_attribution import (
    compute_member_attribution,
)
from app.domain.review.canonical_composition import compose_canonical_review_scope
from app.domain.review.review_capability import ScopeCapability
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

# -- Frozen Dataset parameters -------------------------------------------------

PER_SCOPE_MEMBERS = 60          # realistic industry/concept scope size
MEMBER_TOTAL_POOL = 4000         # union member universe
SCOPE_COUNTS = [32, 128, 512, 1024, 4096]
LARGEST_N = max(SCOPE_COUNTS)


def _make_member(member_id: str, *, seed: int) -> MemberObservation:
    """Deterministic-but-varied MemberObservation fixture."""
    r = (seed * 9301 + 49297) % 233280 / 233280.0
    trend = 1 if r > 0.5 else -1 if r < 0.3 else None
    swing = -1 if r > 0.6 else 1 if r < 0.2 else None
    internal = 1 if r > 0.7 else -1 if r < 0.25 else None
    momentum = "up" if r > 0.55 else "down" if r < 0.25 else "flat"
    return MemberObservation(
        member_id=member_id,
        price_candidate=True,
        return_1d=(r - 0.5) * 0.1,
        amount=1_000_000.0 * (0.2 + r),
        trend=trend,
        swing=swing,
        internal=internal,
        momentum=momentum,
        t1_trend=trend,
        t1_swing=swing,
        t1_internal=internal,
        t1_momentum=momentum,
        vol_ratio20=0.5 + r,
        amt_ratio20=0.4 + r,
        volume_t=10_000.0 * (0.3 + r),
        vol_ratio200=0.6 + r,
        vol_pct20=r,
        vol_pct200=r * 0.9,
        vol_zscore20=(r - 0.5) * 2.0,
        vol_zscore200=(r - 0.5) * 1.5,
        regime_strength=abs(r - 0.5),
        dsa_dir_bars=5.0 + r * 10.0,
        dsa_vwap_dev_pct=(r - 0.5) * 0.03,
        segment_id=int(r * 8),
        segment_direction=1.0 if r > 0.5 else -1.0,
        segment_bars=10.0 + r * 20.0,
        segment_change_pct=(r - 0.5) * 0.05,
        segment_slope=(r - 0.5) * 0.02,
        seg_vol_ratio=0.5 + r,
        seg_amt_ratio=0.4 + r,
        seg_vol_mean=1000.0 + r * 500.0,
        seg_amt_mean_prev=2000.0 + r * 800.0,
        structure_alignment_categorical="aligned" if r > 0.5 else "divergent",
        active_internal_ob_count=1.0 + r * 3.0,
        active_swing_ob_count=1.0 + r * 2.0,
        volatility_phase="high" if r > 0.6 else "low",
        momentum_direction_raw="up" if r > 0.55 else "down",
        momentum_change=(r - 0.5) * 0.1,
        sqzmom_delta=(r - 0.5) * 0.2,
        sqzmom_val=r,
        release_volume_ratio=None,
        momentum_volume_relation="expand" if r > 0.5 else "contract",
        bb_position=r,
        bb_width=0.02 + r * 0.03,
        vwap_ret_total=(r - 0.5) * 0.04,
        trailing_top_pct=0.8 + r * 0.1,
        trailing_bottom_pct=0.2 + r * 0.1,
    )


def _make_scope_members(scope_idx: int, n: int, order: str) -> list[MemberObservation]:
    """Build n MemberObservations for a scope, with controllable member order."""
    ids = [f"MEM-{scope_idx}-{i:04d}" for i in range(n)]
    if order == "reversed":
        ids = list(reversed(ids))
    elif order == "random":
        ids = list(ids)
        random.seed(scope_idx)
        random.shuffle(ids)
    return [_make_member(mid, seed=hash(mid) & 0xFFFF) for mid in ids]


def _leadership_for(members: list[MemberObservation], trade_date: str, ew: float) -> Any:
    contrib = compute_member_leadership_contributions(members)
    snap = build_leadership_snapshot(
        trade_date=trade_date, ew_return=ew, contribution_facts=contrib
    )
    return snap


@dataclass
class StageTimings:
    scope_observation: float = 0.0
    internal_structure: float = 0.0
    leadership: float = 0.0
    member_attribution: float = 0.0
    composition: float = 0.0


def run_benchmark(n_scopes: int, order: str) -> dict[str, Any]:
    """Run the pure canonical owner pipeline for n_scopes, return timings + memory."""
    tracemalloc.start()
    t0 = time.perf_counter()
    cpu0 = time.process_time()

    stage = StageTimings()
    compositions = []

    for s in range(n_scopes):
        members = _make_scope_members(s, PER_SCOPE_MEMBERS, order)
        trade_date = date(2026, 8, 10)
        trade_date_str = trade_date.isoformat()
        ew = 0.01 if s % 2 == 0 else -0.02

        # 1) scope observation
        ts = time.perf_counter()
        obs = compute_scope_observation(
            scope_type="industry_l1",
            scope_key=f"IND-{s}",
            trade_date=trade_date,
            pit_member_ids=[m.member_id for m in members],
            pit_member_ids_t1=[m.member_id for m in members],
            members=members,
            events=None,
            event_coverage_member_ids=(),
        )
        stage.scope_observation += time.perf_counter() - ts

        # 2) internal structure
        ts = time.perf_counter()
        internal = compute_internal_structure(obs)
        stage.internal_structure += time.perf_counter() - ts

        # 3) leadership (T-1 + T snapshots -> migration)
        ts = time.perf_counter()
        prev_snap = _leadership_for(members, "2026-08-07", ew * 0.8)
        curr_snap = _leadership_for(members, trade_date_str, ew)
        migration = compute_leadership_migration(
            previous_snapshot=prev_snap, current_snapshot=curr_snap
        )
        leader_layer = serialize_leadership_migration(migration)
        stage.leadership += time.perf_counter() - ts

        # 4) member attribution
        ts = time.perf_counter()
        attr = compute_member_attribution(
            members=members, observation=obs, leadership_migration=migration
        )
        stage.member_attribution += time.perf_counter() - ts

        # 5) composition
        ts = time.perf_counter()
        cap = ScopeCapability(
            scope_type="industry_l1",
            scope_name="Industry L1",
            persistence_activated=True,
            current_membership_available=True,
            historical_membership_available=True,
            historical_dynamics_runtime_wired=False,
            leadership_runtime_wired=True,
            member_attribution_available=True,
        )
        comp = compose_canonical_review_scope(
            scope_type="industry_l1",
            scope_key=f"IND-{s}",
            trade_date=trade_date_str,
            capability=cap,
            scope_observation={"status": "ready", **obs},
            internal_structure_facts=internal,
            leadership=leader_layer,
            member_attribution={"status": "ready", **attr},
        )
        stage.composition += time.perf_counter() - ts
        compositions.append(comp)

    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "n_scopes": n_scopes,
        "per_scope_members": PER_SCOPE_MEMBERS,
        "union_member_pool": MEMBER_TOTAL_POOL,
        "total_member_facts": n_scopes * PER_SCOPE_MEMBERS,
        "order": order,
        "wall_s": round(wall, 4),
        "cpu_s": round(cpu, 4),
        "peak_rss_mb": round(peak / 1_048_576, 2),
        "throughput_scopes_per_s": round(n_scopes / wall, 2),
        "stages": {
            "scope_observation": round(stage.scope_observation, 4),
            "internal_structure": round(stage.internal_structure, 4),
            "leadership": round(stage.leadership, 4),
            "member_attribution": round(stage.member_attribution, 4),
            "composition": round(stage.composition, 4),
        },
        "checksums": [hash(json.dumps(c, sort_keys=True, default=str)) for c in compositions[:3]],
    }


def main() -> None:
    print("=" * 78)
    print("REVIEW CANONICAL RUNTIME — LOCAL FROZEN DATASET BENCHMARK")
    print("NO PG / NO REMOTE DB. Pure in-memory canonical owners only.")
    print("=" * 78)

    results = []
    for n in SCOPE_COUNTS:
        res = run_benchmark(n, "original")
        results.append(res)
        print(
            f"\nN={n:5d} scopes | members/scope={PER_SCOPE_MEMBERS} | "
            f"total_member_facts={res['total_member_facts']:6d}"
        )
        print(f"  wall={res['wall_s']:.3f}s  cpu={res['cpu_s']:.3f}s  "
              f"peak={res['peak_rss_mb']:.1f}MB  "
              f"throughput={res['throughput_scopes_per_s']:.1f} scopes/s")
        print("  stages:")
        for name, val in res["stages"].items():
            pct = (val / res["wall_s"] * 100) if res["wall_s"] else 0
            print(f"    {name:22s} {val:.4f}s  ({pct:5.1f}%)")

    # Determinism gate
    print("\n" + "=" * 78)
    print(f"DETERMINISM GATE (N={LARGEST_N}, original vs reversed vs random member order)")
    print("=" * 78)
    base = run_benchmark(LARGEST_N, "original")
    for order in ("reversed", "random"):
        res = run_benchmark(LARGEST_N, order)
        same = base["checksums"] == res["checksums"]
        print(f"  order={order:9s} wall={res['wall_s']:.3f}s "
              f"composition_checksum_match={same}")

    # cProfile top hotspots at largest N
    print("\n" + "=" * 78)
    print(f"cPROFILE TOP HOTSPOTS (N={LARGEST_N})")
    print("=" * 78)
    profiler = cProfile.Profile()
    profiler.enable()
    run_benchmark(LARGEST_N, "original")
    profiler.disable()
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    stats.print_stats(25)
    print(stream.getvalue())

    out_path = "tools/review_local_perf_benchmark_report.json"
    with open(out_path, "w") as fh:
        json.dump(
            {
                "config": {
                    "per_scope_members": PER_SCOPE_MEMBERS,
                    "scope_counts": SCOPE_COUNTS,
                    "member_total_pool": MEMBER_TOTAL_POOL,
                },
                "results": results,
            },
            fh,
            indent=2,
        )
    print(f"\nReport written: {out_path}")


if __name__ == "__main__":
    main()
