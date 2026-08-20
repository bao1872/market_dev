"""REVIEW REAL-MARKET ACCEPTANCE — thin launcher / serializer (offline frozen dataset).

REVIEW-FROZEN-DATASET-ACCEPTANCE.  This script ONLY:
  1. loads frozen source facts from the canonical raw dataset
     (``backend/.perfdata/review/review-source-<sha>-v1``) via the shared probe
     loaders + prep owners; and
  2. calls the SAME canonical domain owners the production orchestrator uses
     (compute_scope_observation / compute_internal_structure /
     build_observation_series + compute_scope_dynamics_analysis /
     compute_member_leadership_contributions -> build_leadership_snapshot ->
     compute_leadership_migration / compute_member_attribution /
     compose_canonical_review_scope), then
  3. serializes the already-computed canonical payloads to jsonl / csv.

It NEVER re-derives EW / AW / HHI / Dynamics / Leadership / Attribution, never
creates a score/label/readiness.  All freshness/availability semantics come from
the frozen owners unchanged.

Data contract (FROZEN): current-static membership only.  Every industry/concept
historical scope aggregate is a current-member evolution (RESEARCH_PROXY_
CURRENT_STATIC_MEMBERSHIP), never strict PIT.  The canonical review pipeline
consumes first_pyramid_daily_state / first_pyramid_events / bars / memberships /
calendar from the BASE dataset; the SFS overlay is NOT consumed by these owners,
so no per-date overlay gap can block this run.

No DB, no SSH, no remote PostgreSQL, no temporary verify DB, no migration.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Repo-root / dataset constants (override via CLI)
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_BACKEND)
_DEFAULT_DATASET = os.path.join(
    _BACKEND, ".perfdata", "review", "review-source-c5c686e-v1"
)
_DEFAULT_OUT = os.path.join(_REPO_ROOT, "experiments", "review_real_market_acceptance")

TARGET_DATES = [
    "2026-07-29",
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
    "2026-08-04",
    "2026-08-10",
]
ACTIVE_FAMILIES = ("industry_l1", "industry_l2", "industry_l3", "concept")
HISTORY = 120  # production dynamics window: T + up to 120 pre-T observations


# ---------------------------------------------------------------------------
# Canonical owners (imported lazily to keep CLI/help fast)
# ---------------------------------------------------------------------------
def _owners():
    from app.domain.review.analysis.internal_structure import (
        compute_internal_structure,
    )
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
    from app.domain.review.analysis.observation_series import (
        build_observation_series,
    )
    from app.domain.review.analysis.scope_dynamics import (
        compute_scope_dynamics_analysis,
    )
    from app.domain.review.canonical_composition import (
        compose_canonical_review_scope,
    )
    from app.domain.review.review_capability import resolve_scope_capability
    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        ScopeReplaySpec,
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )
    from scripts.review_scope_dynamics_probe import (
        _build_replay_selection_from_specs,
        _load_capacity_facts,
        _load_parquet_rows,
    )

    return {
        "compute_scope_observation": compute_scope_observation,
        "compute_internal_structure": compute_internal_structure,
        "build_observation_series": build_observation_series,
        "compute_scope_dynamics_analysis": compute_scope_dynamics_analysis,
        "compute_member_attribution": compute_member_attribution,
        "compute_member_leadership_contributions": (
            compute_member_leadership_contributions
        ),
        "build_leadership_snapshot": build_leadership_snapshot,
        "compute_leadership_migration": compute_leadership_migration,
        "serialize_leadership_migration": serialize_leadership_migration,
        "compose_canonical_review_scope": compose_canonical_review_scope,
        "resolve_scope_capability": resolve_scope_capability,
        "ScopeReplaySpec": ScopeReplaySpec,
        "build_prepared_scopes_from_union": build_prepared_scopes_from_union,
        "build_union_fact_context_from_loaded_facts": (
            build_union_fact_context_from_loaded_facts
        ),
        "_build_replay_selection_from_specs": _build_replay_selection_from_specs,
        "_load_capacity_facts": _load_capacity_facts,
        "_load_parquet_rows": _load_parquet_rows,
    }


def _board_scope_type(board: dict) -> str | None:
    """Map a frozen board to its activation family.

    - concept board  -> ``concept``
    - industry board -> ``industry_`` + hierarchy_level.lower() (L1/L2/L3)
    """
    btype = str(board.get("type"))
    if btype == "concept":
        return "concept"
    if btype == "industry":
        hl = str(board.get("hierarchy_level") or "").lower()
        if hl in ("l1", "l2", "l3"):
            return f"industry_{hl}"
    return None


def _build_all_active_scope_specs(dataset_dir: str) -> list[Any]:
    """Enumerate ALL active industry_l1/l2/l3 + concept scopes from the frozen
    boards + current membership snapshot.  current-static only."""
    o = _owners()
    ScopeReplaySpec = o["ScopeReplaySpec"]
    boards = []
    for r in o["_load_parquet_rows"](dataset_dir, "boards"):
        if not r.get("is_active", True):
            continue
        st = _board_scope_type(r)
        if st is not None and st in ACTIVE_FAMILIES:
            boards.append((st, r))
    board_ids = {str(b[1]["id"]) for b in boards}
    by_board: dict[str, list] = {}
    for m in o["_load_parquet_rows"](dataset_dir, "board_memberships_current_snapshot"):
        bid = str(m["board_id"])
        if bid in board_ids:
            by_board.setdefault(bid, []).append(m["instrument_id"])
    specs: list[Any] = []
    for st, b in sorted(boards, key=lambda b_: str(b_[1]["id"])):
        bid = str(b["id"])
        # ``instrument_id`` arrives as a STRING from parquet; the frozen fact
        # dicts (states/bars) are keyed by UUID, so convert to UUID here or every
        # member resolves no bars -> price_candidate=False -> whole scope
        # unavailable (prep would silently drop every member).
        member_ids = tuple(uuid.UUID(str(i)) for i in by_board.get(bid, ()))
        specs.append(
            ScopeReplaySpec(
                scope_type=st,
                scope_key=bid,
                scope_name=str(b.get("name") or bid),
                member_ids=member_ids,
            )
        )
    return specs


# ---------------------------------------------------------------------------
# JSON / CSV serialization helpers (pure projection, NO recompute)
# ---------------------------------------------------------------------------
def _json_default(x: Any) -> Any:
    if isinstance(x, (date,)):
        return x.isoformat()
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if hasattr(x, "asdict"):
        return x.asdict()
    return str(x)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def _flatten_scalars(obj: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collapse nested dict/list into dotted scalar columns (pure projection)."""
    out = out if out is not None else {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten_scalars(v, key, out)
    elif isinstance(obj, list):
        if not obj:
            out[f"{prefix}.len"] = 0
        elif all(not isinstance(i, (dict, list)) for i in obj):
            out[prefix] = obj
        else:
            for i, v in enumerate(obj):
                _flatten_scalars(v, f"{prefix}[{i}]", out)
    elif isinstance(obj, (bool, int, float, str)) or obj is None:
        out[prefix] = obj
    return out


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_dumps(r) + "\n")


