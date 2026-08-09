"""Phase 4B — Market-Only Stage B Chronological Canary（一次性工程验证脚本）。

只验证 historical replay engine 的工程机制：
observations persistence / chronology / previous-history accumulation /
idempotency / chunk transaction / resume。

不验证完整 Review 产品 readiness。

严格边界（Phase 4B §5 / §6 / §10）：
- 只处理 scope_type=market / scope_key=market
  （通过 bootstrap_single_date(scope_types={"market"}) 的正式 selector，
   不使用 runtime monkeypatch）
- source_kind=history_replay / review_run_id=NULL
- source_history_run_id 必须 == 指定 canonical run
- history_contract_version == review-history-v2
- 每个日期单独 short transaction（禁止 5-day giant transaction）
- 不写 MarketReviewRun

用法：
    python -m scripts.phase4b_market_canary dry-run --date 2026-02-06
    python -m scripts.phase4b_market_canary apply --dates 2026-02-06,2026-02-09,...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.services import review_bootstrap_service as bootstrap_mod

EXPECTED_SOURCE_RUN_ID = "be56dcd2-d2f8-4ff9-bd66-ad2ed83f3813"
EXPECTED_CONTRACT = "review-history-v2"

# 正式 scope selector（非 monkeypatch）：硬保证零非-market 写入
MARKET_ONLY: frozenset[str] = frozenset({"market"})


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


async def _observation_snapshot(session) -> dict[str, Any]:
    """当前 observation 表的分布快照（§16）。"""
    total = (
        await session.execute(text("SELECT count(*) FROM market_review_metric_observations"))
    ).scalar_one()
    rows = (
        await session.execute(
            text(
                "SELECT coalesce(source_kind,'NULL') AS sk, scope_type, trade_date::text AS td, "
                "count(*) AS c FROM market_review_metric_observations "
                "GROUP BY 1,2,3 ORDER BY 3,1,2"
            )
        )
    ).mappings().all()
    return {"total": int(total), "breakdown": [dict(r) for r in rows]}


async def _previous_observation_count(session, trade_date: date) -> dict[str, Any]:
    """§11 chronology proof：严格 trade_date < 当前日期的 market replay observation。"""
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS rows_before, "
                "count(DISTINCT trade_date) AS dates_before, "
                "coalesce(max(trade_date)::text,'NONE') AS max_date_before, "
                "count(*) FILTER (WHERE trade_date >= :td) AS future_or_same "
                "FROM market_review_metric_observations "
                "WHERE scope_type='market' AND scope_key='market' "
                "AND source_kind='history_replay' "
                "AND history_contract_version=:contract "
                "AND source_history_run_id=:run_id "
                "AND trade_date < :td"
            ),
            {"td": trade_date, "contract": EXPECTED_CONTRACT, "run_id": EXPECTED_SOURCE_RUN_ID},
        )
    ).mappings().one()
    return dict(row)


async def _lineage_audit(session, trade_date: date) -> dict[str, Any]:
    """§10 每日 observation contract 校验。"""
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE source_kind='history_replay') AS ok_source_kind, "
                "count(*) FILTER (WHERE review_run_id IS NULL) AS ok_run_null, "
                "count(*) FILTER (WHERE source_history_run_id=:run_id) AS ok_source_run, "
                "count(*) FILTER (WHERE history_contract_version=:contract) AS ok_contract, "
                "count(*) FILTER (WHERE scope_type='market' AND scope_key='market') AS ok_scope, "
                "count(*) FILTER (WHERE taxonomy_compatibility_key IS NULL) AS taxo_null, "
                "count(DISTINCT metric_code) AS metrics "
                "FROM market_review_metric_observations WHERE trade_date=:td"
            ),
            {"td": trade_date, "contract": EXPECTED_CONTRACT, "run_id": EXPECTED_SOURCE_RUN_ID},
        )
    ).mappings().one()
    return dict(row)


async def _metric_status(session, trade_date: date) -> list[dict[str, Any]]:
    """§13 raw metric 可用性 + §12 normalized status。"""
    rows = (
        await session.execute(
            text(
                "SELECT metric_code, component_name, status, "
                "(raw_value IS NOT NULL) AS has_raw, denominator "
                "FROM market_review_metric_observations "
                "WHERE trade_date=:td AND scope_type='market' "
                "ORDER BY metric_code, component_name"
            ),
            {"td": trade_date},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def run_dry_run(target: date) -> int:
    async with AsyncSessionLocal() as session:
        started = time.monotonic()
        result = await bootstrap_mod.bootstrap_single_date(
            session, trade_date=target, dry_run=True, scope_types=MARKET_ONLY,
        )
        elapsed = time.monotonic() - started
        # dry_run 严格零写入：显式 rollback 保证没有任何脏状态
        await session.rollback()
        snapshot = await _observation_snapshot(session)
        prev = await _previous_observation_count(session, target)

    print(json.dumps({
        "mode": "dry_run",
        "trade_date": target.isoformat(),
        "elapsed_sec": round(elapsed, 2),
        "result": result,
        "previous_observations": prev,
        "observation_snapshot": snapshot,
    }, ensure_ascii=False, indent=2, default=str))

    if result.get("written"):
        print("FATAL: dry-run wrote data", file=sys.stderr)
        return 2
    scopes = result.get("scopes") or []
    if any(s.get("scope_type") != "market" for s in scopes):
        print("FATAL: non-market scope present", file=sys.stderr)
        return 2
    return 0


async def run_apply(targets: list[date]) -> int:
    """§9：chronological，每个日期单独 short transaction。"""
    report: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as session:
        before = await _observation_snapshot(session)

    for target in targets:
        # 每个日期一个独立 session + 独立 transaction（禁止跨日期 giant transaction）
        async with AsyncSessionLocal() as session:
            prev_before = await _previous_observation_count(session, target)
            started = time.monotonic()
            result = await bootstrap_mod.bootstrap_single_date(
                session, trade_date=target, dry_run=False, scope_types=MARKET_ONLY,
            )
            await session.commit()
            elapsed = time.monotonic() - started

        async with AsyncSessionLocal() as session:
            lineage = await _lineage_audit(session, target)
            metrics = await _metric_status(session, target)

        entry = {
            "trade_date": target.isoformat(),
            "txn_duration_sec": round(elapsed, 2),
            "previous_observations_before_this_date": prev_before,
            "result": result,
            "lineage_audit": lineage,
            "metric_rows": metrics,
        }
        report.append(entry)
        print(json.dumps(entry, ensure_ascii=False, indent=2, default=str), flush=True)

        if result.get("status") not in {"completed"}:
            print(f"STOP: {target} status={result.get('status')}", file=sys.stderr)
            break

    async with AsyncSessionLocal() as session:
        after = await _observation_snapshot(session)

    print(json.dumps({
        "mode": "apply_summary",
        "before": before,
        "after": after,
        "dates": [d.isoformat() for d in targets],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4B market-only Stage B canary")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dry-run")
    p_dry.add_argument("--date", required=True)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--dates", required=True, help="逗号分隔，必须严格递增")

    args = parser.parse_args()

    if args.cmd == "dry-run":
        return asyncio.run(run_dry_run(_parse_date(args.date)))

    targets = [_parse_date(x) for x in args.dates.split(",") if x.strip()]
    if targets != sorted(targets) or len(set(targets)) != len(targets):
        print("FATAL: dates must be strictly increasing and unique", file=sys.stderr)
        return 2
    return asyncio.run(run_apply(targets))


if __name__ == "__main__":
    raise SystemExit(main())
