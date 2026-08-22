"""Phase 4B — Server DB-backed Full-Universe Review-Core Benchmark.

目标：在服务器直接连接真实 PostgreSQL，对 2026-08-17 已持久化的 5293 股 universe
执行一次全量 Review-Core 主链 benchmark，测真实 DB read + MDAS + CPU compute wall-clock。

本轮不使用任何 parquet / FrozenMDAS / frozen dataset。

设计边界（来自用户规格）：
- DB reads REAL（symbol SELECT / MDAS bars_daily+adj_factor / released config SELECT 全部真实）
- CPU compute REAL（compute_review_core_for_trade_date 真实执行）
- DB writes IN-MEMORY（run-item 状态 + snapshot upsert 全部 fake，不污染生产表）
- MDAS 为真实 production MarketDataAggregationService，但强制 allow_backfill=False（薄 DBOnly 语义）
- external provider calls = 0 强证据：所有 fetch_*_bars (pytdx) 调用即 FAIL（真实行情必须来自 DB）
- DB fallback (_fetch_bars_from_db) 真实调用 original 并计时/计数（不 raise），正常成功路径理想值 = 0
- batch_size = 25（与生产主链一致）
- 不增加 parallel workers，serial per-stock compute

Timing 模型（正确，非嵌套）：
- mdas_wall  = Σ real get_bars_batch elapsed      （主链 batch 预读）
- review_core_wall = Σ compute_review_core_for_trade_date elapsed（逐股 CPU compute）
- db_fallback = Σ _fetch_bars_from_db calls       （正常路径应为 0）
两者在正常成功路径不嵌套：MDAS batch 结果直接传入 compute，compute 内部仅当 bars=None 才 fallback。

输出目录：experiments/duplicate_compute_audit/output/4B-server-db/
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import resource
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# ============================================================================
# 0. 路径 + 环境（必须在 import app.* 前完成）
# ============================================================================
# 4B 不依赖 cwd：脚本自己解析 backend root，并支持 PANJI_REPO_ROOT / PANJI_BACKEND_ROOT
# 覆盖（服务器可放 /tmp，通过环境变量指向 /root/web_dev/backend 的 production code）。
REPO_ROOT = Path(
    os.environ.get("PANJI_REPO_ROOT", Path(__file__).resolve().parents[2])
).resolve()

BACKEND_ROOT = Path(
    os.environ.get("PANJI_BACKEND_ROOT", REPO_ROOT / "backend")
).resolve()

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 4B 必须运行在真实服务器环境：禁止 dummy DATABASE_URL / REDIS_URL / APP_ENV=test。
# 配置必须由真实运行环境继承；缺失则 fail-closed。
# 注意：production app.config 在 import 期即校验 DATABASE_URL 与 REDIS_URL，
# 这里显式 fail-closed 让缺失环境得到清晰报错，而非被内部异常淹没。
_required_env = ["DATABASE_URL", "REDIS_URL"]
_missing = [k for k in _required_env if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        f"4B requires real server environment: missing {_missing}. "
        "Do not set dummy DATABASE_URL; run on the production server with real config."
    )

import pandas as pd
import psutil
from sqlalchemy import event, text

# ---- production imports ----
from app.db import AsyncSessionLocal, async_engine
from app.services import feature_snapshot_service as fss
from app.services import snapshot_run_item_service as sris
from app.services.market_data_aggregation_service import (
    MarketDataAggregationService,
)
from app.services.core_run_context import (
    SqlAlchemyReleasedConfigResolver,
    resolve_core_run_context,
)

# ============================================================================
# 常量（来自用户规格 / 已审 Git evidence）
# ============================================================================
SNAPSHOT_RUN_ID = uuid.UUID("2b7c5877-7d36-4396-84c3-7186dc911073")
TRADE_DATE = date(2026, 8, 17)
EXPECTED_UNIVERSE_SHA256 = "c57e1737fed01f531aadf7b653d10ae92539b2986781988e2a33a498dfb45577"
EXPECTED_PARAMETER_HASH = "5284aa5fec7f58ce65ee7fcf416be5620baa21a345e93c7e942995ef654705aa"
PRODUCTION_CODE_SHA = "ac9c3810b63f64e702b0d60f7e7822112ab137fb"
BATCH_SIZE = 25
WARMUP_STOCKS = 25

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "4B-server-db"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 计时 / 计数
# ============================================================================
class Counters:
    def __init__(self) -> None:
        self.mdas_elapsed = 0.0
        self.mdas_calls = 0
        self.mdas_bars = 0
        self.review_core_elapsed = 0.0
        self.review_core_calls = 0
        self.db_fallback_calls = 0
        self.db_fallback_elapsed = 0.0
        self.fake_persist_elapsed = 0.0
        self.fake_persist_calls = 0
        self.external_calls = 0


C = Counters()

# SQL 读指标（experiment-only listener）
SQL_METRICS = {
    "select_count": 0,
    "select_elapsed": 0.0,
    "by_category": {
        "bars_daily": {"count": 0, "elapsed": 0.0},
        "adj_factor": {"count": 0, "elapsed": 0.0},
        "instruments": {"count": 0, "elapsed": 0.0},
        "strategy": {"count": 0, "elapsed": 0.0},
        "other": {"count": 0, "elapsed": 0.0},
    },
}


# ============================================================================
# 1. Universe gate（真实 DB read）
# ============================================================================
async def read_universe() -> list[uuid.UUID]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text(
                "SELECT DISTINCT instrument_id FROM stock_feature_snapshot_run_items "
                "WHERE snapshot_run_id = :rid"
            ),
            {"rid": SNAPSHOT_RUN_ID},
        )
        return [r[0] for r in rows]


def compute_universe_sha256(ids: list[uuid.UUID]) -> str:
    return hashlib.sha256(
        "\x00".join(sorted(str(i) for i in ids)).encode()
    ).hexdigest()


# ============================================================================
# 2. Config gate（真实 released config resolver）
# ============================================================================
async def resolve_config(ids: list[uuid.UUID]):
    async with AsyncSessionLocal() as cfg_db:
        resolver = SqlAlchemyReleasedConfigResolver(cfg_db)
        return await resolve_core_run_context(
            trade_date=TRADE_DATE,
            snapshot_run_id=SNAPSHOT_RUN_ID,
            eligible_instrument_ids=ids,
            resolver=resolver,
        )


# ============================================================================
# 3. In-memory fake run-item store + fake snapshot upsert
# ============================================================================
class InMemoryRunItemStore:
    def __init__(self) -> None:
        self._items: dict[uuid.UUID, dict] = {}
        self._run_items: dict[uuid.UUID, list[uuid.UUID]] = {}

    def seed(self, run_id: uuid.UUID, instrument_ids: list[uuid.UUID]) -> int:
        existing = self._run_items.setdefault(run_id, [])
        n = 0
        for iid in instrument_ids:
            if iid not in self._items:
                self._items[iid] = {
                    "instrument_id": iid,
                    "status": "pending",
                    "error": None,
                }
                existing.append(iid)
                n += 1
        return n

    def claim(self, run_id: uuid.UUID, batch_size: int) -> list[dict]:
        out = []
        for iid in self._run_items.get(run_id, []):
            it = self._items[iid]
            if it["status"] == "pending":
                it["status"] = "claimed"
                out.append(it)
                if len(out) >= batch_size:
                    break
        return out

    def mark_succeeded(self, item_id: uuid.UUID) -> None:
        self._items[item_id]["status"] = "succeeded"

    def mark_failed(self, item_id: uuid.UUID, error: str) -> None:
        self._items[item_id]["status"] = "failed"
        self._items[item_id]["error"] = error

    def progress(self, run_id: uuid.UUID) -> dict:
        items = [self._items[i] for i in self._run_items.get(run_id, [])]
        total = len(items)
        succeeded = sum(1 for i in items if i["status"] == "succeeded")
        failed = sum(1 for i in items if i["status"] == "failed")
        coverage = (succeeded / total) if total else 0.0
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "coverage": coverage,
        }


STORE = InMemoryRunItemStore()


# ============================================================================
# 4. Harness: monkeypatch seams
# ============================================================================
def install_harness(ids: list[uuid.UUID]) -> None:
    # ---- fake run-item service (in-memory, no DB write) ----
    async def fake_create_run_items(session, snapshot_run_id, instrument_ids, **kwargs):
        return STORE.seed(snapshot_run_id, list(instrument_ids))

    async def fake_claim_items(session, snapshot_run_id, *, batch_size=BATCH_SIZE, **kwargs):
        return STORE.claim(snapshot_run_id, batch_size)

    async def fake_mark_item_succeeded(session, item_id, **kwargs):
        STORE.mark_succeeded(item_id)
        return True

    async def fake_mark_item_failed(session, item_id, error="", **kwargs):
        STORE.mark_failed(item_id, error)
        return True

    async def fake_get_run_progress(session, snapshot_run_id, **kwargs):
        return STORE.progress(snapshot_run_id)

    sris.create_run_items = fake_create_run_items
    sris.claim_items = fake_claim_items
    sris.mark_item_succeeded = fake_mark_item_succeeded
    sris.mark_item_failed = fake_mark_item_failed
    sris.get_run_progress = fake_get_run_progress

    # ---- fake snapshot upsert (in-memory, no DB write) ----
    async def fake_upsert_snapshot(session, snapshot, **kwargs):
        t0 = time.perf_counter()
        _ = snapshot  # 真实持久化被跳过
        C.fake_persist_calls += 1
        C.fake_persist_elapsed += time.perf_counter() - t0
        return snapshot

    fss.upsert_snapshot = fake_upsert_snapshot

    # ---- 真实 MDAS，但强制 allow_backfill=False（薄 DBOnly 语义，不改数据来源） ----
    _real_get_bars_batch = MarketDataAggregationService.get_bars_batch

    async def db_only_get_bars_batch(self, session, instrument_ids, **kwargs):
        t0 = time.perf_counter()
        kwargs["allow_backfill"] = False
        result = await _real_get_bars_batch(self, session, instrument_ids, **kwargs)
        C.mdas_elapsed += time.perf_counter() - t0
        C.mdas_calls += 1
        for v in result.values():
            if hasattr(v, "actual_count"):
                C.mdas_bars += int(v.actual_count)
        return result

    MarketDataAggregationService.get_bars_batch = db_only_get_bars_batch

    # ---- DB fallback: 真实调用 original 并计时/计数（不 raise） ----
    # 正常成功路径下 primary_bars 已由 batch 预读传入，fallback 应为 0。
    _real_fetch_bars = fss._fetch_bars_from_db

    async def timed_fetch_bars_from_db(*args, **kwargs):
        C.db_fallback_calls += 1
        t0 = time.perf_counter()
        try:
            return await _real_fetch_bars(*args, **kwargs)
        finally:
            C.db_fallback_elapsed += time.perf_counter() - t0

    fss._fetch_bars_from_db = timed_fetch_bars_from_db

    # ---- external provider guard：调用即 FAIL（真实行情必须来自 DB） ----
    class UnexpectedExternalFetch(RuntimeError):
        pass

    async def _fail_external(*args, **kwargs):
        C.external_calls += 1
        raise UnexpectedExternalFetch(
            "4B 禁止 pytdx/external provider 调用（DB-only benchmark）"
        )

    import app.services.market_data_aggregation_service as mdas_mod

    for fn in (
        "fetch_daily_bars",
        "fetch_15min_bars",
        "fetch_60min_bars",
        "fetch_today_daily_bars",
        "fetch_minute_bars",
    ):
        setattr(mdas_mod, fn, _fail_external)

    # ---- Review-Core compute 计时（独立，不嵌套 MDAS batch） ----
    _real_compute = fss.compute_review_core_for_trade_date

    async def timed_compute(*args, **kwargs):
        t0 = time.perf_counter()
        result = await _real_compute(*args, **kwargs)
        C.review_core_elapsed += time.perf_counter() - t0
        C.review_core_calls += 1
        return result

    fss.compute_review_core_for_trade_date = timed_compute


# ============================================================================
# 5. SQL read metrics listener（experiment-only）
# ============================================================================
def install_sql_listener(engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._sql_t0 = time.perf_counter()  # type: ignore[attr-defined]

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        dt = time.perf_counter() - getattr(context, "_sql_t0", time.perf_counter())
        SQL_METRICS["select_count"] += 1
        SQL_METRICS["select_elapsed"] += dt
        cat = _classify(statement)
        c = SQL_METRICS["by_category"][cat]
        c["count"] += 1
        c["elapsed"] += dt


def _classify(sql_text: str) -> str:
    t = (sql_text or "").lower()
    if "bars_daily" in t:
        return "bars_daily"
    if "adj_factor" in t:
        return "adj_factor"
    if "instruments" in t:
        return "instruments"
    if "strategy" in t:
        return "strategy"
    return "other"


# ============================================================================
# 6. 主流程
# ============================================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def machine_info(avg_cpu=None) -> dict:
    vm = psutil.virtual_memory()
    return {
        "hostname_anonymized": platform.node()[:3] + "-***",
        "cpu_model": platform.processor() or "unknown",
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "ram_total_gib": round(vm.total / (1024**3), 2),
        "python_version": platform.python_version(),
        "pandas_version": pd.__version__,
        "numpy_version": __import__("numpy").__version__,
        "avg_cpu_percent": avg_cpu,
        "recorded_at": now_iso(),
    }


async def main() -> int:
    print(f"[4B] 启动 {now_iso()}")

    # ---- Universe gate ----
    t0 = time.perf_counter()
    ids = await read_universe()
    universe_read_elapsed = time.perf_counter() - t0
    universe_sha = compute_universe_sha256(ids)
    print(f"[4B] universe: {len(ids)} instruments, sha256={universe_sha}")
    print(f"[4B] expected sha256={EXPECTED_UNIVERSE_SHA256}")
    if universe_sha != EXPECTED_UNIVERSE_SHA256:
        print("[4B] STOP: universe hash 不等，数据库样本可能已变化")
        return 2
    if len(ids) != 5293:
        print(f"[4B] STOP: universe 大小 {len(ids)} != 5293")
        return 2

    # ---- Config gate ----
    t0 = time.perf_counter()
    ctx = await resolve_config(ids)
    config_elapsed = time.perf_counter() - t0
    param_hash = ctx.parameter_hash
    print(f"[4B] parameter_hash={param_hash}")
    print(f"[4B] expected     ={EXPECTED_PARAMETER_HASH}")
    if param_hash != EXPECTED_PARAMETER_HASH:
        print("[4B] STOP: parameter_hash 不等，released config 可能已变化")
        return 2

    # ---- install harness ----
    install_harness(ids)
    install_sql_listener(async_engine)

    # ---- warmup（25 股 1 batch，不计入） ----
    print(f"[4B] warmup {WARMUP_STOCKS} stocks ...")
    warmup_ids = ids[:WARMUP_STOCKS]
    STORE.seed(SNAPSHOT_RUN_ID, warmup_ids)
    await fss.compute_review_core_with_run_items(
        TRADE_DATE,
        warmup_ids,
        SNAPSHOT_RUN_ID,
        batch_size=BATCH_SIZE,
        failure_threshold=1.0,
        released_config_resolver=None,
    )
    # 重置计时，warmup 不计入结果
    C.mdas_elapsed = C.review_core_elapsed = C.db_fallback_elapsed = 0.0
    C.mdas_calls = C.review_core_calls = C.db_fallback_calls = 0
    C.mdas_bars = 0
    C.fake_persist_elapsed = 0.0
    C.fake_persist_calls = 0
    for c in SQL_METRICS["by_category"].values():
        c["count"] = 0
        c["elapsed"] = 0.0
    SQL_METRICS["select_count"] = 0
    SQL_METRICS["select_elapsed"] = 0.0

    # ---- full run：5293 股，1 次 ----
    print(f"[4B] FULL RUN {len(ids)} stocks, batch_size={BATCH_SIZE} ...")
    run_start = time.perf_counter()
    run_start_cpu = time.process_time()
    result = await fss.compute_review_core_with_run_items(
        TRADE_DATE,
        ids,
        SNAPSHOT_RUN_ID,
        batch_size=BATCH_SIZE,
        failure_threshold=1.0,
        released_config_resolver=None,
    )
    run_wall = time.perf_counter() - run_start
    run_cpu_time = time.process_time() - run_start_cpu

    peak_rss_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024

    # 其他编排开销（fake bookkeeping + serialization 难以单独分离，用残差近似）
    accounted = (
        C.mdas_elapsed
        + C.review_core_elapsed
        + C.fake_persist_elapsed
        + C.db_fallback_elapsed
    )
    other_orchestration = max(run_wall - accounted, 0.0)

    try:
        avg_cpu = psutil.Process().cpu_percent(interval=1.0)
    except Exception:
        avg_cpu = None

    # ====================================================================
    # 输出
    # ====================================================================
    server_env = machine_info(avg_cpu=avg_cpu)

    universe_gate = {
        "snapshot_run_id": str(SNAPSHOT_RUN_ID),
        "trade_date": TRADE_DATE.isoformat(),
        "instrument_count": len(ids),
        "distinct_instrument_count": len(set(ids)),
        "computed_sha256": universe_sha,
        "expected_sha256": EXPECTED_UNIVERSE_SHA256,
        "match": universe_sha == EXPECTED_UNIVERSE_SHA256,
        "universe_read_elapsed_sec": round(universe_read_elapsed, 4),
    }

    config_gate = {
        "parameter_hash": param_hash,
        "expected_parameter_hash": EXPECTED_PARAMETER_HASH,
        "match": param_hash == EXPECTED_PARAMETER_HASH,
        "algorithm_versions": ctx.algorithm_versions,
        "config_resolution_elapsed_sec": round(config_elapsed, 4),
    }

    full_run_metrics = {
        "trade_date": TRADE_DATE.isoformat(),
        "universe_size": len(ids),
        "batch_size": BATCH_SIZE,
        "snapshot_count": result.get("snapshot_count"),
        "failed_count": result.get("failed_count"),
        "skipped_count": result.get("skipped_count"),
        "coverage": result.get("coverage"),
        "failure_threshold": 1.0,
        "mdas_batch_reads": result.get("mdas_batch_read_count"),
        "total_wall_clock_sec": round(run_wall, 3),
        "total_cpu_process_time_sec": round(run_cpu_time, 3),
        "external_provider_calls": C.external_calls,
        "db_fallback_calls": C.db_fallback_calls,
        "db_writes": 0,
        "scheduler_triggers": 0,
        "publish_calls": 0,
    }

    stage_timing = {
        "total_wall_clock_sec": round(run_wall, 3),
        "universe_read_sec": round(universe_read_elapsed, 4),
        "config_resolution_sec": round(config_elapsed, 4),
        "mdas_db_wall_clock_sec": round(C.mdas_elapsed, 3),
        "mdas_calls": C.mdas_calls,
        "mdas_bars_aggregated": C.mdas_bars,
        "review_core_compute_wall_clock_sec": round(C.review_core_elapsed, 3),
        "review_core_calls": C.review_core_calls,
        "db_fallback_calls": C.db_fallback_calls,
        "db_fallback_elapsed_sec": round(C.db_fallback_elapsed, 4),
        "serialization_artifact_sec": None,
        "fake_snapshot_persist_sec": round(C.fake_persist_elapsed, 4),
        "other_orchestration_sec": round(other_orchestration, 4),
        "note": "MDAS batch 与 Review-Core compute 正常路径不嵌套：MDAS batch 结果直接传入 "
        "compute；db_fallback 仅当 bars=None 时触发（理想 0）。other_orchestration 为 "
        "total - mdas - review_core - fake_persist - db_fallback 残差。",
    }

    sql_read_metrics = {
        "select_count": SQL_METRICS["select_count"],
        "select_total_elapsed_sec": round(SQL_METRICS["select_elapsed"], 3),
        "by_category": {
            k: {"count": v["count"], "elapsed_sec": round(v["elapsed"], 3)}
            for k, v in SQL_METRICS["by_category"].items()
        },
        "note": "仅记录 SELECT count/elapsed 与粗粒度表分类，不保存 SQL 参数/凭据/query text",
    }

    local_vs_server = {
        "universe": {"local_projection": 5293, "server_actual": len(ids)},
        "local_central_projection_min": 36.72,
        "local_optimistic_projection_min": 31.21,
        "local_conservative_projection_min": 42.22,
        "server_full_wall_clock_min": round(run_wall / 60.0, 2),
        "server_db_mdas_min": round(C.mdas_elapsed / 60.0, 2),
        "server_cpu_compute_min": round(C.review_core_elapsed / 60.0, 2),
        "server_db_fallback_calls": C.db_fallback_calls,
        "server_other_orchestration_min": round(other_orchestration / 60.0, 2),
        "comparison_note": "LOCAL PROJECTION 仅作对照基线，非最终结果；"
        "服务器实测为真实 DB + real compute。",
        "decision_gate": _decision_gate(run_wall, C.mdas_elapsed, C.review_core_elapsed),
    }

    failures = []
    progress = STORE.progress(SNAPSHOT_RUN_ID)
    if progress["failed"] > 0:
        for iid, it in STORE._items.items():
            if it["status"] == "failed":
                failures.append({"instrument_id": str(iid), "error": it["error"]})

    evidence_meta = {
        "production_code_sha": PRODUCTION_CODE_SHA,
        "benchmark_harness_sha": os.environ.get(
            "PANJI_BENCHMARK_HARNESS_SHA", "TODO_FILL_AT_COMMIT"
        ),
        "note": "production_code_sha 为被审计的 production baseline (ac9c3810)；"
        "benchmark_harness_sha 由运行环境通过 PANJI_BENCHMARK_HARNESS_SHA 注入"
        "（即本次 harness 脚本的 commit SHA），不写死在脚本内以避免循环依赖。",
    }

    def _write(name: str, obj: dict) -> None:
        with open(OUTPUT_DIR / name, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    _write("server_environment.json", server_env)
    _write("universe_gate.json", universe_gate)
    _write("config_gate.json", config_gate)
    _write("full_run_metrics.json", full_run_metrics)
    _write("stage_timing.json", stage_timing)
    _write("sql_read_metrics.json", sql_read_metrics)
    _write("local_vs_server.json", local_vs_server)
    _write("evidence_meta.json", evidence_meta)
    _write("failures.jsonl", {"failures": failures} if failures else {})
    with open(OUTPUT_DIR / "batch_metrics.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "note": "4B 通过 compute_review_core_with_run_items 黑盒主链执行；"
                    "逐 batch 计时由内部 MDAS/CPU wrapper 全局聚合，见 stage_timing.json",
                    "total_batches": result.get("batch_count"),
                    "mdas_batch_reads": result.get("mdas_batch_read_count"),
                    "mdas_calls": C.mdas_calls,
                    "mdas_bars_aggregated": C.mdas_bars,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    print("=" * 60)
    print("[4B] FULL RUN 完成")
    print(f"  total wall-clock : {run_wall/60:.2f} min")
    print(f"  MDAS DB wall     : {C.mdas_elapsed/60:.2f} min")
    print(f"  Review-Core CPU  : {C.review_core_elapsed/60:.2f} min")
    print(f"  db_fallback calls: {C.db_fallback_calls}")
    print(f"  external calls   : {C.external_calls}")
    print(f"  other orchestr.  : {other_orchestration/60:.2f} min")
    print(f"  snapshot/failed  : {result.get('snapshot_count')}/{result.get('failed_count')}")
    print(f"  peak RSS         : {peak_rss_gib:.2f} GiB")
    print(f"  decision gate    : {local_vs_server['decision_gate']}")
    print("=" * 60)
    print(f"[4B] 输出目录: {OUTPUT_DIR}")
    return 0


def _decision_gate(total_sec: float, mdas_sec: float, cpu_sec: float) -> str:
    total_min = total_sec / 60.0
    mdas_min = mdas_sec / 60.0
    cpu_min = cpu_sec / 60.0
    if 31 <= total_min <= 42:
        if cpu_min >= mdas_min:
            return "A: Server≈31-42min 且 CPU 占绝大多数 → 下一步 bounded parallelism"
        return "B: Server≈31-42min 但 DB 占大量 → 先调查 DB batching/query/index"
    if total_min > 42:
        return "B: Server 明显 >42min → 先调查 DB/MDAS（batching/query/index/connection/I/O）"
    return "C: Server 明显 <31min → 先看 SLA 再决定是否值得并行"


if __name__ == "__main__":
    rc = asyncio.run(main())
    raise SystemExit(rc)
