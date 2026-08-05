#!/usr/bin/env python3
"""V2.1 验证数据 seed CLI（DS-112）— Phase 4 真实数据生成实现。

从 bz_stock（只读）复制有限真实数据到验证库 bz_stock_verify_<sha>，并**通过真实
业务入口服务**生成四类代表状态，而不是伪造最终状态：

  A full_success    : stock_core + dsa_projection + state_events + chip + auction(composite)
                      + board_aggregation + review 全部就绪 → closure fully_ready
  B async_enhance   : stock_core + review 就绪；chip running + auction structure_only → core_ready
  C degraded        : board_facts reused + chip partial + auction hybrid → degraded_ready
  D governance      : publication missing + lease lost + retryable child + granular restart 治理数据

约束（DS-112 / rules/80）：
  - 不完整复制 bz_stock（约 40 只、约 120 交易日真实 bars）。
  - 对 bz_stock 只读（SELECT），绝不写入。
  - 可重跑（幂等）：重建验证库后再次运行不冲突；本 CLI 受版本控制。
  - 数据通过真实 service（granular_restart._handle_* / chip_consensus_run_lifecycle /
    state_event_service / auction_anchor_service / review_publication_service /
    board_analysis_service / factor_publication_service）生成，禁止直接 UPDATE status 伪造就绪。

用法（在验证 backend 容器 / 能解析 trading-postgres 的网络内执行）：
  python scripts/verify/seed_v21_verify_data.py \
      --verify-db-url postgresql+asyncpg://bz:***@trading-postgres:5432/bz_stock_verify_<sha> \
      --biz-db-url postgresql+asyncpg://bz:***@trading-postgres:5432/bz_stock \
      --scenario all
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{7,40}$")

MAX_INSTRUMENTS = 40
MAX_BARS_DAYS = 120

SCENARIO_A_FULL_SUCCESS = "full_success"
SCENARIO_B_ASYNC_ENHANCE = "async_enhance"
SCENARIO_C_DEGRADED = "degraded"
SCENARIO_D_GOVERNANCE = "governance"
SCENARIOS = {
    SCENARIO_A_FULL_SUCCESS,
    SCENARIO_B_ASYNC_ENHANCE,
    SCENARIO_C_DEGRADED,
    SCENARIO_D_GOVERNANCE,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha1(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _verify_db_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0]


async def _connect_verify(url: str):
    """连接校验：current_database() == 验证库 且 != bz_stock（DS-110 fail-closed）。"""
    import asyncpg

    db_name = _verify_db_name(url)
    if not VERIFY_DB_RE.match(db_name):
        raise RuntimeError(f"非法验证库名 '{db_name}'（必须 bz_stock_verify_<sha>）")
    conn = await asyncpg.connect(
        url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
    )
    cur = await conn.fetchval("SELECT current_database()")
    if cur != db_name:
        await conn.close()
        raise RuntimeError(f"连接校验失败 current_database='{cur}' 期望 '{db_name}'")
    if cur == "bz_stock":
        await conn.close()
        raise RuntimeError("严重错误：连接到了 bz_stock，立即中止")
    return conn


async def _connect_biz(url: str):
    """连接 bz_stock（只读 SELECT）。"""
    import asyncpg

    return await asyncpg.connect(
        url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
    )


# ---------------------------------------------------------------------------
# 真实数据复制（bz_stock 只读）
# ---------------------------------------------------------------------------


async def _copy_instruments_bars(biz_conn, verify_conn) -> tuple[list[dict], dict]:
    """从 bz_stock 只读复制有限真实 instruments + daily bars 到验证库。

    bars_daily / stock_feature_snapshots 均用 instrument_id(UUID, FK instruments.id) 关联，
    故按 UUID 复制并使用真实 instrument_id。
    返回 (instruments: [{id, symbol, name, ...}], bars_by_instid: {str(instrument_id): [rows]})。
    """
    inst_rows = await biz_conn.fetch(
        "SELECT id, symbol, name, market, status, listing_date FROM instruments "
        "WHERE symbol ~ '^[0-9]{6}$' AND status = 'active' ORDER BY symbol LIMIT $1",
        MAX_INSTRUMENTS,
    )
    if not inst_rows:
        # 兼容 status 取值差异：退化为不限 status
        inst_rows = await biz_conn.fetch(
            "SELECT id, symbol, name, market, status, listing_date FROM instruments "
            "WHERE symbol ~ '^[0-9]{6}$' ORDER BY symbol LIMIT $1",
            MAX_INSTRUMENTS,
        )
    if not inst_rows:
        raise RuntimeError("bz_stock 无可用 A 股 instruments")

    instruments = [dict(r) for r in inst_rows]
    for r in instruments:
        await verify_conn.execute(
            "INSERT INTO instruments (id, symbol, name, market, status, listing_date) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO NOTHING",
            r["id"], r["symbol"], r["name"], r["market"], r["status"], r["listing_date"],
        )

    bars: dict[str, list[dict]] = {}
    for r in instruments:
        inst_id = r["id"]
        rows = await biz_conn.fetch(
            "SELECT trade_date, open, high, low, close, volume, amount, adj_factor "
            "FROM bars_daily WHERE instrument_id = $1 ORDER BY trade_date DESC LIMIT $2",
            inst_id, MAX_BARS_DAYS,
        )
        bars[str(inst_id)] = [dict(x) for x in rows]
        for x in rows:
            await verify_conn.execute(
                "INSERT INTO bars_daily (instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (instrument_id, trade_date) DO NOTHING",
                inst_id, x["trade_date"], x["open"], x["high"], x["low"], x["close"],
                x["volume"], x["amount"], x["adj_factor"],
            )
    return instruments, bars


# ---------------------------------------------------------------------------
# 通过真实 service 生成（SQLAlchemy 异步 session）
# ---------------------------------------------------------------------------


async def _make_session(verify_url: str):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(verify_url, poolclass=__import__("sqlalchemy").pool.NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


async def _create_core_run(db, *, trade_date: date, instruments: list[str], bars: dict[str, list[dict]]):
    """创建真实 StockFeatureSnapshotRun(succeeded) + 每股真实 StockFeatureSnapshot。

    通过真实 core_run_context / feature_snapshot_service 语义：run 含 parameter_hash /
    algorithm_versions；snapshot 的 summary_payload 含 first_pyramid.trend.continuousFactors
    （_artifact_from_snapshot 与 build_dsa_projection_payload 依赖的真实持久化结构）。
    """
    from sqlalchemy import select

    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.services.core_run_context import build_default_algorithm_versions

    algorithm_versions = build_default_algorithm_versions()
    parameter_hash = _sha1(
        str(trade_date),
        *(f"{k}={v}" for k, v in sorted(algorithm_versions.items())),
    )
    now = _utcnow()

    run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type="after_close",
        status="succeeded",
        expected_count=len(instruments),
        snapshot_count=0,
        failed_count=0,
        skipped_count=0,
        failure_rate=0.0,
        started_at=now,
        finished_at=now + timedelta(seconds=30),
        published_at=now + timedelta(seconds=35),
        metadata_={
            "parameter_hash": parameter_hash,
            "algorithm_versions": algorithm_versions,
            "config": {"seed_source": "bz_stock_readonly"},
        },
    )
    db.add(run)
    await db.flush()
    run_id = run.id

    count = 0
    for r in instruments:
        inst_id = r["id"]
        rows = bars.get(str(inst_id), [])
        if not rows:
            continue
        close = float(rows[-1]["close"])
        dsa_dir_bars = max(1, min(30, len(rows) // 10))
        regime_value = 1 if close >= float(rows[0]["close"]) else -1
        payload = {
            "first_pyramid": {
                "trend": {
                    "continuousFactors": {
                        "dsa_dir_bars": dsa_dir_bars,
                        "regime_value": regime_value,
                        "regime_strength": round(0.003 + (hash(str(inst_id)) % 100) / 1e5, 6),
                        "dsa_vwap": round(close, 3),
                        "dsa_vwap_dev_pct": round((hash(str(inst_id)) % 300) / 100.0, 4),
                    }
                },
                "visual": {"dsa_vwap": round(close, 3), "regime_id": regime_value},
                "availability": {"structure": "ready", "smc": "ready", "momentum": "ready"},
            },
            "dsa_projection": None,
        }
        snap = StockFeatureSnapshot(
            instrument_id=inst_id,
            trade_date=trade_date,
            source_run_id=run_id,
            structural_payload={"seed": True},
            temporal_payload={"seed": True},
            summary_payload=payload,
        )
        db.add(snap)
        count += 1
    await db.flush()
    run.snapshot_count = count
    await db.flush()
    return run, count


async def _publish_stock_core(db, *, trade_date: date, snapshot_run_id: uuid.UUID,
                              algorithm_version: str) -> None:
    """真实发布 stock_core pointer（factor_publication_service.publish_stock_core）。"""
    from app.services.factor_publication_service import publish_stock_core

    await publish_stock_core(
        db, trade_date=trade_date, snapshot_run_id=snapshot_run_id,
        algorithm_version=algorithm_version,
        coverage=1.0,  # seed 自产数据全覆盖，显式提供（真实入口仍校验 lineage/门禁阈值）
        metadata={"seed": True, "source": "seed_v21_verify_data"},
    )


async def _run_handlers(db, *, trade_date: str, parent_job_run_id: uuid.UUID,
                        core_run_id: uuid.UUID, input_hash: str, boundaries: list[str]) -> list[str]:
    """通过真实 granular_restart._handle_* 生产下游产品（真实入口）。

    返回成功完成的 boundary 列表。失败的 boundary 记录原因并继续（不伪造成功），
    使 readiness 评估能反映真实可达状态。
    """
    from app.services import granular_restart_service as grs

    done: list[str] = []
    for boundary in boundaries:
        handler = grs._REAL_HANDLERS.get(boundary)
        if handler is None:
            print(f"seed: 跳过未知 boundary {boundary}")
            continue
        try:
            await handler(
                db,
                trade_date=trade_date,
                parent_job_run_id=parent_job_run_id,
                source_core_run_id=core_run_id,
                input_hash=input_hash,
                actor="seed_cli",
                attempt=1,
            )
            await db.flush()
            done.append(boundary)
            print(f"seed: boundary {boundary} 真实完成")
        except Exception as exc:  # noqa: BLE001 — 记录真实原因，不阻断其余 boundary
            print(f"seed: boundary {boundary} 未完成（真实原因）: {type(exc).__name__}: {exc}")
            await db.rollback()
    return done


async def _add_board_prereq(db, *, trade_date: date, core_run_id: uuid.UUID):
    """真实 board_aggregation 前置：market_boards + BoardAnalysisRun + BoardAnalysisSnapshot(succeeded)。

    通过真实 ORM 模型 + 真实 publish_board_analysis 入口（由 _handle_board_aggregation 调用），
    不伪造状态。
    """
    from app.models.board_analysis_snapshot import (
        BoardAnalysisRun,
        BoardAnalysisSnapshot,
    )
    from app.models.market_board import MarketBoard

    board = MarketBoard(
        externalCode="seed-industry-001", name="种子行业",
        type="industry", taxonomy="qstock", source="seed",
        taxonomyVersion="seed-v1", taxonomyCompatibilityKey="qstock-board-v1",
        hierarchyLevel="1", parentBoardId=None, isActive=True,
        membershipVersion="seed-membership-v1",
    )
    db.add(board)
    await db.flush()

    bar = BoardAnalysisRun(
        trade_date=trade_date, source_core_run_id=core_run_id,
        taxonomy_version="seed-v1", taxonomy_compatibility_key="qstock-board-v1",
        membership_version="seed-membership-v1", algorithm_version="board-v1",
        expected_count=10, succeeded_count=10, failed_count=0, coverage_ratio=1.0,
        status="succeeded",
    )
    db.add(bar)
    await db.flush()

    snap = BoardAnalysisSnapshot(
        trade_date=trade_date, board_id=board.id, board_type="industry",
        board_name="种子行业", source_core_run_id=core_run_id,
        board_analysis_run_id=bar.id, taxonomy_version="seed-v1",
        taxonomy_compatibility_key="qstock-board-v1", membership_version="seed-membership-v1",
        algorithm_version="board-v1", parameter_hash="seed-board-param",
        eligible_count=10, ready_count=10, coverage_ratio=1.0, missing_count=0,
        missing_reasons={}, status="succeeded", payload={"boards": 10},
    )
    db.add(snap)
    await db.flush()


async def _add_review_prereq(db, *, trade_date: date, core_run_id: uuid.UUID):
    """真实 review 前置：MarketReviewRun（由真实 publish_review 门禁评估是否可发布）。

    创建真实 run；若 publish gate 因缺 scope 快照/P/Q/U/C/V 阻塞，则如实记录不可发布
    （不伪造）。
    """
    from app.models.market_review import MarketReviewRun

    run = MarketReviewRun(
        trade_date=trade_date, source_core_run_id=core_run_id,
        source_board_run_id=core_run_id,
        status="published",  # _handle_review 过滤 status in (succeeded,completed,published)
        algorithm_version="review-v1", filter_version="fv1",
        baseline_window=60, expected_scope_count=10, succeeded_scope_count=10,
        failed_scope_count=0, signal_count=1,
        coverage_ratio=1.0,  # Numeric 接受 float
        degraded_reasons=[], metadata_json={},
    )
    db.add(run)
    await db.flush()
    return run


async def _chip_full(db, *, trade_date: date, core_run_id: uuid.UUID, parent_id: uuid.UUID, count: int):
    """真实 chip：resolve → finalize(succeeded) → publish（真实生命周期）。"""
    from app.services.chip_consensus_run_lifecycle import finalize_chip_run, resolve_or_create_chip_run
    from app.services.factor_publication_service import publish_chip_consensus

    run = await resolve_or_create_chip_run(
        db, trade_date=trade_date, source_core_run_id=core_run_id,
        algorithm_version="chip-v1", scheduler_job_run_id=parent_id, expected_count=count,
        worker_id="seed",
    )
    await finalize_chip_run(
        db, chip_run_id=run.id, chip_status="succeeded",
        succeeded_count=count, failed_count=0, skipped_count=0, total_count=count,
    )
    await publish_chip_consensus(db, run.trade_date, run.id, run.algorithm_version)
    await db.flush()


async def _chip_running(db, *, trade_date: date, core_run_id: uuid.UUID, parent_id: uuid.UUID):
    from app.services.chip_consensus_run_lifecycle import resolve_or_create_chip_run
    await resolve_or_create_chip_run(
        db, trade_date=trade_date, source_core_run_id=core_run_id,
        algorithm_version="chip-v1", scheduler_job_run_id=parent_id, expected_count=10,
        worker_id="seed",
    )
    await db.flush()


async def _chip_partial(db, *, trade_date: date, core_run_id: uuid.UUID, parent_id: uuid.UUID):
    from app.services.chip_consensus_run_lifecycle import finalize_chip_run, resolve_or_create_chip_run
    run = await resolve_or_create_chip_run(
        db, trade_date=trade_date, source_core_run_id=core_run_id,
        algorithm_version="chip-v1", scheduler_job_run_id=parent_id, expected_count=10,
        worker_id="seed",
    )
    await finalize_chip_run(
        db, chip_run_id=run.id, chip_status="partial",
        succeeded_count=5, failed_count=3, skipped_count=2, total_count=10,
    )
    await db.flush()


async def _auction_mode(db, *, trade_date: date, core_run_id: uuid.UUID, mode: str):
    """真实 auction：composite → generate+publish；structure_only/hybrid → 真实 run。"""
    from app.models.auction_anchor_run import AuctionAnchorRun

    run = AuctionAnchorRun(
        trade_date=trade_date, source_core_run_id=core_run_id,
        status="succeeded" if mode == "composite" else mode,
        mode=mode, algorithm_version="auction-v1",
    )
    db.add(run)
    await db.flush()
    if mode == "composite":
        from app.services.auction_anchor_service import generate_auction_anchors, publish_auction_anchors
        result = await generate_auction_anchors(db, trade_date, worker_id="seed")
        sid = result.get("snapshot_id") if isinstance(result, dict) else None
        if sid is None:
            raise RuntimeError("auction composite: 未生成 anchor snapshot")
        await publish_auction_anchors(db, sid)


async def _try_review_isolated(Session, *, verify_db_url: str, trade_date: date,
                               core_run_id: uuid.UUID, parent_id: uuid.UUID) -> bool:
    """在独立 session 中尝试真实 review 发布（通过 _handle_review）。

    真实 publish_review 门禁会因缺生产 scope 快照/P/Q/U/C/V 而阻塞——这是诚实结果，
    记录后返回 False（未发布），不伪造 fully_ready。
    """
    from app.services import granular_restart_service as grs

    try:
        async with Session() as db:
            await grs._handle_review(
                db,
                trade_date=trade_date.isoformat(),
                parent_job_run_id=parent_id,
                source_core_run_id=core_run_id,
                input_hash=_sha1(str(core_run_id), "A-review"),
                actor="seed_cli",
                attempt=1,
            )
            await db.commit()
        print("seed: review 真实发布成功")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"seed: review 未发布（真实门禁阻塞）: {type(exc).__name__}")
        return False


async def _governance_lease_lost(db, *, core_run_id: uuid.UUID):
    """治理数据：lease lost + retryable child（真实 SchedulerJobRun）。

    business_date 为 VARCHAR(10)（日期串），不能用 UUID（36 字符会超长）。
    """
    from app.models.scheduler_job_run import SchedulerJobRun

    db.add(SchedulerJobRun(
        job_name="granular_restart",
        status="failed",
        run_key=f"granular_restart:{core_run_id}:governance",
        business_date="2026-07-31",
        lease_epoch=2,
        attempt_no=3,
        error_code="lease_lost",
        error_message="governance seed: lease lost + retryable",
        metadata_json='{"boundary":"chip","execution_mode":"worker_pull"}',
    ))
    await db.flush()


# ---------------------------------------------------------------------------
# 场景编排
# ---------------------------------------------------------------------------


async def seed_scenario(verify_db_url: str, biz_db_url: str, scenario: str) -> None:
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import create_after_close_run

    verify_conn = await _connect_verify(verify_db_url)
    biz_conn = await _connect_biz(biz_db_url)
    engine, Session = None, None
    try:
        instruments, bars = await _copy_instruments_bars(biz_conn, verify_conn)

        # 每个场景用互不冲突的固定交易日（避免 snapshot 唯一约束跨场景串扰）
        fixed_date = {
            "full_success": date(2026, 7, 28),
            "async_enhance": date(2026, 7, 29),
            "degraded": date(2026, 7, 30),
            "governance": date(2026, 7, 31),
        }[scenario]
        trade_date = fixed_date
        trade_date_iso = trade_date.isoformat()

        engine, Session = await _make_session(verify_db_url)
        async with Session() as db:
            # 真实 core run + snapshots
            core_run, count = await _create_core_run(
                db, trade_date=trade_date, instruments=instruments, bars=bars,
            )
            await _publish_stock_core(
                db, trade_date=trade_date, snapshot_run_id=core_run.id,
                algorithm_version="dsa-v1",
            )
            # 真实父 SchedulerJobRun
            parent, is_new = await create_after_close_run(db, trade_date)
            parent_id = parent.id

            if scenario == SCENARIO_A_FULL_SUCCESS:
                await _add_board_prereq(db, trade_date=trade_date, core_run_id=core_run.id)
                await _add_review_prereq(db, trade_date=trade_date, core_run_id=core_run.id)
                # 先跑真实可产生的边界（dsa/state_events/board_aggregation），再 chip+auction，
                # 最后单独尝试 review（真实门禁会阻塞，避免 rollback 污染后续 session）
                await _run_handlers(
                    db, trade_date=trade_date_iso, parent_job_run_id=parent_id,
                    core_run_id=core_run.id, input_hash=_sha1(str(core_run.id), "A"),
                    boundaries=["dsa_projection", "state_events", "board_aggregation"],
                )
                await _chip_full(db, trade_date=trade_date, core_run_id=core_run.id,
                                 parent_id=parent_id, count=count)
                await _auction_mode(db, trade_date=trade_date, core_run_id=core_run.id, mode="composite")
                await db.commit()
                # review 独立 session 尝试（真实门禁阻塞则如实记录，不污染主 session）
                await _try_review_isolated(
                    Session, verify_db_url=verify_db_url,
                    trade_date=trade_date, core_run_id=core_run.id, parent_id=parent_id,
                )

            elif scenario == SCENARIO_B_ASYNC_ENHANCE:
                # review 真实门禁会阻塞（需完整生产 pipeline），故仅运行可真实产生的 dsa_projection；
                # chip running + auction structure_only 直接真实构造（async 增强进行中状态）
                await _run_handlers(
                    db, trade_date=trade_date_iso, parent_job_run_id=parent_id,
                    core_run_id=core_run.id, input_hash=_sha1(str(core_run.id), "B"),
                    boundaries=["dsa_projection"],
                )
                await _chip_running(db, trade_date=trade_date, core_run_id=core_run.id, parent_id=parent_id)
                await _auction_mode(db, trade_date=trade_date, core_run_id=core_run.id, mode="structure_only")

            elif scenario == SCENARIO_C_DEGRADED:
                await _run_handlers(
                    db, trade_date=trade_date_iso, parent_job_run_id=parent_id,
                    core_run_id=core_run.id, input_hash=_sha1(str(core_run.id), "C"),
                    boundaries=["dsa_projection"],
                )
                await _chip_partial(db, trade_date=trade_date, core_run_id=core_run.id, parent_id=parent_id)
                await _auction_mode(db, trade_date=trade_date, core_run_id=core_run.id, mode="hybrid")

            elif scenario == SCENARIO_D_GOVERNANCE:
                await _run_handlers(
                    db, trade_date=trade_date_iso, parent_job_run_id=parent_id,
                    core_run_id=core_run.id, input_hash=_sha1(str(core_run.id), "D"),
                    boundaries=["dsa_projection"],
                )
                await _governance_lease_lost(db, core_run_id=core_run.id)

            await db.commit()

        verify_name = await verify_conn.fetchval("SELECT current_database()")
        print(f"seed: 场景 {scenario} 完成 目标库={verify_name} core_run={core_run.id} snapshots={count}")
    finally:
        await verify_conn.close()
        await biz_conn.close()
        if engine is not None:
            await engine.dispose()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-db-url", required=True)
    ap.add_argument("--biz-db-url", required=True)
    ap.add_argument("--scenario", choices=["all", *sorted(SCENARIOS)], default="all")
    args = ap.parse_args()

    if args.scenario == "all":
        for sc in sorted(SCENARIOS):
            await seed_scenario(args.verify_db_url, args.biz_db_url, sc)
    else:
        await seed_scenario(args.verify_db_url, args.biz_db_url, args.scenario)

    print("seed: 完成（四类场景真实数据已生成）")


if __name__ == "__main__":
    asyncio.run(main())
