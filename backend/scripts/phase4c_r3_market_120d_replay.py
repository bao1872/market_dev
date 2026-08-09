"""Phase 4C R3 — Market-only 120 trading-day historical replay（FIRST_BLOCKER remediation）。

RTM Requirement R3：Market 120-trading-day baseline。
5-day canary（Phase 4B）只证明 engine 机制正确，不能替代 120 日 baseline。

严格边界：
- 只处理 scope_type=market（正式 selector ``bootstrap_single_date(scope_types={"market"})``，
  不使用 monkeypatch；不执行 industry / major_index / style / concept）
- canonical source **必须**通过正式 ``validate_canonical_history_run_readiness``
  实时解析（复用 orchestrator 的 ``_resolve_canonical_history_source``），
  **禁止 hardcode run id 作为产品逻辑**；expected run id 仅作为 assertion 用途传入
- source_kind=history_replay / review_run_id=NULL
- 每个 trade_date 单独 short transaction（禁止跨日期 giant transaction）
- 不写 MarketReviewRun
- 保留已有 canary observations（相同 logical upsert contract 自然扩展，禁止 delete/truncate）

用法：
    python -m scripts.phase4c_r3_market_120d_replay resolve --as-of 2026-08-07
    python -m scripts.phase4c_r3_market_120d_replay run --as-of 2026-08-07 \
        --expect-source be56dcd2-d2f8-4ff9-bd66-ad2ed83f3813 \
        --checkpoint-at 20 --chunk 5
    python -m scripts.phase4c_r3_market_120d_replay audit --as-of 2026-08-07
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.services import review_bootstrap_service as bootstrap_mod
from app.services.review_orchestrator_service import _resolve_canonical_history_source

MARKET_ONLY: frozenset[str] = frozenset({"market"})
WINDOW_SIZE = 120
DEFAULT_CHUNK = 5
DEFAULT_CHECKPOINT_AT = 20


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# G. canonical source：正式 validator 实时解析（禁止 hardcode 作为产品逻辑）
# ---------------------------------------------------------------------------
async def resolve_source(session) -> dict[str, Any]:
    source_run_id, contract_version = await _resolve_canonical_history_source(session)
    if source_run_id is None:
        return {"status": "not_ready", "run_id": None, "contract": contract_version}
    detail = await bootstrap_mod.validate_canonical_history_run_readiness(
        session, source_run_id, contract_version,
    )
    return {
        "status": detail.get("status"),
        "run_id": str(source_run_id),
        "contract": contract_version,
        "validator_detail": {k: str(v) for k, v in detail.items()},
    }


# ---------------------------------------------------------------------------
# H. 真实交易日解析（禁止自然日）
# ---------------------------------------------------------------------------
async def resolve_trading_dates(session, as_of: date, size: int = WINDOW_SIZE) -> list[date]:
    """截至 as_of（含）最后 ``size`` 个完整交易日，按时间升序返回。

    权威来源：trading_calendar（is_trading_day=true 且 status='OPEN'，
    status='UNKNOWN' 不作为权威交易日）。
    """
    rows = (
        await session.execute(
            text(
                "SELECT trade_date FROM trading_calendar "
                "WHERE market='A' AND is_trading_day = true AND status='OPEN' "
                "AND trade_date <= :as_of "
                "ORDER BY trade_date DESC LIMIT :size"
            ),
            {"as_of": as_of, "size": size},
        )
    ).scalars().all()
    return sorted(rows)


# ---------------------------------------------------------------------------
# K. R3 PASS contract 证据查询
# ---------------------------------------------------------------------------
async def audit_evidence(
    session, window: list[date], source_run_id: str, contract: str,
) -> dict[str, Any]:
    first, last = window[0], window[-1]
    params = {
        "first": first, "last": last,
        "run_id": source_run_id, "contract": contract,
    }

    overall = (
        await session.execute(
            text(
                "SELECT count(*) AS rows_total, "
                "count(DISTINCT trade_date) AS distinct_dates, "
                "min(trade_date)::text AS min_date, "
                "max(trade_date)::text AS max_date, "
                "count(*) FILTER (WHERE source_kind <> 'history_replay') AS bad_source_kind, "
                "count(*) FILTER (WHERE review_run_id IS NOT NULL) AS bad_review_run_id, "
                "count(*) FILTER (WHERE source_history_run_id <> :run_id) AS wrong_source_rows, "
                "count(*) FILTER (WHERE history_contract_version <> :contract) "
                "AS wrong_contract_rows, "
                "count(*) FILTER (WHERE scope_type <> 'market') AS non_market_rows "
                "FROM market_review_metric_observations "
                "WHERE scope_type='market' AND trade_date BETWEEN :first AND :last"
            ),
            params,
        )
    ).mappings().one()

    # future leakage：窗口外未来日期的 market replay 行
    future = (
        await session.execute(
            text(
                "SELECT count(*) FROM market_review_metric_observations "
                "WHERE scope_type='market' AND source_kind='history_replay' "
                "AND trade_date > :last"
            ),
            {"last": last},
        )
    ).scalar_one()

    # duplicate logical keys
    dup = (
        await session.execute(
            text(
                "SELECT count(*) FROM ("
                "  SELECT trade_date, scope_type, scope_key, metric_code, component_name, "
                "         source_kind, source_history_run_id, history_contract_version, "
                "         count(*) AS c "
                "  FROM market_review_metric_observations "
                "  WHERE scope_type='market' AND trade_date BETWEEN :first AND :last "
                "  GROUP BY 1,2,3,4,5,6,7,8 HAVING count(*) > 1"
                ") d"
            ),
            {"first": first, "last": last},
        )
    ).scalar_one()

    # rows/date 分布
    per_date = (
        await session.execute(
            text(
                "SELECT c AS rows_per_date, count(*) AS date_count FROM ("
                "  SELECT trade_date, count(*) AS c "
                "  FROM market_review_metric_observations "
                "  WHERE scope_type='market' AND trade_date BETWEEN :first AND :last "
                "  GROUP BY trade_date"
                ") t GROUP BY c ORDER BY c"
            ),
            {"first": first, "last": last},
        )
    ).mappings().all()

    # 窗口内缺失日期
    covered = set(
        (
            await session.execute(
                text(
                    "SELECT DISTINCT trade_date FROM market_review_metric_observations "
                    "WHERE scope_type='market' AND trade_date BETWEEN :first AND :last"
                ),
                {"first": first, "last": last},
            )
        ).scalars().all()
    )
    missing = [d.isoformat() for d in window if d not in covered]

    return {
        "window": {
            "count": len(window),
            "first": first.isoformat(),
            "last": last.isoformat(),
        },
        "overall": dict(overall),
        "future_rows_beyond_window": int(future),
        "duplicate_logical_keys": int(dup),
        "rows_per_date_distribution": [dict(r) for r in per_date],
        "missing_dates_in_window": missing,
    }


def evaluate_pass(evidence: dict[str, Any]) -> tuple[bool, list[str]]:
    """K. R3 PASS contract 判定（任一失败 → BROKEN/PARTIAL）。"""
    failures: list[str] = []
    o = evidence["overall"]
    if int(o["distinct_dates"]) != WINDOW_SIZE:
        failures.append(f"distinct_dates={o['distinct_dates']} != {WINDOW_SIZE}")
    if o["min_date"] != evidence["window"]["first"]:
        failures.append(f"min_date={o['min_date']} != {evidence['window']['first']}")
    if o["max_date"] != evidence["window"]["last"]:
        failures.append(f"max_date={o['max_date']} != {evidence['window']['last']}")
    for key in (
        "bad_source_kind", "bad_review_run_id", "wrong_source_rows",
        "wrong_contract_rows", "non_market_rows",
    ):
        if int(o[key]) != 0:
            failures.append(f"{key}={o[key]} != 0")
    if evidence["future_rows_beyond_window"] != 0:
        failures.append(f"future_rows={evidence['future_rows_beyond_window']} != 0")
    if evidence["duplicate_logical_keys"] != 0:
        failures.append(f"duplicate_logical_keys={evidence['duplicate_logical_keys']} != 0")
    if evidence["missing_dates_in_window"]:
        failures.append(f"missing_dates={len(evidence['missing_dates_in_window'])}")
    return (not failures), failures


# ---------------------------------------------------------------------------
# J. 分块执行：chronological / short transaction / resume idempotent
# ---------------------------------------------------------------------------
async def _covered_dates(session, window: list[date]) -> set[date]:
    return set(
        (
            await session.execute(
                text(
                    "SELECT DISTINCT trade_date FROM market_review_metric_observations "
                    "WHERE scope_type='market' AND source_kind='history_replay' "
                    "AND trade_date BETWEEN :first AND :last"
                ),
                {"first": window[0], "last": window[-1]},
            )
        ).scalars().all()
    )


async def run_replay(
    as_of: date, expect_source: str | None, chunk: int, checkpoint_at: int,
    resume: bool,
) -> int:
    async with AsyncSessionLocal() as session:
        src = await resolve_source(session)
        window = await resolve_trading_dates(session, as_of)
        already = await _covered_dates(session, window) if resume else set()

    print(json.dumps({
        "phase": "preflight",
        "canonical_source": src,
        "window": {
            "count": len(window),
            "first": window[0].isoformat() if window else None,
            "last": window[-1].isoformat() if window else None,
        },
        "already_covered_dates": len(already),
    }, ensure_ascii=False, indent=2), flush=True)

    if src["status"] != "ok":
        print(f"STOP: canonical source not ready: {src}", file=sys.stderr)
        return 2
    if expect_source and src["run_id"] != expect_source:
        print(
            f"STOP: resolved source {src['run_id']} != expected {expect_source}",
            file=sys.stderr,
        )
        return 2
    if len(window) != WINDOW_SIZE:
        print(
            f"STOP: trading dates count={len(window)} != {WINDOW_SIZE}",
            file=sys.stderr,
        )
        return 2

    pending = [d for d in window if d not in already]
    checkpoint_done = len(already) >= checkpoint_at
    processed = len(already)
    t_start = time.monotonic()

    for i in range(0, len(pending), chunk):
        batch = pending[i:i + chunk]
        for target in batch:
            async with AsyncSessionLocal() as session:
                t0 = time.monotonic()
                try:
                    result = await bootstrap_mod.bootstrap_single_date(
                        session, trade_date=target, dry_run=False,
                        scope_types=MARKET_ONLY,
                    )
                    await session.commit()
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    print(
                        f"STOP: {target} raised {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    return 2
                elapsed = time.monotonic() - t0

            processed += 1
            print(json.dumps({
                "phase": "date_done",
                "trade_date": target.isoformat(),
                "seq": processed,
                "txn_sec": round(elapsed, 2),
                "status": result.get("status"),
                "scopes": len(result.get("scopes") or []),
            }, ensure_ascii=False), flush=True)

            if result.get("status") != "completed":
                print(
                    f"STOP: {target} status={result.get('status')}",
                    file=sys.stderr,
                )
                return 2

        # J. 20 distinct dates checkpoint
        if not checkpoint_done and processed >= checkpoint_at:
            async with AsyncSessionLocal() as session:
                partial = await audit_evidence(
                    session, window[:processed], src["run_id"], src["contract"],
                )
            ok = (
                int(partial["overall"]["bad_source_kind"]) == 0
                and int(partial["overall"]["bad_review_run_id"]) == 0
                and int(partial["overall"]["wrong_source_rows"]) == 0
                and int(partial["overall"]["wrong_contract_rows"]) == 0
                and int(partial["overall"]["non_market_rows"]) == 0
                and partial["duplicate_logical_keys"] == 0
                and partial["future_rows_beyond_window"] == 0
                and not partial["missing_dates_in_window"]
            )
            print(json.dumps({
                "phase": "checkpoint",
                "at_dates": processed,
                "verdict": "GREEN" if ok else "RED",
                "elapsed_sec": round(time.monotonic() - t_start, 1),
                "evidence": partial,
            }, ensure_ascii=False, indent=2, default=str), flush=True)
            if not ok:
                print("STOP: checkpoint RED", file=sys.stderr)
                return 2
            checkpoint_done = True

    async with AsyncSessionLocal() as session:
        evidence = await audit_evidence(
            session, window, src["run_id"], src["contract"],
        )
    passed, failures = evaluate_pass(evidence)
    print(json.dumps({
        "phase": "final",
        "canonical_source": src,
        "total_elapsed_sec": round(time.monotonic() - t_start, 1),
        "evidence": evidence,
        "r3_verdict": "PASS" if passed else "BROKEN",
        "failures": failures,
    }, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if passed else 2


async def run_audit(as_of: date) -> int:
    async with AsyncSessionLocal() as session:
        src = await resolve_source(session)
        window = await resolve_trading_dates(session, as_of)
        if src["status"] != "ok" or len(window) != WINDOW_SIZE:
            print(json.dumps({
                "canonical_source": src, "window_count": len(window),
            }, ensure_ascii=False, indent=2), flush=True)
            return 2
        evidence = await audit_evidence(
            session, window, src["run_id"], src["contract"],
        )
    passed, failures = evaluate_pass(evidence)
    print(json.dumps({
        "canonical_source": src,
        "evidence": evidence,
        "r3_verdict": "PASS" if passed else "BROKEN",
        "failures": failures,
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if passed else 2


async def run_resolve(as_of: date) -> int:
    async with AsyncSessionLocal() as session:
        src = await resolve_source(session)
        window = await resolve_trading_dates(session, as_of)
        already = await _covered_dates(session, window) if window else set()
    print(json.dumps({
        "canonical_source": src,
        "trading_window": {
            "count": len(window),
            "first": window[0].isoformat() if window else None,
            "last": window[-1].isoformat() if window else None,
            "dates": [d.isoformat() for d in window],
        },
        "already_covered": sorted(d.isoformat() for d in already),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4C R3 market 120-day replay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("--as-of", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--as-of", required=True)
    p_run.add_argument("--expect-source", default=None)
    p_run.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    p_run.add_argument("--checkpoint-at", type=int, default=DEFAULT_CHECKPOINT_AT)
    p_run.add_argument("--no-resume", action="store_true")

    p_audit = sub.add_parser("audit")
    p_audit.add_argument("--as-of", required=True)

    args = parser.parse_args()
    if args.cmd == "resolve":
        return asyncio.run(run_resolve(_parse_date(args.as_of)))
    if args.cmd == "audit":
        return asyncio.run(run_audit(_parse_date(args.as_of)))
    if args.expect_source:
        uuid.UUID(args.expect_source)  # 格式校验（assertion 用途，非产品逻辑）
    return asyncio.run(run_replay(
        _parse_date(args.as_of), args.expect_source,
        args.chunk, args.checkpoint_at, not args.no_resume,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
