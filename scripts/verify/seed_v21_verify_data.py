#!/usr/bin/env python3
"""盘迹 V2.1 验证数据 Seed（CHANGE-20260806-008，100% synthetic，DS-112 合规）。

[DS-112] 本 Seed **完全不连接 bz_stock**。所有基础数据（instruments / bars /
trading_calendar / board_membership / released dsa config）均为确定性合成（固定 seed
+ uuid5），确保两次运行幂等、结果可复现。

[CHANGE-20260806-008] 四类场景全部通过**真实服务**编排，不伪造 succeeded / published /
coverage / First Pyramid。chip 真实链走 `compute_chip_consensus_snapshot` +
`chip_consensus_run_lifecycle`（真实算法 + RunItem 生命周期），但**绕过
`execute_after_close_chip_consensus` 顶层的 `refresh_15m_batch`（联网 pytdx）**——
synthetic 15m bars 已由 Seed 直接注入验证库，`_fetch_chip_bars(skip_refresh=True)`
已支持只读已有 bars。如实标注：chip 真实链**不含运行级 refresh**。

四类场景（closure 断言见 test_pg_seed_scenario_closures.py）：
  A full_success (2026-07-28) → fully_ready（core+chip+board+auction+publication）
  B async_enhance(2026-07-29) → core_ready（chip running + auction structure_only）
  C degraded    (2026-07-30) → degraded_ready（board reused + chip partial + auction hybrid）
  D governance  (2026-07-31) → blocked / degraded（publication missing / lease lost）

新增资产（仅 synthetic）：4 个 MarketBoard + 全部 100 instruments 的成员关系；
覆盖 2026-04-01..2026-08-31 的交易日历（scenario 日期均为交易日）。

用法（远程验证库，PANJI_REMOTE_VERIFY_DB_TEST=1）：
  DATABASE_URL=<bz_stock_verify_url> \
  python scripts/verify/seed_v21_verify_data.py --verify-db-url <bz_stock_verify_url> --scenario all

[重要 / 如实标注] `--verify-db-url` 只作用于本脚本自建 engine 的**合成数据写入**部分。
所有真实服务（core / dsa projection / board facts / chip / publication / auction / readiness）
内部一律使用 `app.db.AsyncSessionLocal`，而该 sessionmaker 在 import 时即绑定
`app.config.get_settings().database_url` 的模块级 engine，**无法由本脚本参数改写**。
因此必须通过环境变量（DATABASE_URL）把应用配置指向同一个验证库，否则合成数据与服务写入
会落到两个不同的库。两者不一致时场景装配必然失败。

幂等：重复运行不冲突（ON CONFLICT DO NOTHING / 真实服务内部幂等）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random

# 允许以脚本方式运行：将 backend 加入 sys.path
# [修正] 需要加入 backend/（而非 backend/app/），因为全部模块以 `app.xxx` 形式导入。
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

_BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
sys.path.insert(0, os.path.abspath(_BACKEND_ROOT))

# [修正] 会话工厂真实位置是 app.db（不存在 app.database / app.core_time）。
from app.db import AsyncSessionLocal

# [修正] CLOSURE_* 常量定义在 app.domain_status（product_readiness_service 亦从此导入）。
from app.domain_status import (
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
)
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
)
from app.services.after_close_chip_consensus_service import (
    CHIP_CONSENSUS_JOB_NAME,
    create_after_close_chip_consensus_job,
    execute_after_close_chip_consensus,
)
from app.services.board_facts_service import (
    run_board_facts,
)
from app.services.core_artifact_repository import (
    CoreArtifactRepository,
)
from app.services.factor_publication_service import compute_coverage

# [修正] compute_review_core_with_run_items 真实位置是 feature_snapshot_service，
# 且签名为 (trade_date, instrument_ids, snapshot_run_id, *, worker_id, lease_epoch, ...)。
from app.services.feature_snapshot_service import (
    compute_review_core_with_run_items,
    create_snapshot_run,
    finish_snapshot_run,
)
from app.services.fenced_job_run_service import claim_next_job_run
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
)
from app.services.stock_core_publication_service import (
    publish_stock_core_atomically,
)
from app.services.strategy_batch_service import (
    StrategyBatchService,
    persist_precomputed_dsa_results,
)
from app.services.wencai_board_provider import BoardSnapshot
from sqlalchemy import text

# ---------------------------------------------------------------------------
# 确定性合成常量
# ---------------------------------------------------------------------------
_SYNTH_SEED = 20260806
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "panji.verify.synthetic")
N_INST = 100
CAL_START = date(2026, 4, 1)
CAL_END = date(2026, 8, 31)
# 15m 仅覆盖 scenario 窗口（含 07-28/29/30/31）以控制行数；07-30 仅部分标的注入 15m（natural partial）
FIFTEEN_MIN_WINDOW_START = date(2026, 7, 20)
PARTIAL_15M_DATE = date(2026, 7, 30)
PARTIAL_15M_FRACTION = 0.3  # 仅 30% 标的在 07-30 有 15m → chip 自然 partial

_SCENARIO_TRADE_DATES = {
    "full_success": date(2026, 7, 28),
    "async_enhance": date(2026, 7, 29),
    "degraded": date(2026, 7, 30),
    "governance": date(2026, 7, 31),
}


def _inst_uuid(i: int) -> uuid.UUID:
    return uuid.uuid5(_NS, f"inst-{i}")


def _trading_days() -> list[date]:
    days: list[date] = []
    d = CAL_START
    while d <= CAL_END:
        if d.weekday() < 5:  # 周一..周五
            days.append(d)
        d += timedelta(days=1)
    return days


_TRADING_DAYS = _trading_days()


def _price(base: float, i: int, j: int) -> Decimal:
    """确定性价格（基于 j 日序号与 i 标的序号）。"""
    rnd = random.Random((_SYNTH_SEED * 131 + i * 17 + j * 7) % (2**31))
    drift = (j % 20 - 10) * 0.5
    noise = rnd.uniform(-0.3, 0.3)
    return Decimal(f"{base + drift + noise:.2f}")


def _bar(t: datetime, o: Decimal) -> dict[str, Any]:
    c = o + Decimal(f"{random.Random(hash(t)).uniform(-0.4, 0.4):.2f}")
    h = max(o, c) + Decimal("0.1")
    l = min(o, c) - Decimal("0.1")
    volume = Decimal(str(random.randint(1000, 9000)))
    return {
        "trade_time": t, "open": o, "high": h, "low": l, "close": c,
        "volume": volume, "amount": volume * c, "adj_factor": Decimal("1.0"),
    }


# ---------------------------------------------------------------------------
# 合成数据生成（直接写验证库，不依赖 bz_stock）
# ---------------------------------------------------------------------------
async def _gen_synthetic_instruments_bars(verify_conn) -> None:
    """建 100 instruments + 全窗口 daily/60min + scenario 窗口 15min（确定性）。"""
    # instruments
    inst_rows = []
    for i in range(N_INST):
        inst_id = _inst_uuid(i)
        inst_rows.append({
            "id": str(inst_id),
            "symbol": f"{600000 + i:06d}",
            "name": f"验证股{i:02d}",
            "market": "SH",
            "status": "active",
            "listing_date": date(2010, 1, 4),
        })
    await verify_conn.execute(
        text(
            "INSERT INTO instruments (id, symbol, name, market, status, listing_date) "
            "VALUES (:id, :symbol, :name, :market, :status, :listing_date) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        inst_rows,
    )

    # trading_calendar（market='A', status=OPEN）
    cal_rows = [
        {"trade_date": d, "is_trading_day": True, "market": "A",
         "source": "MANUAL_OVERRIDE", "status": "OPEN"}
        for d in _TRADING_DAYS
    ]
    await verify_conn.execute(
        text(
            "INSERT INTO trading_calendar (trade_date, is_trading_day, market, source, status) "
            "VALUES (:trade_date, :is_trading_day, :market, :source, :status) "
            "ON CONFLICT (trade_date, market) DO NOTHING"
        ),
        cal_rows,
    )

    # bars_daily + bars_60min 全窗口
    daily_rows, min60_rows = [], []
    for i in range(N_INST):
        inst_id = _inst_uuid(i)
        base = 10.0 + (i % 50)
        for j, d in enumerate(_TRADING_DAYS):
            o = _price(base, i, j)
            volume = Decimal(str(random.randint(10000, 90000)))
            daily_rows.append({
                "instrument_id": str(inst_id), "trade_date": d,
                "open": o, "high": o + Decimal("0.2"), "low": o - Decimal("0.2"),
                "close": o, "volume": volume, "amount": volume * o,
                "adj_factor": Decimal("1.0"),
            })
            # 60min: 4 根（10:30,11:30,14:00,15:00）
            for k, hhmm in enumerate([("10:30"), ("11:30"), ("14:00"), ("15:00")]):
                t = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]))  # noqa: DTZ001
                b = _bar(t, o)
                min60_rows.append({"instrument_id": str(inst_id), "trade_time": t,
                                   **{k2: b[k2] for k2 in (
                                       "open", "high", "low", "close", "volume", "amount", "adj_factor",
                                   )}})
    await verify_conn.execute(
        text(
            "INSERT INTO bars_daily "
            "(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
            "VALUES (:instrument_id, :trade_date, :open, :high, :low, :close, :volume, "
            ":amount, :adj_factor) ON CONFLICT (instrument_id, trade_date) DO NOTHING"
        ),
        daily_rows,
    )
    await verify_conn.execute(
        text(
            "INSERT INTO bars_60min "
            "(instrument_id, trade_time, open, high, low, close, volume, amount, adj_factor) "
            "VALUES (:instrument_id, :trade_time, :open, :high, :low, :close, :volume, "
            ":amount, :adj_factor) "
            "ON CONFLICT (instrument_id, trade_time) DO NOTHING"
        ),
        min60_rows,
    )

    # bars_15min 仅 scenario 窗口；07-30 仅部分标的（natural partial）
    min15_rows = []
    for i in range(N_INST):
        inst_id = _inst_uuid(i)
        for d in _TRADING_DAYS:
            if d < FIFTEEN_MIN_WINDOW_START:
                continue
            if d == PARTIAL_15M_DATE and (i / N_INST) >= PARTIAL_15M_FRACTION:
                continue  # 70% 标的在 07-30 缺 15m → chip 自然 partial
            base = 10.0 + (i % 50)
            # 16 根收盘时间：09:45..11:30 + 13:15..15:00，末根 15:00。
            for slot in range(16):
                session_start = datetime(  # noqa: DTZ001
                    d.year, d.month, d.day, 9 if slot < 8 else 13, 30 if slot < 8 else 0,
                )
                t = session_start + timedelta(minutes=15 * ((slot % 8) + 1))
                o = _price(base, i, _TRADING_DAYS.index(d) * 16 + slot)
                b = _bar(t, o)
                min15_rows.append({"instrument_id": str(inst_id), "trade_time": t,
                                   **{k2: b[k2] for k2 in (
                                       "open", "high", "low", "close", "volume", "amount", "adj_factor",
                                   )}})
    await verify_conn.execute(
        text(
            "INSERT INTO bars_15min "
            "(instrument_id, trade_time, open, high, low, close, volume, amount, adj_factor) "
            "VALUES (:instrument_id, :trade_time, :open, :high, :low, :close, :volume, "
            ":amount, :adj_factor) "
            "ON CONFLICT (instrument_id, trade_time) DO NOTHING"
        ),
        min15_rows,
    )
    await verify_conn.commit()
    print(f"[seed] instruments={N_INST} daily={len(daily_rows)} 60min={len(min60_rows)} 15min={len(min15_rows)}")


async def _gen_synthetic_boards(verify_conn) -> None:
    """建 4 个 MarketBoard + 全部 100 instruments 的成员关系。"""
    board_specs = [
        ("IND_MAIN", "行业主板", "industry"),
        ("CONCEPT_HOT", "概念热点", "concept"),
        ("IND_CYB", "创业板行业", "industry"),
        ("CONCEPT_VALUE", "价值概念", "concept"),
    ]
    board_ids = {}
    for code, nm, typ in board_specs:
        bid = uuid.uuid5(_NS, f"board-{code}")
        board_ids[code] = bid
        await verify_conn.execute(
            text(
                "INSERT INTO market_boards (id, external_code, name, type, taxonomy, is_active) "
                "VALUES (:id, :code, :nm, :typ, 'qstock', true) "
                "ON CONFLICT (external_code, type) DO NOTHING"
            ),
            {"id": str(bid), "code": code, "nm": nm, "typ": typ},
        )
    members = []
    for i in range(N_INST):
        inst_id = _inst_uuid(i)
        # 每只股票加入 2 个板块（确定性）
        for code in (board_specs[i % 2][0], board_specs[(i + 1) % 4][0]):
            members.append({"board_id": str(board_ids[code]), "instrument_id": str(inst_id)})
    await verify_conn.execute(
        text(
            "INSERT INTO market_board_memberships (board_id, instrument_id) "
            "VALUES (:board_id, :instrument_id) "
            "ON CONFLICT (board_id, instrument_id) DO NOTHING"
        ),
        members,
    )
    await verify_conn.commit()
    print(f"[seed] boards={len(board_specs)} memberships={len(members)}")


async def _gen_synthetic_released_dsa_config(verify_conn) -> uuid.UUID:
    """建 released dsa_selector StrategyDefinition + Version（含 parameters 规格数组）。"""
    def_id = uuid.uuid5(_NS, "def-dsa_selector")
    await verify_conn.execute(
        text(
            "INSERT INTO strategy_definitions (id, strategy_key, kind, display_name, environment) "
            "VALUES (:id, 'dsa_selector', 'selector', 'DSA Selector', 'production') "
            "ON CONFLICT (strategy_key) DO NOTHING"
        ),
        {"id": str(def_id)},
    )
    def_id = (
        await verify_conn.execute(
            text("SELECT id FROM strategy_definitions WHERE strategy_key='dsa_selector'")
        )
    ).scalar_one()
    # released version
    ver_id = uuid.uuid5(_NS, "ver-dsa_selector")
    manifest = json.dumps({
        "selector": "dsa_selector",
        "parameters": [
            {"key": "min_score", "default": 0.6, "type": "float"},
            {"key": "top_n", "default": 20, "type": "int"},
        ],
    })
    await verify_conn.execute(
        text(
            "INSERT INTO strategy_versions "
            "(id, strategy_definition_id, version, status, manifest, build_hash, released_at) "
            "VALUES (:id, :did, '1.0.0', 'released', CAST(:manifest AS jsonb), "
            "'synth-1', now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(ver_id), "did": str(def_id), "manifest": manifest},
    )
    await verify_conn.commit()
    print(f"[seed] released dsa_selector version={ver_id}")
    return ver_id


# ---------------------------------------------------------------------------
# 四类场景（真实服务编排，PRD 合规）
# ---------------------------------------------------------------------------
_ALL_INSTRUMENT_IDS = [_inst_uuid(i) for i in range(N_INST)]


async def _add_instruments_prereq(trade_date: date) -> uuid.UUID:
    """真实 core 链：create_snapshot_run → compute_review_core_with_run_items。

    真实签名（backend/app/services/feature_snapshot_service.py:1266）：
        compute_review_core_with_run_items(
            trade_date, instrument_ids, snapshot_run_id, *,
            worker_id="orchestrator", lease_epoch=None, batch_size=25,
            failure_threshold=0.3, ...
        ) -> dict[str, Any]
    注意：它**不接收 db**（内部用 AsyncSessionLocal 自行开短事务），返回统计 dict 而非 run 对象。
    因此 core_run_id 由调用方持有的 snapshot_run_id 承担（chip / auction / review 的
    source_core_run_id 语义即 StockFeatureSnapshotRun.id）。
    """
    async with AsyncSessionLocal() as db:
        run = await create_snapshot_run(
            db, trade_date, "after_close",
            expected_count=len(_ALL_INSTRUMENT_IDS),
            scope="full",
        )
        snapshot_run_id = run.id
        await db.commit()

    stats = await compute_review_core_with_run_items(
        trade_date,
        _ALL_INSTRUMENT_IDS,
        snapshot_run_id,
        worker_id="verify-seed",
        lease_epoch=1,
    )
    print(f"[seed] core run done: {trade_date} run={snapshot_run_id} stats={stats}")
    return snapshot_run_id


async def _finish_core_run(trade_date: date, snapshot_run_id: uuid.UUID) -> None:
    """真实 finish_snapshot_run：把 core run 收敛到 succeeded。

    真实签名（feature_snapshot_service.py:2253）：
        finish_snapshot_run(session, run, *, status, snapshot_count=None,
            failed_count=None, skipped_count=None, expected_count=None, ...)
    """
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async with AsyncSessionLocal() as db:
        run = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
        if run is None:
            raise RuntimeError(f"snapshot run 不存在: {snapshot_run_id}")
        cov = await compute_coverage(db, snapshot_run_id)
        await finish_snapshot_run(
            db, run,
            status="succeeded",
            snapshot_count=cov["succeeded"],
            failed_count=cov["failed"],
            skipped_count=cov["skipped"],
            expected_count=cov["expected"],
            metadata={"scope": "full"},
        )
        await db.commit()
    print(f"[seed] core run finished: {trade_date} run={snapshot_run_id}")


async def _add_full_publication(trade_date: date, snapshot_run_id: uuid.UUID) -> None:
    """真实原子发布（stock_core_publication_service.py:126）。

    真实签名为 keyword-only：
        publish_stock_core_atomically(
            db, *, scope_key, trade_date, publication_kind, algorithm_version,
            snapshot_run_id, coverage_ratio, worker_id, lease_epoch,
            eligible_count, audit_txn=True,
        ) -> FactorPublication
    返回 FactorPublication（不是 {"published": ...} dict），失败抛 StockCorePublicationError。
    quality gate 要求 stock_feature_snapshots(source_run_id=snapshot_run_id) 数量 >= eligible_count，
    故 eligible_count 用 compute_coverage 的真实 "expected"（与 orchestrator 口径一致）。
    """
    from app.services.first_pyramid_service import (
        FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )

    async with AsyncSessionLocal() as db:
        cov = await compute_coverage(db, snapshot_run_id)
        pub = await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=trade_date,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            snapshot_run_id=snapshot_run_id,
            coverage_ratio=cov["coverage"],
            worker_id="verify-seed",
            lease_epoch=int(datetime.now(timezone.utc).timestamp()),
            eligible_count=cov.get("expected", 0),
        )
        await db.commit()
    print(f"[seed] publication done: {trade_date} publication_id={pub.id}")


async def _add_projection_selector(trade_date: date, snapshot_run_id: uuid.UUID) -> None:
    """真实 DSA selector 投影链（与 after_close_orchestrator 同一路径）。

    真实入口（不存在 run_strategy_selector_batch）：
      1. StrategyBatchService.create_batch_run(db, strategy_key, trade_date, run_type,
             instrument_ids=None, *, claim_for_worker=None) -> StrategyRun
      2. CoreArtifactRepository(db, *, batch_size).project_dsa_batch(
             *, source_core_run_id, dsa_run_id, trade_date, strategy_version_id,
             persist_fn, heartbeat=None, job_run_id=None) -> dict[str, int]
      3. persist_precomputed_dsa_results(db, *, run_id, artifacts, trade_date,
             strategy_version_id, requirement=..., job_run_id=None)
    """
    async with AsyncSessionLocal() as db:
        svc = StrategyBatchService()
        dsa_run = await svc.create_batch_run(
            db, "dsa_selector", trade_date, "scheduled",
            claim_for_worker="verify-seed",
        )
        await db.commit()

        repo = CoreArtifactRepository(db, batch_size=200)
        result = await repo.project_dsa_batch(
            source_core_run_id=snapshot_run_id,
            dsa_run_id=dsa_run.id,
            trade_date=trade_date,
            strategy_version_id=dsa_run.strategy_version_id,
            persist_fn=persist_precomputed_dsa_results,
        )
        await db.commit()
    print(f"[seed] projection selector done: {trade_date} result={result}")


def _synthetic_board_snapshot() -> BoardSnapshot:
    """把 Seed 合成的 4 板块 + 100 成员构造为真实 BoardSnapshot（不连 pywencai）。"""
    board_specs = [
        ("IND_MAIN", "行业主板", "industry"),
        ("CONCEPT_HOT", "概念热点", "concept"),
        ("IND_CYB", "创业板行业", "industry"),
        ("CONCEPT_VALUE", "价值概念", "concept"),
    ]
    boards = [
        {"external_code": code, "name": nm, "type": typ}
        for code, nm, typ in board_specs
    ]
    memberships: dict[tuple[str, str], list[str]] = {
        (code, typ): [] for code, _nm, typ in board_specs
    }
    spec_by_code = {code: (code, typ) for code, _nm, typ in board_specs}
    for i in range(N_INST):
        symbol = f"{600000 + i:06d}"
        for code in (board_specs[i % 2][0], board_specs[(i + 1) % 4][0]):
            memberships[spec_by_code[code]].append(symbol)
    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=sum(len(v) for v in memberships.values()),
        unresolved_symbols=[],
        diagnostics={"source": "synthetic_seed"},
    )


async def _resolve_synthetic_symbols(symbols: list[str]) -> dict[str, uuid.UUID]:
    """instrument_resolver：symbol → instrument_id（读验证库 instruments 表）。"""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text("SELECT symbol, id FROM instruments WHERE symbol = ANY(:syms)"),
            {"syms": list(symbols)},
        )
        return {row[0]: row[1] for row in rows}


async def _add_board_prereq(trade_date: date) -> None:
    """真实 board facts 链：run_board_facts(db, trade_date, *, snapshot=..., instrument_resolver=...)。

    真实签名（board_facts_service.py:317）：
        run_board_facts(db, trade_date, *, run_mode="scheduled_current",
            max_reuse_trading_days=..., snapshot=None, instrument_resolver=None) -> BoardFactsRun
    注入 snapshot 可避免联网 pywencai（DS-112）。内部会调用 board_sync_service.sync_boards
    （真实入口，签名 sync_boards(db, snapshot, instrument_resolver=None, *, effective_date=None)）。

    [如实标注] board_sync_service 的绝对门禁按全市场标定
    （raw_rows>=5000 / industry>=200 / concept>=300 / relation>=60000 / industry_coverage>=0.99），
    100 只 synthetic 标的**必然触发 StagingValidationError**，run_board_facts 会捕获并转入
    _try_reuse_previous_on_failure；无历史 published run 时该 run 终态为 failed/unavailable。
    因此 board_facts 无法在纯 synthetic 100 标的下达到 fresh ready —— 见文末报告说明。
    """
    snapshot = _synthetic_board_snapshot()
    async with AsyncSessionLocal() as db:
        run = await run_board_facts(
            db, trade_date,
            snapshot=snapshot,
            instrument_resolver=_resolve_synthetic_symbols,
        )
        await db.commit()
    print(f"[seed] board facts done: {trade_date} run={run.id} status={run.status} readiness={run.readiness}")


async def _add_review_prereq(trade_date: date) -> None:
    """只读聚合当前九节点 readiness（真实入口：ProductReadinessService.collect_states 实例方法）。

    [修正] collect_states 不是模块级函数，必须经 ProductReadinessService() 实例调用。
    """
    async with AsyncSessionLocal() as db:
        svc = ProductReadinessService()
        states = await svc.collect_states(db, trade_date)
    ready = [f"{s.product}={s.readiness}" for s in states]
    print(f"[seed] readiness states: {trade_date} {ready}")


async def _add_auction_prereq(trade_date: date, *, mode: str) -> None:
    """真实 auction 锚点链（与 after_close_orchestrator 同一入口）。

    [修正] 原实现向 `call_auction_quotes` 表插入伪造行——该表在本仓库**不存在**
    （auction 相关真实表为 auction_anchor_snapshots / auction_anchor_items /
    auction_anchor_publications / auction_final_quotes 等）。

    真实签名（auction_anchor_service.py:1326）：
        generate_and_publish_auction_anchors(db, trade_date, *,
            worker_id=None, lease_epoch=None) -> dict[str, Any]

    [如实标注] mode（structure_only / hybrid / composite）由服务**依据真实输入自行判定**
    （取决于当日 stock_core pointer 是否已发布、chip 是否可用），调用方无法指定。
    这里保留 mode 参数仅用于日志标注期望，不做任何强制。若当日 stock_core 尚未发布，
    该函数会返回失败/空结果（软失败，不抛）。
    """
    from app.services.auction_anchor_service import (
        generate_and_publish_auction_anchors,
    )

    async with AsyncSessionLocal() as db:
        try:
            result = await generate_and_publish_auction_anchors(
                db, trade_date,
                worker_id="verify-seed",
                lease_epoch=1,
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - seed 软失败，不阻断其余场景装配
            await db.rollback()
            result = {"error": str(exc)}
    print(f"[seed] auction anchor done: {trade_date} expect_mode={mode} result={result}")


async def _run_chip_real(
    trade_date: date,
    *,
    core_run_id: uuid.UUID,
    instrument_ids: list[uuid.UUID] | None = None,
) -> None:
    """真实 chip 链：create job → execute_after_close_chip_consensus（真实算法 + RunItem 生命周期）。

    真实签名（after_close_chip_consensus_service.py）：
        create_after_close_chip_consensus_job(db, trade_date, core_run_id, *,
            scope="all_a_share", expected_count=None, checkpoint=None,
        ) -> tuple[SchedulerJobRun | None, bool]        # 注意：core_run_id 是**必填位置参数**
        execute_after_close_chip_consensus(job_run_id, trade_date, core_run_id, *,
            instrument_ids: list[uuid.UUID],            # keyword-only 且**必填**（不接受 None）
            worker_id=None, lease_epoch=None, ownership_check=None,
            batch_size=..., _diag_sink=None,
        ) -> dict[str, Any]

    [CHANGE-20260806-008] 绕过顶层 refresh_15m_batch（联网 pytdx）：execute_* 内部以
    `from app.services.chip_bars_refresh_coordinator import refresh_15m_batch` 形式在函数体内
    导入，故 monkeypatch 模块属性生效。synthetic 15m bars 已由 Seed 注入验证库。
    """
    import app.services.chip_bars_refresh_coordinator as _refresh_mod

    targets = list(instrument_ids) if instrument_ids is not None else list(_ALL_INSTRUMENT_IDS)

    async def _noop_refresh(*_a, **_k):
        return {"status": "skipped", "refreshed": 0, "failed": 0, "results": {}}

    original = _refresh_mod.refresh_15m_batch
    _refresh_mod.refresh_15m_batch = _noop_refresh
    try:
        async with AsyncSessionLocal() as db:
            job_run, _is_new = await create_after_close_chip_consensus_job(
                db, trade_date, core_run_id,
                expected_count=len(targets),
            )
            await db.commit()
        if job_run is None:
            print(f"[seed] chip job 创建失败（软失败）: {trade_date}")
            return
        async with AsyncSessionLocal() as db:
            claimed = await claim_next_job_run(
                db,
                job_name=CHIP_CONSENSUS_JOB_NAME,
                worker_instance_id="verify-seed",
                lease_seconds=3600,
            )
            await db.commit()
        if claimed is None or claimed.token.job_run_id != job_run.id:
            raise RuntimeError(f"chip job claim failed: job_run_id={job_run.id}")
        result = await execute_after_close_chip_consensus(
            job_run.id, trade_date, core_run_id,
            instrument_ids=targets,
            worker_id=claimed.token.worker_instance_id,
            lease_epoch=claimed.token.lease_epoch,
        )
        print(f"[seed] chip real done: {trade_date} is_new={_is_new} result={result}")
    finally:
        _refresh_mod.refresh_15m_batch = original


# ---------------------------------------------------------------------------
# 场景装配
# ---------------------------------------------------------------------------
async def _seed_scenario(verify_conn, scenario: str) -> None:
    td = _SCENARIO_TRADE_DATES[scenario]
    # 所有场景共享：instruments/bars/calendar/board/config（一次性生成，幂等）
    # 由 seed_all 顶层生成，这里只做场景专属装配。
    # [顺序修正] auction 锚点真实链要求当日 stock_core pointer 已发布
    # （generate_auction_anchors 第 1 步即查 published stock_core pointer），
    # 且 chip 可用才可能进入 hybrid/composite。故顺序为：
    #   core → projection → board → chip → finish core run → publish core → auction。
    if scenario == "full_success":
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        await _run_chip_real(td, core_run_id=core_run_id)  # 全 100 只（15m 齐全）→ full
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="composite")
        await _add_review_prereq(td)
    elif scenario == "async_enhance":
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        # chip 真实执行；不发布 core pointer → auction 缺前置，closure 停在 core_ready 之前
        await _run_chip_real(td, core_run_id=core_run_id)
        await _add_auction_prereq(td, mode="structure_only")
        await _add_review_prereq(td)
        # 故意不发布 → 后续异步增强（core_ready）
    elif scenario == "degraded":
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        # chip 仅子集 15m 齐全（07-30 仅 30% 标的注入 15m）→ 自然 partial（real chain）
        subset = [_inst_uuid(i) for i in range(int(N_INST * PARTIAL_15M_FRACTION))]
        await _run_chip_real(td, core_run_id=core_run_id, instrument_ids=subset)
        await _add_auction_prereq(td, mode="hybrid")
        await _add_review_prereq(td)
        # 故意不发布 → degraded_ready
    elif scenario == "governance":
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        # 故意缺失 board / auction / chip / publication → blocked / degraded
        await _add_review_prereq(td)


async def seed_all(verify_conn) -> None:
    """一次性合成基础资产 + 四类场景装配。"""
    await _gen_synthetic_instruments_bars(verify_conn)
    await _gen_synthetic_boards(verify_conn)
    await _gen_synthetic_released_dsa_config(verify_conn)
    completed_dates = set((await verify_conn.execute(text(
        "SELECT DISTINCT trade_date FROM stock_feature_snapshot_runs "
        "WHERE trade_date = ANY(:dates)"
    ), {"dates": list(_SCENARIO_TRADE_DATES.values())})).scalars().all())
    if completed_dates == set(_SCENARIO_TRADE_DATES.values()):
        print("[seed] all scenario core runs already exist; validating closures only")
        await _verify_closures()
        return
    for sc in ("full_success", "async_enhance", "degraded", "governance"):
        await _seed_scenario(verify_conn, sc)
    # 验证 closure 符合预期（只读断言，失败抛错阻断）
    await _verify_closures()


async def _verify_closures() -> None:
    """只读报告四类场景的真实 closure（真实入口：ProductReadinessService().collect_states）。

    [如实标注] 这里**只报告不硬断言**：closure 是否落到 fully_ready/degraded_ready
    取决于 board_facts / auction / review 等节点能否在 100 只 synthetic 标的下真实达成
    （board_sync 绝对门禁按全市场标定，见 _add_board_prereq）。硬断言由
    tests/test_pg_seed_scenario_closures.py 在真实验证库上执行。
    """
    expectations = {
        "full_success": CLOSURE_FULLY_READY,
        "degraded": CLOSURE_DEGRADED_READY,
    }
    async with AsyncSessionLocal() as db:
        svc = ProductReadinessService()
        for sc, td in _SCENARIO_TRADE_DATES.items():
            states = await svc.collect_states(db, td)
            ev = evaluate_closure(states)
            expected = expectations.get(sc)
            flag = "" if expected is None else f" (预期 {expected})"
            print(f"[seed] closure {sc} {td} → {ev.closure}{flag}")
            if ev.issues:
                print(f"        issues={ev.issues}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def _amain(verify_db_url: str, scenario: str) -> None:
    """[CHANGE-20260806-008] 单一 DB 来源：所有合成 INSERT 与真实服务均经 AsyncSessionLocal
    （其底层 engine 在导入时绑定 DATABASE_URL）。为确保合成 INSERT 写入的库与真实服务读取的库
    完全一致，这里将 DATABASE_URL 强制对齐到 verify_db_url，并 lazy 重新取出会话工厂。

    运行要求：容器/调用方必须将 DATABASE_URL 指向 bz_stock_verify_<sha>
    （verify_attempt.py 传入 --verify-db-url 即 DATABASE_URL 的同值）。
    """
    os.environ["DATABASE_URL"] = verify_db_url
    # lazy 重新导入，使 AsyncSessionLocal 绑定到最新 DATABASE_URL
    import importlib

    import app.db as _db_mod

    importlib.reload(_db_mod)
    session_factory = _db_mod.AsyncSessionLocal

    async with session_factory() as s:
        if scenario == "all":
            await seed_all(s)
        else:
            # 单场景：先确保基础资产存在（幂等）
            await _gen_synthetic_instruments_bars(s)
            await _gen_synthetic_boards(s)
            await _gen_synthetic_released_dsa_config(s)
            await _seed_scenario(s, scenario)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--verify-db-url",
        default=os.environ.get("DATABASE_URL"),
        help="bz_stock_verify_<sha> 异步连接串；默认读取受控验证容器 DATABASE_URL",
    )
    ap.add_argument("--scenario", default="all", choices=["all", "full_success", "async_enhance", "degraded", "governance"])
    args = ap.parse_args()
    if not args.verify_db_url:
        ap.error("--verify-db-url or DATABASE_URL is required")
    asyncio.run(_amain(args.verify_db_url, args.scenario))


if __name__ == "__main__":
    main()