def _write_csv_from_scalar_rows(path: str, rows: list[dict]) -> None:
    columns: list[str] = []
    for r in rows:
        for k in _flatten_scalars(r).keys():
            if k not in columns:
                columns.append(k)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        seen_headers = set()
        for r in rows:
            flat = _flatten_scalars(r)
            seen_headers.update(flat.keys())
            w.writerow([flat.get(c, "") for c in columns])


# ---------------------------------------------------------------------------
# Single target-date computation (reuses production owners over frozen dataset)
# ---------------------------------------------------------------------------
def _run_target_date(
    dataset_dir: str,
    specs: list[Any],
    target: date,
    *,
    history: int,
    writers: dict[str, Any],
    stats: dict[str, Any],
) -> None:
    o = _owners()
    selection = o["_build_replay_selection_from_specs"](
        dataset_dir, specs, asof_override=target
    )
    trading_days = list(selection.trading_days)
    ai = bisect.bisect_left(trading_days, target)
    if ai >= len(trading_days) or trading_days[ai] != target:
        raise RuntimeError(f"{target} not a trading day in frozen calendar")
    window_dates = list(trading_days[max(0, ai - history + 1): ai + 1])

    prep_counters: dict[str, int] = {}
    prep_fallback_reasons: list[str] = []
    facts = o["_load_capacity_facts"](
        dataset_dir, specs, window_dates=window_dates, selection=selection
    )
    union_ctx = o["build_union_fact_context_from_loaded_facts"](
        t1_by_date=facts["t1_by_date"],
        states_by_date=facts["states_by_date"],
        bars=facts["bars"],
        events_by_date=facts["events_by_date"],
    )
    prepared = o["build_prepared_scopes_from_union"](
        trade_dates=facts["trade_dates"],
        scope_specs=facts["scope_specs"],
        union_ctx=union_ctx,
        membership_t1_by_scope=None,
        current_only_facts_by_date=facts["current_only_facts_by_date"],
        pit_status_t="current_static",
        pit_status_t1="current_static",
        t1_membership_available=False,
        prep_counters=prep_counters,
        prep_fallback_reasons=prep_fallback_reasons,
    )

    scope_stats = stats.setdefault("per_scope", {})
    for spec in specs:
        sk = str(spec.scope_key)
        series = prepared.get(sk)
        stat = scope_stats.setdefault(
            sk,
            {
                "scope_type": spec.scope_type,
                "scope_name": spec.scope_name,
                "member_count": len(spec.member_ids),
                "per_date": {},
            },
        )
        if not series or len(series) != len(window_dates):
            stat["per_date"][target.isoformat()] = {
                "status": "unavailable_current",
                "reason": "no_prepared_series",
            }
            stats["unavailable_current"] += 1
            stats["exceptions"] += 1
            continue

        # --- Scope Observation (per window date, once per scope per date) ---
        snapshots: list[dict] = []
        obs_by_idx: list[dict] = []
        for ps in series:
            obs = o["compute_scope_observation"](
                scope_type=ps.scope_type,
                scope_key=ps.scope_key,
                trade_date=ps.trade_date,
                pit_member_ids=[str(i) for i in ps.pit_member_ids],
                pit_member_ids_t1=[str(i) for i in ps.pit_member_ids_t1],
                members=ps.members,
                events=ps.events,
                t1_membership_available=ps.t1_membership_available,
                event_coverage_member_ids=ps.event_coverage_member_ids,
            )
            obs_by_idx.append(obs)
            snapshots.append(
                {
                    "trade_date": ps.trade_date.isoformat(),
                    "readiness": "ready",
                    "payload": obs,
                }
            )

        # --- Historical Dynamics (120D window ending at target) ---
        obs_series = o["build_observation_series"](
            scope_type=spec.scope_type,
            scope_key=sk,
            from_date=window_dates[0],
            to_date=window_dates[-1],
            trading_dates=window_dates,
            snapshot_series=snapshots,
        )
        dyn = o["compute_scope_dynamics_analysis"](obs_series)
        hd = dyn["historical_dynamics"]
        phase = dyn["dynamics_phase"]

        # target T = last index (window ends at T)
        obs_T = obs_by_idx[-1]
        ps_T = series[-1]
        phase_T = phase[-1]
        status_T = phase_T.get("status", "unavailable_current")
        hd_T = {k: arr[-1] for k, arr in hd.items()}

        # --- Internal Structure ---
        internal_structure = o["compute_internal_structure"](obs_T)

        # --- Leadership (real, offline, current-static) ---
        contrib_T = o["compute_member_leadership_contributions"](ps_T.members)
        snap_T = o["build_leadership_snapshot"](
            trade_date=ps_T.trade_date.isoformat(),
            ew_return=(obs_T.get("price") or {}).get("equal_weight_return"),
            contribution_facts=contrib_T,
        )
        contrib_T1 = o["compute_member_leadership_contributions"](series[-2].members)
        snap_T1 = o["build_leadership_snapshot"](
            trade_date=series[-2].trade_date.isoformat(),
            ew_return=(obs_by_idx[-2].get("price") or {}).get("equal_weight_return"),
            contribution_facts=contrib_T1,
        )
        leadership_facts = o["compute_leadership_migration"](
            previous_snapshot=snap_T1, current_snapshot=snap_T
        )
        leadership_layer = o["serialize_leadership_migration"](leadership_facts)
        if leadership_layer.get("status") == "unavailable":
            # boundary adapt: leadership's honest vocab -> composition contract
            leadership_layer = {**leadership_layer, "status": "unavailable_current"}

        # --- Member Attribution ---
        member_attribution = o["compute_member_attribution"](
            members=ps_T.members,
            observation=obs_T,
            leadership_migration=leadership_facts,
        )

        # --- Canonical Composition (fixed 6-key owner) ---
        capability = o["resolve_scope_capability"](
            scope_type=spec.scope_type, scope_name=spec.scope_name
        )
        historical_dynamics_layer = {
            "status": status_T,
            "scope_type": spec.scope_type,
            "scope_key": sk,
            "trade_date": ps_T.trade_date.isoformat(),
            "historical_dynamics": hd,
            "dynamics_phase": phase,
        }
        composition = o["compose_canonical_review_scope"](
            scope_type=spec.scope_type,
            scope_key=sk,
            trade_date=ps_T.trade_date.isoformat(),
            capability=capability,
            scope_observation={"status": "ready", **obs_T},
            historical_dynamics=historical_dynamics_layer,
            internal_structure_facts=internal_structure,
            leadership=leadership_layer,
            member_attribution={"status": "ready", **member_attribution},
        )

        # --- record + serialize ---
        readiness = composition["composition_readiness"]
        stats["computed"] += 1
        stats[readiness] += 1
        stat["per_date"][target.isoformat()] = {"status": readiness}

        writers["canonical"].write(_dumps(composition) + "\n")

        row = {
            "trade_date": target.isoformat(),
            "scope_type": spec.scope_type,
            "scope_key": sk,
            "scope_name": spec.scope_name,
            **obs_T,
        }
        writers["scope_observation_rows"].append(row)

        writers["internal_structure_rows"].append(
            {
                "trade_date": target.isoformat(),
                "scope_type": spec.scope_type,
                "scope_key": sk,
                "scope_name": spec.scope_name,
                **internal_structure,
            }
        )
        writers["dynamics_rows"].append(
            {
                "trade_date": target.isoformat(),
                "scope_type": spec.scope_type,
                "scope_key": sk,
                "scope_name": spec.scope_name,
                **hd_T,
                "phase": phase_T,
            }
        )
        writers["leadership_rows"].append(
            {
                "trade_date": target.isoformat(),
                "scope_type": spec.scope_type,
                "scope_key": sk,
                "scope_name": spec.scope_name,
                **leadership_layer,
            }
        )
        writers["attribution_rows"].append(
            {
                "trade_date": target.isoformat(),
                "scope_type": spec.scope_type,
                "scope_key": sk,
                "scope_name": spec.scope_name,
                **member_attribution,
            }
        )
    # memory hygiene between dates
    del facts, union_ctx, prepared


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Review Real-Market Acceptance launcher")
    p.add_argument("--dataset-dir", default=_DEFAULT_DATASET)
    p.add_argument("--out-dir", default=_DEFAULT_OUT)
    p.add_argument("--dates", nargs="*", default=TARGET_DATES)
    p.add_argument("--history", type=int, default=HISTORY)
    args = p.parse_args(argv)

    dataset_dir = args.dataset_dir
    out_dir = args.out_dir
    history = args.history
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    specs = _build_all_active_scope_specs(dataset_dir)
    print(f"[acceptance] scopes resolved: {len(specs)}")
    for fam in ACTIVE_FAMILIES:
        print(f"[acceptance]   {fam}: {sum(1 for s in specs if s.scope_type == fam)}")

    stats: dict[str, Any] = {
        "code_sha": "workspace checked",  # filled by run_manifest
        "dataset_dir": dataset_dir,
        "target_dates": args.dates,
        "history": args.history,
        "requested_scope_count": len(specs),
        "requested_scope_date_count": len(specs) * len(args.dates),
        "computed": 0,
        "ready": 0,
        "insufficient_history": 0,
        "unavailable_current": 0,
        "exceptions": 0,
        "per_scope": {},
    }

    wanted = set(args.dates)
    delays: list[float] = []
    with open(os.path.join(out_dir, "canonical_compositions.jsonl"), "w", encoding="utf-8") as cfh:
        writers = {
            "canonical": cfh,
            "scope_observation_rows": [],
            "internal_structure_rows": [],
            "dynamics_rows": [],
            "leadership_rows": [],
            "attribution_rows": [],
        }
        for ds in args.dates:
            target = date.fromisoformat(ds)
            ts = time.time()
            _run_target_date(
                dataset_dir, specs, target, history=history, writers=writers, stats=stats
            )
            secs = time.time() - ts
            delays.append(secs)
            print(
                f"[acceptance] date={ds} computed so far={stats['computed']} "
                f"ready={stats['ready']} insufficient={stats['insufficient_history']} "
                f"unavailable={stats['unavailable_current']} ({secs:.1f}s)"
            )

        # ---- serialize flat outputs ----
        _write_jsonl(os.path.join(out_dir, "member_attribution.jsonl"), writers["attribution_rows"])
        _write_csv_from_scalar_rows(os.path.join(out_dir, "scope_observation.csv"), writers["scope_observation_rows"])
        _write_csv_from_scalar_rows(os.path.join(out_dir, "internal_structure.csv"), writers["internal_structure_rows"])
        _write_csv_from_scalar_rows(os.path.join(out_dir, "historical_dynamics.csv"), writers["dynamics_rows"])
        _write_csv_from_scalar_rows(os.path.join(out_dir, "leadership.csv"), writers["leadership_rows"])

    # ---- run manifest + execution summary ----
    files = ["canonical_compositions.jsonl", "scope_observation.csv",
             "internal_structure.csv", "historical_dynamics.csv",
             "leadership.csv", "member_attribution.jsonl"]
    manifest = {
        "dataset_dir": dataset_dir,
        "target_dates": stats["target_dates"],
        "history": stats["history"],
        "activated_families": list(ACTIVE_FAMILIES),
        "membership": {"mode": "current_static",
                       "research_proxy": "RESEARCH_PROXY_CURRENT_STATIC_MEMBERSHIP"},
        "sfs_overlay_used": False,
        "sfs_overlay_note": (
            "canonical review pipeline consumes first_pyramid_daily_state/events/bars "
            "from base dataset only; SFS overlay is NOT consumed by these owners."
        ),
        "output_files": files,
        "checksums": {
            f: hashlib.sha256(open(os.path.join(out_dir, f), "rb").read()).hexdigest()
            for f in files
        },
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    stats["elapsed_sec"] = round(time.time() - t0, 1)
    stats["per_date_sec"] = [round(d, 1) for d in delays]
    stats.pop("per_scope")  # per-scope detail kept only in canonical rows for size
    with open(os.path.join(out_dir, "execution_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "# Review Real-Market Acceptance\n\n"
            "Frozen offline dataset acceptance run. See `execution_summary.json` for "
            "engineering integrity counts and `canonical_compositions.jsonl` for the "
            "full 6-key canonical payload per (scope, trade_date). No market "
            "interpretation performed by the launcher.\n"
        )

    print("\n=== execution summary ===")
    print(f"requested scope-date : {stats['requested_scope_date_count']}")
    print(f"computed             : {stats['computed']}")
    print(f"ready                : {stats['ready']}")
    print(f"insufficient_history : {stats['insufficient_history']}")
    print(f"unavailable_current  : {stats['unavailable_current']}")
    print(f"exceptions           : {stats['exceptions']}")
    print(f"elapsed_sec          : {stats['elapsed_sec']}")
    print(f"output dir           : {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())