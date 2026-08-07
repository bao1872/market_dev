#!/usr/bin/env python3
"""盘迹 V2.1 验证数据 Seed（CHANGE-20260806-008，100% synthetic，DS-112 合规）。

[DS-112] 本 Seed **完全不连接 bz_stock**。所有基础数据（instruments / bars /
trading_calendar / board_membership / released dsa config）均为确定性合成（固定 seed
+ uuid5），确保两次运行幂等、结果可复现。

[CHANGE-20260806-008] 六态 canonical 场景全部通过**真实服务**编排，不伪造 succeeded /
published / coverage / First Pyramid。chip 真实链走 `compute_chip_consensus_snapshot` +
`chip_consensus_run_lifecycle`（真实算法 + RunItem 生命周期），但**绕过
`execute_after_close_chip_consensus` 顶层的 `refresh_15m_batch`（联网 pytdx）**——
synthetic 15m bars 已由 Seed 直接注入验证库，`_fetch_chip_bars(skip_refresh=True)`
已支持只读已有 bars。如实标注：chip 真实链**不含运行级 refresh**。

六态 canonical 场景（closure 断言见 test_pg_seed_scenario_closures.py，与
backend/tests/readiness_fixtures.py 共享唯一事实源）：
  pending_no_core            (2026-07-28) → pending（stock_core 未发布）
  blocked_mandatory_failure  (2026-07-29) → blocked（board_facts 人为 failed，reason=EXTERNAL_GATE_UNSATISFIED）
  core_ready_waiting_mandatory (2026-07-30) → core_ready（stock_core 可消费，review 未发布）
  mandatory_ready_enhancing (2026-07-31) → mandatory_ready_enhancing（mandatory 全 ready，enhancement 未全终态）
  degraded_terminal_partial  (2026-08-01) → degraded_ready（mandatory 全消费，enhancement 终态但部分非 truly ready）
  fully_ready_all_fresh      (2026-08-02) → fully_ready（mandatory 全 fresh + enhancement 全 truly ready + auction composite）

[用户选项B / 约束5+6+7] 单一放大的 full_market universe：220 行业 + 320 概念 board +
≥5,200 instruments + ≥65,000 合法 memberships，使 Board Facts 绝对门禁合法通过，
fully_ready 真实可达；blocked 用人为 failed BoardFactsRun 构造（不依赖规模不足），
禁止跨 universe 无声拼接。

新增资产（仅 synthetic）：220 行业 + 320 概念 MarketBoard + 全部 instruments 的成员关系；
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
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_PENDING,
)
from app.models.factor_publication import (
    PUBLICATION_KIND_BOARD_FACTS,
    PUBLICATION_KIND_STOCK_CORE,
)
from app.models.market_review import MarketReviewRun
from app.services.after_close_chip_consensus_service import (
    CHIP_CONSENSUS_JOB_NAME,
    create_after_close_chip_consensus_job,
    execute_after_close_chip_consensus,
)
from app.services.board_analysis_service import compute_all_boards
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
from sqlalchemy import select, text

# ---------------------------------------------------------------------------
# 确定性合成常量
# ---------------------------------------------------------------------------
_SYNTH_SEED = 20260806
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "panji.verify.synthetic")
N_INST = 100  # 历史兼容；bars 子集构造等仍用 N_INST
# [用户选项B / 约束5+6] full_market universe：≥5,200 唯一可解析 A 股 synthetic instruments
FM_N_INST = 5200
CAL_START = date(2026, 4, 1)
CAL_END = date(2026, 8, 31)
# [性能] full_market universe 下 instruments 放大到 5200，daily/60min bars 仅覆盖
# scenario 窗口（与 15m 同窗口）以控制行数（5200×~13天 可控）。
CORE_BARS_WINDOW_START = date(2026, 7, 20)
# [auction 结构锚点 / 事实对齐] 核心 100 标的（_ALL_INSTRUMENT_IDS）需要 ≥60 根日线，
# 否则 feature_snapshot_service 判定 daily insufficient(<60) → first_pyramid=None →
# 无 SMC structure（BOS/CHoCH/OB/trailing）→ AuctionAnchor coverage_ratio=0。
# 故为这 100 标的追加一段确定性的多 regime 日线历史（2026-04-01 起），
# 让正式 SMC 算法自然产出结构锚点。仅追加 core instruments，控制行数。
FULLY_READY_DAILY_START = date(2026, 4, 1)
# 15m 仅覆盖 scenario 窗口（含 07-28/29/30/31）以控制行数；07-30 仅部分标的注入 15m（natural partial）
FIFTEEN_MIN_WINDOW_START = date(2026, 7, 20)
PARTIAL_15M_DATE = date(2026, 7, 30)
PARTIAL_15M_FRACTION = 0.3  # 仅 30% 标的在 07-30 有 15m → chip 自然 partial

# [审查报告修订 / 六状态事实证明] 六态 canonical 场景，每个有独立输入事实与唯一预期 closure。
# 单一放大的 full_market universe（见 _gen_synthetic_boards）：满足 Board Facts 门禁合法通过，
# 使 fully_ready 真实可达（约束6）；blocked 用人为 failed BoardFactsRun 构造（约束2/7），
# 不依赖规模不足，禁止跨 universe 无声拼接（约束5）。
_SCENARIO_TRADE_DATES = {
    "pending_no_core": date(2026, 7, 28),
    "blocked_mandatory_failure": date(2026, 7, 29),
    "core_ready_waiting_mandatory": date(2026, 7, 30),
    "mandatory_ready_enhancing": date(2026, 7, 31),
    "degraded_terminal_partial": date(2026, 8, 1),
    "fully_ready_all_fresh": date(2026, 8, 2),
}

# 唯一预期 closure（与 backend/tests/readiness_fixtures.py 对齐；审查第七节共享 fixture 只含事实与期望）
_SCENARIO_EXPECTED_CLOSURE = {
    "pending_no_core": CLOSURE_PENDING,
    "blocked_mandatory_failure": CLOSURE_BLOCKED,
    "core_ready_waiting_mandatory": CLOSURE_CORE_READY,
    "mandatory_ready_enhancing": CLOSURE_MANDATORY_READY_ENHANCING,
    "degraded_terminal_partial": CLOSURE_DEGRADED_READY,
    "fully_ready_all_fresh": CLOSURE_FULLY_READY,
}

# [审查第六节] 结构化诊断输出目录（日志仅汇总数/lineage ID/失败节点/reason code/有界样本）
_DIAG_DIR = os.environ.get("READINESS_DIAG_DIR", "/tmp/readiness-diagnostics")


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


def _extended_daily_ohlc(base: float, idx: int, total: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """[auction 结构锚点] 为核心 100 标的生成确定性多 regime 日线 OHLC。

    通过分段明确的上涨/下跌 regime（每段约 20 根切换方向），制造 HH/HL 与 LH/LL 腿，
    使正式 SMC 算法（compute_smc_pine）自然产出 BOS/CHoCH/OB 与 trailing 结构——
    不伪造 structure payload，仅提供确定性的 raw bars 输入。

    idx: 扩展历史内的序号（0..total-1），从 FULLY_READY_DAILY_START 起算。
    末根价格平滑收敛到窗口起点附近（base-5），避免与 scenario 窗口日线产生过大跳空。
    """
    # regime 切换：每段 20 根，方向交替（涨→跌→涨→跌…）
    seg = idx // 20
    pos_in_seg = idx % 20
    direction = 1 if seg % 2 == 0 else -1
    # 段内线性移动，每段幅度约 ±6；整体基线在 base-5 附近平稳过渡
    drift = direction * pos_in_seg * 0.3
    level = base - 5.0 + (seg % 2) * 6.0 + drift
    # 末段（最后 10 根）向 base-5 收敛，消除与窗口起点的跳空
    if total - idx <= 10:
        level = base - 5.0 + (level - (base - 5.0)) * ((total - idx) / 10.0)
    o = Decimal(f"{level:.2f}")
    c = Decimal(f"{level + direction * 0.3:.2f}")
    hi = max(o, c) + Decimal("0.4")
    lo = min(o, c) - Decimal("0.4")
    return o, hi, lo, c


def _extended_60min_ohlc(base: float, idx: int, slot: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """[auction 结构锚点] 扩展日线历史的 60min 杆（4 根/日），沿用当日日线 regime 走势。"""
    # 日内 4 根微调，制造小幅波动，方向与日线 regime 一致
    seg = idx // 20
    direction = 1 if seg % 2 == 0 else -1
    level = base - 5.0 + (seg % 2) * 6.0 + idx * 0.3 + slot * 0.1 * direction
    o = Decimal(f"{level:.2f}")
    c = Decimal(f"{level + 0.1 * direction:.2f}")
    hi = max(o, c) + Decimal("0.3")
    lo = min(o, c) - Decimal("0.3")
    return o, hi, lo, c


# ---------------------------------------------------------------------------
# 合成数据生成（直接写验证库，不依赖 bz_stock）
# ---------------------------------------------------------------------------
# [性能 / 路径2] 分批写入批大小：控制单次 executemany 的内存峰值，避免全量 list 驻留 OOM。
_BATCH = 500


async def _exec_batch(verify_conn, sql: str, rows: list[dict[str, Any]]) -> None:
    """分批执行 INSERT（每批 _BATCH 行），流式清空 rows 列表以释放内存。"""
    for start in range(0, len(rows), _BATCH):
        batch = rows[start:start + _BATCH]
        await verify_conn.execute(text(sql), batch)
    rows.clear()


async def _gen_synthetic_instruments_bars(verify_conn) -> None:
    """[用户选项B / full_market universe] 建 FM_N_INST(≥5200) instruments +
    窗口内 daily/60min + scenario 窗口 15min（确定性）。

    [性能 / 路径2] daily/60min 仅覆盖 CORE_BARS_WINDOW_START(2026-07-20) 起的交易日，
    控制 5200 instruments 下的 bars 行数（5200×约30天 可控）。
    分批写入（每批 _BATCH 行）并流式清空缓冲区，避免 5200×30×N 全量 list 驻留导致 OOM。
    """
    # [用户选项B] instruments 放大到 FM_N_INST（≥5,200 唯一可解析 A 股 synthetic）
    inst_rows: list[dict[str, Any]] = []
    for i in range(FM_N_INST):
        inst_rows.append({
            "id": str(_inst_uuid(i)),
            "symbol": f"{600000 + i:06d}",
            "name": f"验证股{i:04d}",
            "market": "SH",
            "status": "active",
            "listing_date": date(2010, 1, 4),
        })
    await _exec_batch(
        verify_conn,
        "INSERT INTO instruments (id, symbol, name, market, status, listing_date) "
        "VALUES (:id, :symbol, :name, :market, :status, :listing_date) "
        "ON CONFLICT (id) DO NOTHING",
        inst_rows,
    )

    # trading_calendar（market='A', status=OPEN），分批写入
    cal_rows = [
        {"trade_date": d, "is_trading_day": True, "market": "A",
         "source": "MANUAL_OVERRIDE", "status": "OPEN"}
        for d in _TRADING_DAYS
    ]
    await _exec_batch(
        verify_conn,
        "INSERT INTO trading_calendar (trade_date, is_trading_day, market, source, status) "
        "VALUES (:trade_date, :is_trading_day, :market, :source, :status) "
        "ON CONFLICT (trade_date, market) DO NOTHING",
        cal_rows,
    )

    # [性能] daily/60min 仅覆盖窗口内交易日
    window_days = [d for d in _TRADING_DAYS if d >= CORE_BARS_WINDOW_START]
    win_idx = {d: j for j, d in enumerate(window_days)}
    daily_rows: list[dict[str, Any]] = []
    min60_rows: list[dict[str, Any]] = []
    daily_sql = (
        "INSERT INTO bars_daily "
        "(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
        "VALUES (:instrument_id, :trade_date, :open, :high, :low, :close, :volume, "
        ":amount, :adj_factor) ON CONFLICT (instrument_id, trade_date) DO NOTHING"
    )
    min60_sql = (
        "INSERT INTO bars_60min "
        "(instrument_id, trade_time, open, high, low, close, volume, amount, adj_factor) "
        "VALUES (:instrument_id, :trade_time, :open, :high, :low, :close, :volume, "
        ":amount, :adj_factor) "
        "ON CONFLICT (instrument_id, trade_time) DO NOTHING"
    )
    for i in range(FM_N_INST):
        inst_id = _inst_uuid(i)
        base = 10.0 + (i % 50)
        for j, d in enumerate(window_days):
            o = _price(base, i, j)
            volume = Decimal(str(random.randint(10000, 90000)))
            daily_rows.append({
                "instrument_id": str(inst_id), "trade_date": d,
                "open": o, "high": o + Decimal("0.2"), "low": o - Decimal("0.2"),
                "close": o, "volume": volume, "amount": volume * o,
                "adj_factor": Decimal("1.0"),
            })
            # 60min: 4 根（10:30,11:30,14:00,15:00）
            for hhmm in [("10:30"), ("11:30"), ("14:00"), ("15:00")]:
                t = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]))  # noqa: DTZ001
                b = _bar(t, o)
                min60_rows.append({"instrument_id": str(inst_id), "trade_time": t,
                                   **{k2: b[k2] for k2 in (
                                       "open", "high", "low", "close", "volume", "amount", "adj_factor",
                                   )}})
            # 60min 与 daily 行数比为 4:1，每满 4 批 daily 即清一次 60min
            if len(daily_rows) >= _BATCH * 4:
                await _exec_batch(verify_conn, daily_sql, daily_rows)
                await _exec_batch(verify_conn, min60_sql, min60_rows)
    # 收尾：写入剩余 daily / 60min
    await _exec_batch(verify_conn, daily_sql, daily_rows)
    await _exec_batch(verify_conn, min60_sql, min60_rows)

    # bars_15min 仅 scenario 窗口；07-30 仅部分标的（natural partial）
    min15_sql = (
        "INSERT INTO bars_15min "
        "(instrument_id, trade_time, open, high, low, close, volume, amount, adj_factor) "
        "VALUES (:instrument_id, :trade_time, :open, :high, :low, :close, :volume, "
        ":amount, :adj_factor) "
        "ON CONFLICT (instrument_id, trade_time) DO NOTHING"
    )
    min15_rows: list[dict[str, Any]] = []
    n15 = 0
    for i in range(FM_N_INST):
        inst_id = _inst_uuid(i)
        for d in window_days:
            if d < FIFTEEN_MIN_WINDOW_START:
                continue
            if d == PARTIAL_15M_DATE and (i / FM_N_INST) >= PARTIAL_15M_FRACTION:
                continue  # 70% 标的在 07-30 缺 15m → chip 自然 partial
            base = 10.0 + (i % 50)
            # 16 根收盘时间：09:45..11:30 + 13:15..15:00，末根 15:00。
            for slot in range(16):
                session_start = datetime(  # noqa: DTZ001
                    d.year, d.month, d.day, 9 if slot < 8 else 13, 30 if slot < 8 else 0,
                )
                t = session_start + timedelta(minutes=15 * ((slot % 8) + 1))
                o = _price(base, i, win_idx[d] * 16 + slot)
                b = _bar(t, o)
                min15_rows.append({"instrument_id": str(inst_id), "trade_time": t,
                                   **{k2: b[k2] for k2 in (
                                       "open", "high", "low", "close", "volume", "amount", "adj_factor",
                                   )}})
                n15 += 1
                if len(min15_rows) >= _BATCH:
                    await _exec_batch(verify_conn, min15_sql, min15_rows)
    await _exec_batch(verify_conn, min15_sql, min15_rows)
    await verify_conn.commit()

    # [auction 结构锚点] 为核心 100 标的（_ALL_INSTRUMENT_IDS）追加确定性多 regime 日线 +
    # 60min 历史，从 FULLY_READY_DAILY_START 到 CORE_BARS_WINDOW_START 前一日。
    # 目的：让正式 SMC 算法获得 ≥60 根日线，自然产出 BOS/CHoCH/OB/trailing 结构锚点，
    # 使 AuctionAnchor coverage_ratio>0（禁止直接写 structure payload / AuctionAnchorItem）。
    ext_days = [d for d in _TRADING_DAYS
                if FULLY_READY_DAILY_START <= d < CORE_BARS_WINDOW_START]
    ext_daily_rows: list[dict[str, Any]] = []
    ext_min60_rows: list[dict[str, Any]] = []
    for i in range(N_INST):  # 仅核心 100 标的
        inst_id = _inst_uuid(i)
        base = 10.0 + (i % 50)
        for idx, d in enumerate(ext_days):
            o, hi, lo, c = _extended_daily_ohlc(base, idx, len(ext_days))
            volume = Decimal(str(random.randint(10000, 90000)))
            ext_daily_rows.append({
                "instrument_id": str(inst_id), "trade_date": d,
                "open": o, "high": hi, "low": lo, "close": c,
                "volume": volume, "amount": volume * c, "adj_factor": Decimal("1.0"),
            })
            for slot, hhmm in enumerate([("10:30"), ("11:30"), ("14:00"), ("15:00")]):
                t = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]))  # noqa: DTZ001
                eo, ehi, elo, ec = _extended_60min_ohlc(base, idx, slot)
                evolume = Decimal(str(random.randint(10000, 90000)))
                ext_min60_rows.append({
                    "instrument_id": str(inst_id), "trade_time": t,
                    "open": eo, "high": ehi, "low": elo, "close": ec,
                    "volume": evolume, "amount": evolume * ec, "adj_factor": Decimal("1.0"),
                })
            if len(ext_daily_rows) >= _BATCH:
                await _exec_batch(verify_conn, daily_sql, ext_daily_rows)
                await _exec_batch(verify_conn, min60_sql, ext_min60_rows)
    await _exec_batch(verify_conn, daily_sql, ext_daily_rows)
    await _exec_batch(verify_conn, min60_sql, ext_min60_rows)
    await verify_conn.commit()

    # 注意：daily/60min/15min 实际行数为分批写入累加值；此处仅打印 instruments 数与 15min 估算。
    print(f"[seed] instruments={FM_N_INST} window_days={len(window_days)} "
          f"15min_est={n15} ext_daily_days={len(ext_days)} ext_core_inst={N_INST}")


# [用户选项B / 约束6] full_market universe board 规模：220 行业 + 320 概念 = 540 boards
_FM_N_INDUSTRY = 220
_FM_N_CONCEPT = 320
_FM_BOARDS_PER_INST = 13  # 5200×13 = 67,600 ≥ 65,000 合法关系


async def _gen_synthetic_boards(verify_conn) -> None:
    """[用户选项B / full_market universe] 建 220 行业 + 320 概念 MarketBoard +
    全部 FM_N_INST instruments 的成员关系（每只确定性加入 13 个 board → ≥65,000 关系）。

    [约束4] 不修改任何 Board 门禁阈值、不 mock validate_snapshot、不直接写 readiness。
    Board Facts 门禁（raw_rows≥5000/industry≥200/concept≥300/relation≥60000/coverage≥0.99）
    由真实 sync_boards 基于本函数生成的合法 synthetic 统计真实通过。
    """
    board_specs: list[tuple[str, str, str]] = []
    for k in range(_FM_N_INDUSTRY):
        board_specs.append((f"IND_{k:03d}", f"行业{k:03d}", "industry"))
    for k in range(_FM_N_CONCEPT):
        board_specs.append((f"CON_{k:03d}", f"概念{k:03d}", "concept"))
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
    # 确定性成员关系：每只 instrument 加入 13 个 board（混合行业/概念，确定性散布）
    # [性能 / 路径2] 分批写入（每批 _BATCH 行），流式清空 members 缓冲避免 67,600 行全量驻留 OOM。
    members: list[dict[str, str]] = []
    n_boards = len(board_specs)
    n_members = 0
    for i in range(FM_N_INST):
        inst_id = _inst_uuid(i)
        for b in range(_FM_BOARDS_PER_INST):
            code = board_specs[(i * 7 + b * 13) % n_boards][0]
            members.append({"board_id": str(board_ids[code]), "instrument_id": str(inst_id)})
            n_members += 1
            if len(members) >= _BATCH:
                await _exec_batch(
                    verify_conn,
                    "INSERT INTO market_board_memberships (board_id, instrument_id) "
                    "VALUES (:board_id, :instrument_id) "
                    "ON CONFLICT (board_id, instrument_id) DO NOTHING",
                    members,
                )
    await _exec_batch(
        verify_conn,
        "INSERT INTO market_board_memberships (board_id, instrument_id) "
        "VALUES (:board_id, :instrument_id) "
        "ON CONFLICT (board_id, instrument_id) DO NOTHING",
        members,
    )
    await verify_conn.commit()
    print(f"[seed] boards={n_boards} memberships={n_members}")


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


async def _synthetic_board_snapshot() -> BoardSnapshot:
    """[用户选项B / full_market universe] 动态读验证库 market_boards + memberships +
    instruments，构造真实 BoardSnapshot（不连 pywencai）。

    [约束4] 不 mock validate_snapshot；snapshot 直接来自已写入的合法 synthetic 事实，
    使 sync_boards 真实计算 raw_rows/industry/concept/relation/coverage 并合法通过门禁。
    """
    boards: list[dict[str, str]] = []
    memberships: dict[tuple[str, str], list[str]] = {}
    seen_boards: set[tuple[str, str]] = set()
    raw_rows = 0
    # [性能 / 路径2] 流式迭代（yield_per）而非 rows.all() 全量驻留：67,600 条 membership
    # 一次性 materialize 会占用大量内存。逐行消费并累积到 memberships 字典。
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT b.external_code, b.name, b.type, m.instrument_id, i.symbol "
                "FROM market_board_memberships m "
                "JOIN market_boards b ON b.id = m.board_id "
                "JOIN instruments i ON i.id = m.instrument_id"
            ).execution_options(yield_per=500)
        )
        for ext_code, nm, typ, _inst_id, symbol in result:
            key = (ext_code, typ)
            if key not in seen_boards:
                seen_boards.add(key)
                boards.append({"external_code": ext_code, "name": nm, "type": typ})
            memberships.setdefault(key, []).append(symbol)
            raw_rows += 1
    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=raw_rows,
        unresolved_symbols=[],
        diagnostics={"source": "synthetic_seed_full_market"},
    )


async def _resolve_synthetic_symbols(symbols: list[str]) -> dict[str, uuid.UUID]:
    """instrument_resolver：symbol → instrument_id（读验证库 instruments 表）。"""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text("SELECT symbol, id FROM instruments WHERE symbol = ANY(:syms)"),
            {"syms": list(symbols)},
        )
        return {row[0]: row[1] for row in rows}


async def _add_board_prereq(trade_date: date) -> uuid.UUID:
    """真实 board facts 链：run_board_facts(db, trade_date, *, snapshot=..., instrument_resolver=...)。

    真实签名（board_facts_service.py:317）：
        run_board_facts(db, trade_date, *, run_mode="scheduled_current",
            max_reuse_trading_days=..., snapshot=None, instrument_resolver=None) -> BoardFactsRun
    注入 snapshot 可避免联网 pywencai（DS-112）。内部会调用 board_sync_service.sync_boards
    （真实入口，签名 sync_boards(db, snapshot, instrument_resolver=None, *, effective_date=None)）。

    [用户选项B / 约束3+4] board_sync_service 的绝对门禁按全市场标定
    （raw_rows>=5000 / industry>=200 / concept>=300 / relation>=60000 / industry_coverage>=0.99）。
    full_market universe（FM_N_INST=5200 标的 / 220 行业 / 320 概念 / >=65,000 关系）由
    _gen_synthetic_boards 合法生成，门禁由 sync_boards **真实计算并通过**——不改阈值、不 mock
    validate_snapshot、不直接写 board_facts readiness。

    Returns:
        BoardFactsRun.id（Review 的 source_board_run_id lineage 输入，约束5）
    """
    snapshot = await _synthetic_board_snapshot()
    async with AsyncSessionLocal() as db:
        run = await run_board_facts(
            db, trade_date,
            snapshot=snapshot,
            instrument_resolver=_resolve_synthetic_symbols,
        )
        await db.commit()
        run_id = run.id
        status, readiness = run.status, run.readiness
    print(f"[seed] board facts done: {trade_date} run={run_id} status={status} readiness={readiness}")
    return run_id


async def _seed_blocked_board_failure(trade_date: date) -> uuid.UUID:
    """[审查第二节 / 约束2+7] blocked 场景的 mandatory terminal failure 构造。

    不新增第七种 closure：这里让 board_facts 节点成为**终态失败且无 publication**，
    使 ProductReadinessService._board_facts_state 判定
    readiness=unavailable / is_terminal=True → evaluate_closure → blocked。

    [如实标注 1] BoardFactsRun 表**没有 reason_code 列**（仅 BoardFactsRunItem 有）；
    run 级别的原因载体是 error_code / error_message / gate_results_json，这里如实写入
    error_code=EXTERNAL_GATE_UNSATISFIED 作为 DB 侧事实。

    [如实标注 2] ProductReadinessState.lineage["reason_code"] 由 service 自行生成为
    "RUN_FAILED"（product_readiness_service.py:937），**不读取** BoardFactsRun.error_code。
    Seed 不修改 service 判定算法，因此断言侧的 reason_code 期望值是 RUN_FAILED；
    EXTERNAL_GATE_UNSATISFIED 仅存在于 run 表事实层。

    [如实标注 3] _board_facts_state 优先看 BOARD_FACTS publication；只要该日存在
    publication 就判 ready。因此本函数必须**先删除**该日 BOARD_FACTS publication，
    否则 failed run 不会生效。

    [约束7] 不依赖"100 只标的规模不足"这一偶发原因；显式写入一个 failed BoardFactsRun，
    使 blocked 语义稳定、与 universe 规模解耦。full_market universe 本身能合法通过门禁，
    所以此处必须显式构造失败，否则该场景会退化成 fully_ready。
    """
    from app.models.board_facts_run import BoardFactsRun

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # [如实标注 3] 清除该日 BOARD_FACTS publication，使 failed run 成为唯一事实来源。
        # 仅限本场景的 synthetic 验证日期（幂等：重复执行结果相同）。
        removed = len((await db.execute(text(
            "DELETE FROM factor_publications "
            "WHERE publication_kind = :kind AND trade_date = :td "
            "RETURNING id"
        ), {"kind": PUBLICATION_KIND_BOARD_FACTS, "td": trade_date})).all())
        existing = (await db.execute(
            select(BoardFactsRun)
            .where(BoardFactsRun.trade_date == trade_date)
            .order_by(BoardFactsRun.created_at.desc())
        )).scalars().first()
        if existing is not None:
            existing.status = "failed"
            existing.readiness = "unavailable"
            existing.error_code = "EXTERNAL_GATE_UNSATISFIED"
            existing.error_message = (
                "synthetic mandatory terminal failure for blocked_mandatory_failure scenario"
            )
            existing.finished_at = existing.finished_at or now
            run_id = existing.id
        else:
            run = BoardFactsRun(
                id=uuid.uuid5(_NS, f"board-facts-blocked-{trade_date}"),
                trade_date=trade_date,
                run_mode="scheduled_current",
                source="pywencai",
                status="failed",
                readiness="unavailable",
                error_code="EXTERNAL_GATE_UNSATISFIED",
                error_message=(
                    "synthetic mandatory terminal failure for blocked_mandatory_failure scenario"
                ),
                started_at=now,
                finished_at=now,
            )
            db.add(run)
            await db.flush()
            run_id = run.id
        await db.commit()
    print(
        f"[seed] board facts BLOCKED (synthetic terminal failure): {trade_date} "
        f"run={run_id} error_code=EXTERNAL_GATE_UNSATISFIED "
        f"removed_publications={removed} (state reason_code 由 service 生成为 RUN_FAILED)"
    )
    return run_id


async def _publish_board_aggregation(trade_date: date) -> uuid.UUID | None:
    """真实 board aggregation 链：compute_all_boards(publish=True) → market_aggregation pointer。

    真实签名（board_analysis_service.py:1649）：
        compute_all_boards(session, trade_date, *, board_type=None, limit=None,
            publish=True, algorithm_version=BOARD_ANALYSIS_ALGORITHM_VERSION) -> dict

    [审查第四节修正] 原 Seed 只做单板块 compute 而**从不写 market_aggregation publication**，
    导致 _board_aggregation_state 永远 unavailable。compute_all_boards 内部在
    batch_run.status == "succeeded" 时才逐板块 publish_board_analysis 并调用
    publish_market_aggregation（写 market_aggregation pointer），是唯一真实 producer。

    [约束4+5] 不直接写 factor_publications；source_core_run_id 由 compute_all_boards 从
    已发布 stock_core pointer 读取，与 Core 同 universe，lineage 天然一致。

    Returns:
        board_analysis_runs.id（成功发布时），否则 None（软失败，由 closure 断言暴露）
    """
    async with AsyncSessionLocal() as db:
        result = await compute_all_boards(db, trade_date, publish=True)
        await db.commit()
    published = result.get("published", 0)
    status = result.get("status")
    run_id = result.get("board_analysis_run_id")
    print(
        f"[seed] board aggregation: {trade_date} run={run_id} status={status} "
        f"succeeded={result.get('succeeded')} failed={result.get('failed')} "
        f"published_boards={published} coverage_below={result.get('coverage_below_threshold')}"
    )
    if result.get("errors"):
        print(f"        errors[:3]={result['errors'][:3]}")
    if status != "succeeded":
        return None
    return uuid.UUID(str(run_id))


async def _run_and_publish_review(
    trade_date: date,
    core_run_id: uuid.UUID,
    board_run_id: uuid.UUID | None = None,
) -> None:
    """真实 review producer 链：create_run → compute_run → publish_run(force=False)。

    真实签名（review_orchestrator_service.py）：
        create_run(session, *, trade_date, source_core_run_id=None,
            source_board_run_id=None, algorithm_version=None, ...) -> MarketReviewRun
        compute_run(session, run, *, canary=False, symbols=None) -> dict
        publish_run(session, run, *, force=False, operator=None,
            idempotency_key=None) -> tuple[FactorPublication | None, list[str]]

    [审查第四节修正] 原 Seed 只把 Review 当 observer（collect_states 只读），
    从不产生 market_review publication，导致 mandatory 永远缺一节点。
    这里让 Review 成为真正 producer。

    [约束4] force=False：必须真实通过 evaluate_publish_gate（coverage 门禁），
    不用 provisional 绕过。门禁失败抛 ReviewPublishBlockError → 这里捕获并如实打印，
    由 closure 严格断言暴露，不静默兜底。

    [约束5] source_core_run_id / source_board_run_id 显式传入，记录同 universe lineage。
    """
    from app.services.review_orchestrator_service import (
        ReviewOrchestratorError,
        compute_run,
        create_run,
        publish_run,
    )

    async with AsyncSessionLocal() as db:
        try:
            run = await create_run(
                db,
                trade_date=trade_date,
                source_core_run_id=core_run_id,
                source_board_run_id=board_run_id,
                idempotency_key=f"verify-seed:{trade_date}",
            )
            await db.commit()
            run_id = run.id
        except ReviewOrchestratorError as exc:
            # board 无已发布 pointer（如 blocked 场景）时 create_run 无法解析 board，
            # 这是预期软失败：review 不发布，由 closure 严格断言暴露。不静默兜底为成功。
            print(f"[seed] review create_run 软失败（无 board pointer）: {trade_date} {exc}")
            return

    async with AsyncSessionLocal() as db:
        compute_target = await db.get(MarketReviewRun, run_id)
        if compute_target is None:
            raise RuntimeError(f"review run 不存在: {run_id}")
        summary = await compute_run(db, compute_target)
        await db.commit()
    print(f"[seed] review computed: {trade_date} run={run_id} summary={summary}")

    async with AsyncSessionLocal() as db:
        publish_target = await db.get(MarketReviewRun, run_id)
        if publish_target is None:
            raise RuntimeError(f"review run 不存在: {run_id}")
        try:
            pub, blockers = await publish_run(
                db, publish_target, force=False, operator="verify-seed",
                idempotency_key=f"verify-seed:{trade_date}",
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - 如实暴露门禁失败，不静默兜底
            await db.rollback()
            print(
                f"[seed] review publish BLOCKED: {trade_date} run={run_id} "
                f"error={type(exc).__name__}: {exc}"
            )
            return
    print(
        f"[seed] review published: {trade_date} run={run_id} "
        f"publication={None if pub is None else pub.id} blockers={blockers}"
    )


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

    # [auction 结构锚点 / 事实对齐] 结构化诊断：确认 BOS/CHoCH/OB/trailing 与
    # POC/VAH/VAL 及 active-anchor 缺失原因。仅读取，不改变行为与发布结果。
    await _diagnose_auction_anchors(trade_date)

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


async def _diagnose_auction_anchors(trade_date: date) -> None:
    """[auction 结构锚点 / 事实对齐] 结构化诊断 active-anchor 缺失来源。

    仅读核查（不影响发布）：
      1) StockFeatureSnapshot.first_pyramid.structure：SMC BOS/CHoCH/OB/trailing 是否存在；
      2) StockChipConsensusSnapshot.chip_payload.chip.continuousFactors：POC/VAH/VAL 是否存在；
      3) 当日已发布 stock_core pointer 是否存在（auction 正式入口的前置条件）。
    输出有界 JSON（汇总数 + 失败节点 + 有界样本，不泄露 UUID）。
    """
    from app.models.chip_consensus import StockChipConsensusSnapshot
    from app.models.factor_publication import FactorPublication
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.services.auction_anchor_service import (
        PUBLICATION_KIND_STOCK_CORE,
        SCOPE_TYPE_MARKET,
    )

    diag: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "structure_source": {},
        "chip_source": {},
        "stock_core_pointer": {},
    }
    try:
        async with AsyncSessionLocal() as db:
            # 3) 已发布 stock_core pointer
            core_pub = (await db.execute(
                select(FactorPublication.data_run_id)
                .where(
                    FactorPublication.scope_type == SCOPE_TYPE_MARKET,
                    FactorPublication.scope_key == "market",
                    FactorPublication.trade_date == trade_date,
                    FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
                )
                .limit(1)
            )).scalar_one_or_none()
            diag["stock_core_pointer"] = {
                "published": core_pub is not None,
            }

            # 1) SMC structure 来源
            snaps = (await db.execute(
                select(StockFeatureSnapshot.summary_payload)
                .where(StockFeatureSnapshot.trade_date == trade_date)
            )).all()
            n_struct = 0
            struct_sample: list[dict[str, Any]] = []
            for (payload,) in snaps:
                fp = (payload or {}).get("first_pyramid") or {}
                struct = fp.get("structure") or {}
                has = any(struct.get(k) for k in
                          ("bos", "choch", "order_blocks", "trailing_top", "trailing_bottom"))
                if has:
                    n_struct += 1
                if len(struct_sample) < 5:
                    struct_sample.append({
                        "has_bos": bool(struct.get("bos")),
                        "has_choch": bool(struct.get("choch")),
                        "has_ob": bool(struct.get("order_blocks")),
                        "has_trailing_top": struct.get("trailing_top") is not None,
                        "has_trailing_bottom": struct.get("trailing_bottom") is not None,
                    })
            diag["structure_source"] = {
                "snapshot_count": len(snaps),
                "with_structure_count": n_struct,
                "sample": struct_sample,
            }

            # 2) Chip continuousFactors 来源
            chips = (await db.execute(
                select(StockChipConsensusSnapshot.chip_payload)
                .where(StockChipConsensusSnapshot.trade_date == trade_date)
            )).all()
            n_chip = 0
            chip_sample: list[dict[str, Any]] = []
            for (payload,) in chips:
                cf = (((payload or {}).get("chip") or {})
                      .get("continuousFactors") or {})
                has = all(cf.get(k) is not None for k in ("poc", "vah", "val"))
                if has:
                    n_chip += 1
                if len(chip_sample) < 5:
                    chip_sample.append({
                        "has_poc": cf.get("poc") is not None,
                        "has_vah": cf.get("vah") is not None,
                        "has_val": cf.get("val") is not None,
                    })
            diag["chip_source"] = {
                "snapshot_count": len(chips),
                "with_continuous_factors_count": n_chip,
                "sample": chip_sample,
            }
    except Exception as exc:  # noqa: BLE001 - 诊断失败不得影响主流程
        diag["diagnostic_error"] = f"{type(exc).__name__}: {exc}"

    # 用既有同步诊断写入器（与 _write_diag 一致，避免 async open lint）
    out_path = _write_diag(f"auction-anchor-diagnostic-{trade_date.isoformat()}.json", diag)
    return out_path


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
# 场景装配（六态 canonical；审查报告修正四个错位）
# ---------------------------------------------------------------------------
async def _seed_scenario(verify_conn, scenario: str) -> None:
    td = _SCENARIO_TRADE_DATES[scenario]
    # 所有场景共享：instruments/bars/calendar/board/config（一次性生成，幂等）
    # 由 seed_all 顶层生成，这里只做场景专属装配。
    # producer 顺序（真实服务依赖方向）：
    #   core → projection → board(publication) → chip → finish core run → publish core
    #   → auction → board_aggregation(publication) → review(create→compute→publish)
    # 约束5：同一场景内部 Core/Board/Aggregation/Review 必须使用同一 universe，禁止跨 universe 拼接。
    if scenario == "pending_no_core":
        # 只建 daily + instruments，stock_core 未发布 → closure=pending
        await _add_instruments_prereq(td)
    elif scenario == "blocked_mandatory_failure":
        # mandatory 主链基本完成，但 board_facts 显式终态失败且无 publication
        # → board_facts unavailable(terminal) → closure=blocked
        # （run 表 error_code=EXTERNAL_GATE_UNSATISFIED；state.reason_code 由 service 生成为 RUN_FAILED）
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _seed_blocked_board_failure(td)
        await _run_chip_real(td, core_run_id=core_run_id)
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="composite")
        # board aggregation 失败（无 cik/name → 不发布 market_aggregation pointer）
        agg_run_id = await _publish_board_aggregation(td)
        # blocked 场景 board 无指针 → review 仍尝试发布但会因无 board pointer 软失败（closure=blocked）
        await _run_and_publish_review(td, core_run_id, agg_run_id)
    elif scenario == "core_ready_waiting_mandatory":
        # stock_core 可消费，但 review 未发布（mandatory 未完成）→ closure=core_ready
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        await _run_chip_real(td, core_run_id=core_run_id)
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="composite")
        await _publish_board_aggregation(td)
        # 故意不发布 review → core_ready
    elif scenario == "mandatory_ready_enhancing":
        # mandatory 全部 ready（含已发布 stock_core），enhancement 部分未终态
        # → closure=mandatory_ready_enhancing
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        await _run_chip_real(td, core_run_id=core_run_id)
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="composite")
        agg_run_id = await _publish_board_aggregation(td)
        await _run_and_publish_review(td, core_run_id, agg_run_id)
        # enhancement（chip/auction）已完成；dsa_projection/state_events 留待异步增强未终态
    elif scenario == "degraded_terminal_partial":
        # mandatory 全部可消费；enhancement 全部终态但部分非 truly ready
        # （chip partial + auction hybrid）→ closure=degraded_ready
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)
        subset = [_inst_uuid(i) for i in range(int(N_INST * PARTIAL_15M_FRACTION))]
        await _run_chip_real(td, core_run_id=core_run_id, instrument_ids=subset)  # 部分 15m → partial
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="hybrid")  # 非 composite → 非 fully ready
        agg_run_id = await _publish_board_aggregation(td)
        await _run_and_publish_review(td, core_run_id, agg_run_id)
    elif scenario == "fully_ready_all_fresh":
        # mandatory 全 fresh + enhancement 全 truly ready + auction composite
        # → closure=fully_ready（约束6：必须真实达到）
        core_run_id = await _add_instruments_prereq(td)
        await _add_projection_selector(td, core_run_id)
        await _add_board_prereq(td)  # full_market universe 下门禁合法通过
        await _run_chip_real(td, core_run_id=core_run_id)
        await _finish_core_run(td, core_run_id)
        await _add_full_publication(td, core_run_id)
        await _add_auction_prereq(td, mode="composite")
        agg_run_id = await _publish_board_aggregation(td)
        await _run_and_publish_review(td, core_run_id, agg_run_id)
    else:
        raise ValueError(f"未知场景: {scenario}（合法值：{sorted(_SCENARIO_TRADE_DATES)}）")


# ---------------------------------------------------------------------------
# 结构化诊断 + 事实向量幂等（审查第五、六节）
# ---------------------------------------------------------------------------
def _write_diag(name: str, payload: Any) -> str:
    """写结构化诊断 JSON（审查第六节：固定文件名，非自由日志）。"""
    os.makedirs(_DIAG_DIR, exist_ok=True)
    path = os.path.join(_DIAG_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(f"[seed] diagnostics written: {path}")
    return path


def _state_row(st) -> dict[str, Any]:
    """单节点事实行（含 lineage 关系，不含随机 UUID 之外的噪声）。"""
    return {
        "product": st.product,
        "readiness": st.readiness,
        "freshness": st.freshness,
        "isMandatory": st.is_mandatory,
        "isTerminal": st.is_terminal,
        "isConsumable": st.is_consumable,
        "isFullyFresh": st.is_fully_fresh,
        "isProductReady": st.is_product_ready,
        "auctionMode": st.auction_mode,
        "reasonCode": st.lineage.get("reason_code"),
        "lineage": {k: (str(v) if v is not None else None) for k, v in sorted(st.lineage.items())},
    }


async def _collect_scenario_matrix() -> dict[str, Any]:
    """采集六场景九节点完整矩阵（真实入口：ProductReadinessService().collect_states）。"""
    matrix: dict[str, Any] = {}
    async with AsyncSessionLocal() as db:
        svc = ProductReadinessService()
        for sc, td in _SCENARIO_TRADE_DATES.items():
            states = await svc.collect_states(db, td)
            ev = evaluate_closure(states)
            matrix[sc] = {
                "tradeDate": str(td),
                "expectedClosure": _SCENARIO_EXPECTED_CLOSURE[sc],
                "actualClosure": ev.closure,
                "match": ev.closure == _SCENARIO_EXPECTED_CLOSURE[sc],
                "mandatoryProductsReady": ev.mandatory_products_ready,
                "mandatoryProductsFullFresh": ev.mandatory_products_full_fresh,
                "enhancementJobsTerminal": ev.enhancement_jobs_terminal,
                "issues": ev.issues,
                "nodes": {st.product: _state_row(st) for st in states},
            }
    return matrix


def _closure_vector(matrix: dict[str, Any]) -> dict[str, str]:
    return {sc: str(v["actualClosure"]) for sc, v in sorted(matrix.items())}


# 审计时间字段：幂等 diff 中允许变化（审查第五节）
_IDEMPOTENCY_IGNORED_KEYS = frozenset({
    "created_at", "updated_at", "started_at", "finished_at",
    "published_at", "computed_at", "generated_at", "observed_at",
})


async def _collect_fact_vector() -> dict[str, Any]:
    """采集完整事实向量（审查第五节：run/item/snapshot/publication/pointer/closure）。

    仅采集计数与关系型指纹，不含审计时间戳；两次 Seed 之间应逐字段相等。
    """
    counts_sql = {
        "core_runs": "SELECT COUNT(*) FROM stock_feature_snapshot_runs",
        "core_run_items": "SELECT COUNT(*) FROM stock_feature_snapshot_run_items",
        "core_snapshots": "SELECT COUNT(*) FROM stock_feature_snapshots",
        "publications": "SELECT COUNT(*) FROM factor_publications",
        "publications_current": (
            "SELECT COUNT(*) FROM factor_publications WHERE superseded_by IS NULL"
        ),
        "board_facts_runs": "SELECT COUNT(*) FROM board_facts_runs",
        "board_analysis_runs": "SELECT COUNT(*) FROM board_analysis_runs",
        "board_analysis_snapshots": "SELECT COUNT(*) FROM board_analysis_snapshots",
        "market_review_runs": "SELECT COUNT(*) FROM market_review_runs",
        "market_boards": "SELECT COUNT(*) FROM market_boards",
        "board_memberships": "SELECT COUNT(*) FROM market_board_memberships",
        "instruments": "SELECT COUNT(*) FROM instruments",
    }
    vector: dict[str, Any] = {"counts": {}, "pointers": {}, "naturalKeys": {}}
    async with AsyncSessionLocal() as db:
        for key, sql in counts_sql.items():
            try:
                vector["counts"][key] = int((await db.execute(text(sql))).scalar_one())
            except Exception as exc:  # noqa: BLE001 - 表缺失如实记录，不静默为 0
                await db.rollback()
                vector["counts"][key] = f"ERROR:{type(exc).__name__}"
        # pointer 向量：当前有效 publication 的 (kind, trade_date) → data_run_id 关系
        try:
            rows = (await db.execute(text(
                "SELECT publication_kind, trade_date::text, data_run_id::text "
                "FROM factor_publications WHERE superseded_by IS NULL "
                "ORDER BY publication_kind, trade_date"
            ))).all()
            vector["pointers"] = {f"{r[0]}@{r[1]}": r[2] for r in rows}
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            vector["pointers"] = {"ERROR": f"{type(exc).__name__}: {exc}"}
        # natural key 向量：每个场景日期的 core run 标识
        try:
            rows = (await db.execute(text(
                "SELECT trade_date::text, COUNT(*), MIN(status), MAX(status) "
                "FROM stock_feature_snapshot_runs GROUP BY trade_date ORDER BY trade_date"
            ))).all()
            vector["naturalKeys"] = {
                r[0]: {"runCount": int(r[1]), "minStatus": r[2], "maxStatus": r[3]}
                for r in rows
            }
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            vector["naturalKeys"] = {"ERROR": f"{type(exc).__name__}: {exc}"}
    return vector


def _diff_fact_vectors(first: dict[str, Any], second: dict[str, Any]) -> list[dict[str, Any]]:
    """结构化 diff（审查第五节：仅审计时间字段允许变化，其余任何差异都是幂等违规）。"""
    diffs: list[dict[str, Any]] = []

    def walk(path: str, a: Any, b: Any) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if k in _IDEMPOTENCY_IGNORED_KEYS:
                    continue
                walk(f"{path}.{k}" if path else str(k), a.get(k), b.get(k))
        elif a != b:
            diffs.append({"path": path, "first": a, "second": b})

    walk("", first, second)
    return diffs


async def seed_all(verify_conn, *, strict: bool = True) -> None:
    """一次性合成基础资产 + 六态 canonical 场景装配 + 严格 closure 断言。"""
    await _gen_synthetic_instruments_bars(verify_conn)
    await _gen_synthetic_boards(verify_conn)
    await _gen_synthetic_released_dsa_config(verify_conn)
    completed_dates = set((await verify_conn.execute(text(
        "SELECT DISTINCT trade_date FROM stock_feature_snapshot_runs "
        "WHERE trade_date = ANY(:dates)"
    ), {"dates": list(_SCENARIO_TRADE_DATES.values())})).scalars().all())
    # pending_no_core 场景不产生 core run，幂等判定只看会产生 core run 的场景
    core_bearing_dates = {
        td for sc, td in _SCENARIO_TRADE_DATES.items() if sc != "pending_no_core"
    }
    if core_bearing_dates.issubset(completed_dates):
        print("[seed] all scenario core runs already exist; validating closures only")
    else:
        for sc in _SCENARIO_TRADE_DATES:
            await _seed_scenario(verify_conn, sc)
    await _verify_closures(strict=strict)


async def _verify_closures(*, strict: bool = True) -> None:
    """六态严格 closure 断言 + 结构化诊断（真实入口：ProductReadinessService().collect_states）。

    [审查第五节修订] 废弃「允许多个 closure」的宽松断言：每个场景有且仅有一个预期 closure。
    strict=True 时任一场景不匹配即抛错阻断（Seed 失败 → 验证尝试失败）。

    输出（审查第六节）：
      - readiness-scenario-matrix.json：六场景 × 九节点完整事实矩阵
      - readiness-lineage.json：每节点 lineage 关系（run/publication/source-core 指向）
      - closure-decision.json：closure 判定输入与结论
    """
    matrix = await _collect_scenario_matrix()

    _write_diag("readiness-scenario-matrix.json", matrix)
    _write_diag("readiness-lineage.json", {
        sc: {p: n["lineage"] for p, n in v["nodes"].items()} for sc, v in matrix.items()
    })
    _write_diag("closure-decision.json", {
        sc: {
            "tradeDate": v["tradeDate"],
            "expectedClosure": v["expectedClosure"],
            "actualClosure": v["actualClosure"],
            "match": v["match"],
            "mandatoryProductsReady": v["mandatoryProductsReady"],
            "mandatoryProductsFullFresh": v["mandatoryProductsFullFresh"],
            "enhancementJobsTerminal": v["enhancementJobsTerminal"],
            "issues": v["issues"],
        }
        for sc, v in matrix.items()
    })

    mismatches: list[str] = []
    for sc, v in matrix.items():
        mark = "OK" if v["match"] else "MISMATCH"
        print(
            f"[seed] closure {sc} {v['tradeDate']} → {v['actualClosure']} "
            f"(预期 {v['expectedClosure']}) [{mark}]"
        )
        if not v["match"]:
            blocking = [
                f"{p}={n['readiness']}"
                + (f"/{n['reasonCode']}" if n["reasonCode"] else "")
                for p, n in sorted(v["nodes"].items())
                if not n["isConsumable"] or not n["isFullyFresh"]
            ]
            print(f"        blocking_nodes={blocking}")
            mismatches.append(
                f"{sc}@{v['tradeDate']}: expected={v['expectedClosure']} "
                f"actual={v['actualClosure']} blocking={blocking}"
            )

    if mismatches and strict:
        raise AssertionError(
            "六态 closure 严格断言失败（详见 readiness-scenario-matrix.json）:\n  "
            + "\n  ".join(mismatches)
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def _amain(verify_db_url: str, scenario: str, *, seed_twice: bool = False,
                 strict: bool = True) -> None:
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

    async def _run_once() -> None:
        async with session_factory() as s:
            if scenario == "all":
                await seed_all(s, strict=strict)
            else:
                # 单场景：先确保基础资产存在（幂等）
                await _gen_synthetic_instruments_bars(s)
                await _gen_synthetic_boards(s)
                await _gen_synthetic_released_dsa_config(s)
                await _seed_scenario(s, scenario)

    if not seed_twice:
        await _run_once()
        return

    # [审查第五节] Seed twice 严格事实向量幂等：两次运行的完整事实向量必须逐字段相等
    print("[seed] === idempotency pass 1/2 ===")
    await _run_once()
    first = await _collect_fact_vector()
    first["closureVector"] = _closure_vector(await _collect_scenario_matrix())

    print("[seed] === idempotency pass 2/2 ===")
    await _run_once()
    second = await _collect_fact_vector()
    second["closureVector"] = _closure_vector(await _collect_scenario_matrix())

    diffs = _diff_fact_vectors(first, second)
    _write_diag("seed-idempotency-diff.json", {
        "first": first,
        "second": second,
        "diffs": diffs,
        "idempotent": not diffs,
        "ignoredKeys": sorted(_IDEMPOTENCY_IGNORED_KEYS),
    })
    if diffs:
        print(f"[seed] idempotency VIOLATION: {len(diffs)} diffs (前 10 条)")
        for d in diffs[:10]:
            print(f"        {d['path']}: {d['first']!r} → {d['second']!r}")
        raise AssertionError(
            f"Seed twice 事实向量不幂等：{len(diffs)} 处差异"
            "（详见 seed-idempotency-diff.json）"
        )
    print("[seed] idempotency OK: 两次运行事实向量完全一致")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--verify-db-url",
        default=os.environ.get("DATABASE_URL"),
        help="bz_stock_verify_<sha> 异步连接串；默认读取受控验证容器 DATABASE_URL",
    )
    ap.add_argument(
        "--scenario",
        default="all",
        choices=["all", *sorted(_SCENARIO_TRADE_DATES)],
        help="六态 canonical 场景；all 表示全部装配并做严格 closure 断言",
    )
    ap.add_argument(
        "--seed-twice",
        action="store_true",
        help="运行两次并对完整事实向量做结构化 diff（审查第五节幂等证明）",
    )
    ap.add_argument(
        "--no-strict",
        action="store_true",
        help="仅诊断：不因 closure 不匹配抛错（不得用于绿色验收）",
    )
    args = ap.parse_args()
    if not args.verify_db_url:
        ap.error("--verify-db-url or DATABASE_URL is required")
    asyncio.run(_amain(
        args.verify_db_url, args.scenario,
        seed_twice=args.seed_twice, strict=not args.no_strict,
    ))


if __name__ == "__main__":
    main()
