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
# warmup 使用独立 synthetic run ID，避免 succeeded items 污染正式 5293 full run
WARMUP_RUN_ID = uuid.UUID("f4b0f00d-0000-4000-8000-000000000001")
TRADE_DATE = date(2026, 8, 17)
EXPECTED_UNIVERSE_SHA256 = "c57e1737fed01f531aadf7b653d10ae92539b2986781988e2a33a498dfb45577"
EXPECTED_PARAMETER_HASH = "5284aa5fec7f58ce65ee7fcf416be5620baa21a345e93c7e942995ef654705aa"
PRODUCTION_CODE_SHA = "ac9c3810b63f64e702b0d60f7e7822112ab137fb"
BATCH_SIZE = 25
WARMUP_STOCKS = 25

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "4B-server-db"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 真实业务进度日志（长任务治理）：每批由 production progress_callback 触发，
# 写入 progress.jsonl，供服务器外部 runner 判断真实进展（而非 generic timeout）。
PROGRESS_JSONL = OUTPUT_DIR / "progress.jsonl"


def _progress_writer() -> "callable":
    """返回 production 兼容的 progress_callback：写入 progress.jsonl 一行 JSON。

    签名对齐 compute_review_core_with_run_items 的回调契约：
        (processed, total, snapshot_count, failed_count)
    不新增 observability framework，只复用已有回调。
    """
    import atexit

    class _Writer:
        def __init__(self) -> None:
            self._fh = open(PROGRESS_JSONL, "w", encoding="utf-8")
            atexit.register(self._fh.close)

        async def __call__(
            self, *, processed: int, total: int,
            snapshot_count: int, failed_count: int,
        ) -> None:
            coverage = (snapshot_count / total) if total > 0 else 0.0
            line = json.dumps(
                {
                    "timestamp": now_iso(),
                    "processed": processed,
                    "completed": processed,
                    "succeeded": snapshot_count,
                    "failed": failed_count,
                    "total": total,
                    "coverage": round(coverage, 6),
                    "elapsed_sec": round(time.perf_counter() - _progress_writer._t0, 3),
                },
                ensure_ascii=False,
            )
            self._fh.write(line + "\n")
            self._fh.flush()

    _progress_writer._t0 = time.perf_counter()
    return _Writer()


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
    "statement_count": 0,
    "other_statement_count": 0,
    "db_write_attempts": 0,
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
# 3. In-memory fake run-item store（移植自 4A-3L 已验证实现）
#    字段对齐 StockFeatureSnapshotRunItem，供 orchestration 读取：
#      item.id / item.instrument_id / item.status / item.lease_epoch /
#      item.attempt_count / item.error
#    按 (run_id) 分桶，warmup / full run 天然隔离。
# ============================================================================
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from typing import Any


@dataclass
class _MemItem:
    id: uuid.UUID
    snapshot_run_id: uuid.UUID
    instrument_id: uuid.UUID
    phase: str = "core"
    status: str = "pending"
    attempt_count: int = 0
    lease_epoch: int = 0
    error: str | None = None


class InMemoryRunItemStore:
    """按 (run_id, phase) 维护 items，模拟 create/claim/mark/progress 语义。"""

    def __init__(self) -> None:
        self._by_run: dict[uuid.UUID, dict[uuid.UUID, _MemItem]] = defaultdict(dict)
        self._seq = 0

    def _new_id(self) -> uuid.UUID:
        self._seq += 1
        return uuid.UUID(int=self._seq)

    async def create_run_items(
        self, session, snapshot_run_id, instrument_ids, *, phase="core",
        input_hash=None,
    ) -> int:
        store = self._by_run[snapshot_run_id]
        created = 0
        for iid in instrument_ids:
            key = uuid.UUID(str(iid))
            if key in store:
                continue
            store[key] = _MemItem(
                id=self._new_id(),
                snapshot_run_id=snapshot_run_id,
                instrument_id=key,
                phase=phase,
                status="pending",
            )
            created += 1
        return created

    async def claim_items(
        self, session, snapshot_run_id, *, worker_instance_id, batch_size=25,
        phase="core", lease_seconds=120, max_attempt_count=3,
    ) -> list[_MemItem]:
        store = self._by_run[snapshot_run_id]
        claimed: list[_MemItem] = []
        for item in store.values():
            if len(claimed) >= batch_size:
                break
            if item.phase != phase:
                continue
            if item.status == "pending":
                pass
            elif item.status == "failed" and item.attempt_count < max_attempt_count:
                pass
            else:
                continue
            item.status = "running"
            item.attempt_count += 1
            item.lease_epoch += 1
            claimed.append(item)
        return claimed

    async def mark_item_succeeded(
        self, session, item_id, *, result_count=None, lease_epoch=None,
    ) -> bool:
        for store in self._by_run.values():
            for item in store.values():
                if item.id == item_id and item.status == "running":
                    item.status = "succeeded"
                    return True
        return False

    async def mark_item_failed(
        self, session, item_id, error, *, lease_epoch=None,
    ) -> bool:
        for store in self._by_run.values():
            for item in store.values():
                if item.id == item_id and item.status == "running":
                    item.status = "failed"
                    item.error = (error or "")[:1000]
                    return True
        return False

    async def get_run_progress(
        self, session, snapshot_run_id, *, phase="core",
    ) -> dict[str, Any]:
        store = self._by_run.get(snapshot_run_id, {})
        counts = Counter(i.status for i in store.values() if i.phase == phase)
        succeeded = counts.get("succeeded", 0)
        failed = counts.get("failed", 0)
        pending = counts.get("pending", 0)
        running = counts.get("running", 0)
        skipped = counts.get("skipped", 0)
        total = succeeded + failed + pending + running + skipped
        coverage = succeeded / total if total > 0 else 0.0
        return {
            "succeeded": succeeded, "failed": failed, "pending": pending,
            "running": running, "skipped": skipped, "total": total,
            "coverage": coverage,
        }


STORE = InMemoryRunItemStore()


# ============================================================================
# 4. Harness: monkeypatch seams
# ============================================================================
def install_harness(ids: list[uuid.UUID]) -> None:
    # ---- fake run-item service (in-memory, no DB write) ----
    async def fake_create_run_items(session, snapshot_run_id, instrument_ids, **kwargs):
        return await STORE.create_run_items(session, snapshot_run_id, instrument_ids, **kwargs)

    async def fake_claim_items(session, snapshot_run_id, **kwargs):
        return await STORE.claim_items(session, snapshot_run_id, **kwargs)

    async def fake_mark_item_succeeded(session, item_id, **kwargs):
        return await STORE.mark_item_succeeded(session, item_id, **kwargs)

    async def fake_mark_item_failed(session, item_id, error="", **kwargs):
        return await STORE.mark_item_failed(session, item_id, error, **kwargs)

    async def fake_get_run_progress(session, snapshot_run_id, **kwargs):
        return await STORE.get_run_progress(session, snapshot_run_id, **kwargs)

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
#    挂 async_engine.sync_engine（AsyncEngine 不支持 cursor execute 事件）。
#    仅记录只读 SELECT；任意写/DDL 触发 fail-closed guard。
# ============================================================================
class UnexpectedDatabaseWrite(RuntimeError):
    pass


_WRITE_KEYWORDS = (
    "insert", "update", "delete", "merge", "create", "alter", "drop", "truncate"
)


def _is_write_statement(sql_text: str) -> bool:
    head = (sql_text or "").strip().lower()
    first = head.split(None, 1)[0] if head else ""
    return first in _WRITE_KEYWORDS


def install_sql_listener(engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._sql_t0 = time.perf_counter()  # type: ignore[attr-defined]
        if _is_write_statement(statement):
            SQL_METRICS["db_write_attempts"] += 1
            raise UnexpectedDatabaseWrite(
                f"4B READ-ONLY guard: write/DDL detected -> {statement[:120]!r}"
            )

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        dt = time.perf_counter() - getattr(context, "_sql_t0", time.perf_counter())
        if _is_write_statement(statement):
            return  # 已在 before 拦截
        head = (statement or "").strip().lower()
        is_select = head.startswith("select") or head.startswith("with")
        SQL_METRICS["statement_count"] += 1
        if is_select:
            SQL_METRICS["select_count"] += 1
            SQL_METRICS["select_elapsed"] += dt
            cat = _classify(statement)
            c = SQL_METRICS["by_category"][cat]
            c["count"] += 1
            c["elapsed"] += dt
        else:
            SQL_METRICS["other_statement_count"] += 1


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


def verify_production_checkout() -> None:
    """Hard gate：被 import 的 production checkout 必须 exact == ac9c3810。

    在连接 DB 之前执行；不符则 STOP（不自动部署 dev）。
    """
    import subprocess

    repo_root = os.environ.get("PANJI_REPO_ROOT", str(REPO_ROOT))
    try:
        actual = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[4B] STOP: 无法读取 production checkout SHA: {exc}")
        raise SystemExit(2)
    print(f"[4B] production checkout HEAD = {actual}")
    print(f"[4B] required baseline        = {PRODUCTION_CODE_SHA}")
    if actual != PRODUCTION_CODE_SHA:
        print("[4B] STOP: production checkout SHA 不等于 ac9c3810，拒绝运行")
        raise SystemExit(2)


def verify_harness_sha_env() -> str:
    """Hard gate：PANJI_BENCHMARK_HARNESS_SHA 必须存在且为完整 40 位 hex。"""
    raw = os.environ.get("PANJI_BENCHMARK_HARNESS_SHA", "").strip()
    if len(raw) != 40 or not all(c in "0123456789abcdef" for c in raw):
        print(
            f"[4B] STOP: PANJI_BENCHMARK_HARNESS_SHA 缺失或非法"
            f"（需要完整 40 位 hex），得到 {raw!r}"
        )
        raise SystemExit(2)
    return raw


async def main() -> int:
    print(f"[4B] 启动 {now_iso()}")

    # ---- Hard gate：production checkout 必须 == ac9c3810（DB 访问前） ----
    verify_production_checkout()
    harness_sha = verify_harness_sha_env()

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
    install_sql_listener(async_engine.sync_engine)

    # ---- warmup（独立 WARMUP_RUN_ID，25 股 1 batch，不计入；不污染 full run） ----
    print(f"[4B] warmup {WARMUP_STOCKS} stocks (run={WARMUP_RUN_ID}) ...")
    warmup_ids = ids[:WARMUP_STOCKS]
    await fss.compute_review_core_with_run_items(
        TRADE_DATE,
        warmup_ids,
        WARMUP_RUN_ID,
        batch_size=BATCH_SIZE,
        failure_threshold=1.0,
        released_config_resolver=None,
    )
    # 重置计时，warmup 不计入结果（只清 counters，不动 store / full-run items）
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
    SQL_METRICS["statement_count"] = 0
    SQL_METRICS["other_statement_count"] = 0
    SQL_METRICS["db_write_attempts"] = 0

    # ---- full run：5293 股，1 次（历史 SNAPSHOT_RUN_ID） ----
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
        progress_callback=await _progress_writer(),
    )
    run_wall = time.perf_counter() - run_start
    run_cpu_time = time.process_time() - run_start_cpu

    peak_rss_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024

    # 其他编排开销残差（db_fallback_elapsed 已含于 review_core_elapsed，不再减）
    accounted = (
        C.mdas_elapsed
        + C.review_core_elapsed
        + C.fake_persist_elapsed
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
        "db_write_attempts": SQL_METRICS["db_write_attempts"],
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
        "db_fallback_subcomponent": {
            "calls": C.db_fallback_calls,
            "elapsed_sec": round(C.db_fallback_elapsed, 4),
            "note": "db_fallback 发生于 compute_review_core_for_trade_date 内部，"
            "其 elapsed 已含于 review_core_compute_wall_clock_sec，不单独再加。理想 calls=0。",
        },
        "serialization_artifact_sec": None,
        "fake_snapshot_persist_sec": round(C.fake_persist_elapsed, 4),
        "other_orchestration_sec": round(other_orchestration, 4),
        "note": "MDAS batch 与 Review-Core compute 正常路径不嵌套：MDAS batch 结果直接传入 "
        "compute；db_fallback 仅当 bars=None 时触发（理想 0）。other_orchestration 为 "
        "total - mdas - review_core - fake_persist 残差（不含 db_fallback）。",
    }

    sql_read_metrics = {
        "select_count": SQL_METRICS["select_count"],
        "select_total_elapsed_sec": round(SQL_METRICS["select_elapsed"], 3),
        "statement_count": SQL_METRICS["statement_count"],
        "other_statement_count": SQL_METRICS["other_statement_count"],
        "db_write_attempts": SQL_METRICS["db_write_attempts"],
        "by_category": {
            k: {"count": v["count"], "elapsed_sec": round(v["elapsed"], 3)}
            for k, v in SQL_METRICS["by_category"].items()
        },
        "note": "仅记录 SELECT/read CTE 的 count/elapsed 与粗粒度表分类，不保存 SQL 参数/"
        "凭据/query text；任意 INSERT/UPDATE/DELETE/MERGE/DDL 触发 read-only guard 并计数。",
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
    progress = await STORE.get_run_progress(None, SNAPSHOT_RUN_ID)
    if progress["failed"] > 0:
        for item in STORE._by_run.get(SNAPSHOT_RUN_ID, {}).values():
            if item.status == "failed":
                failures.append(
                    {"instrument_id": str(item.instrument_id), "error": item.error}
                )

    # 运行身份：仅读取 remote runner 注入的环境变量并记录，harness 本身不探测服务器。
    # 四个身份必须互相一致（由 remote runner 在启动容器前 gate），此处只做记录与一致性提示。
    runtime_identities = {
        "production_code_sha": PRODUCTION_CODE_SHA,
        "benchmark_harness_sha": harness_sha,
        "server_repo_head": os.environ.get("PANJI_SERVER_REPO_HEAD", ""),
        "live_runtime_sha": os.environ.get("PANJI_LIVE_RUNTIME_SHA", ""),
        "runtime_git_sha": os.environ.get("PANJI_RUNTIME_GIT_SHA", ""),
        "note": "production_code_sha 为被审计的 production baseline (ac9c3810)，"
        "运行时经 git rev-parse 硬校验；benchmark_harness_sha 由运行环境通过 "
        "PANJI_BENCHMARK_HARNESS_SHA 注入（即本次 harness 脚本的 commit SHA）。"
        "server_repo_head / live_runtime_sha / runtime_git_sha 由 governed remote runner "
        "在容器启动前 gate 并注入，harness 仅记录，不自行探测服务器。四者中 "
        "production_code_sha 与 server_repo_head / live_runtime_sha / runtime_git_sha 必须相等；"
        "benchmark_harness_sha 允许不同（harness 与 production 是两个独立身份）。",
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
    _write("evidence_meta.json", runtime_identities)
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


async def harness_smoke() -> int:
    """本地 unit-level harness smoke：验证 run-item store contract 与 run 隔离。

    不连接真实 DB；只验证 InMemoryRunItemStore 对象契约与 warmup/full 分桶隔离。
    """
    print("[4B-smoke] 启动（不连接真实 DB）")
    sample_ids = [uuid.uuid4() for _ in range(25)]

    # 1) create 25 fake items
    n = await STORE.create_run_items(None, WARMUP_RUN_ID, sample_ids, phase="core")
    assert n == 25, f"create_run_items 应创建 25，得到 {n}"

    # 2) claim 返回对象契约：pending -> running，attempt_count/lease_epoch +=1
    claimed = await STORE.claim_items(
        None, WARMUP_RUN_ID, worker_instance_id="smoke", batch_size=25
    )
    assert len(claimed) == 25, f"claim 应返回 25，得到 {len(claimed)}"
    first = claimed[0]
    assert hasattr(first, "id") and hasattr(first, "instrument_id"), "item 缺 id/instrument_id"
    assert hasattr(first, "lease_epoch") and hasattr(first, "status"), "item 缺 lease_epoch/status"
    assert first.status == "running", f"claim 后 status 应为 running，得到 {first.status}"
    assert first.attempt_count == 1 and first.lease_epoch == 1, "attempt_count/lease_epoch 应 +1"

    # 3) mark succeeded 25/25
    ok = 0
    for it in claimed:
        if await STORE.mark_item_succeeded(None, it.id):
            ok += 1
    assert ok == 25, f"mark_succeeded 应 25，得到 {ok}"

    # 4) progress 反映 succeeded=25
    prog = await STORE.get_run_progress(None, WARMUP_RUN_ID, phase="core")
    assert prog["succeeded"] == 25 and prog["total"] == 25, f"progress 异常: {prog}"

    # 5) warmup/full 隔离：SNAPSHOT_RUN_ID 不应被 WARMUP_RUN_ID 污染
    full_prog = await STORE.get_run_progress(None, SNAPSHOT_RUN_ID, phase="core")
    assert full_prog["total"] == 0, f"full run store 应仍为空，得到 {full_prog}"

    # 额外：mark_failed 路径
    await STORE.create_run_items(None, SNAPSHOT_RUN_ID, [uuid.uuid4()], phase="core")
    f_claimed = await STORE.claim_items(
        None, SNAPSHOT_RUN_ID, worker_instance_id="smoke", batch_size=25
    )
    await STORE.mark_item_failed(None, f_claimed[0].id, "smoke-error")
    f_prog = await STORE.get_run_progress(None, SNAPSHOT_RUN_ID, phase="core")
    assert f_prog["failed"] == 1, f"failed 应 1，得到 {f_prog}"

    print("[4B-smoke] PASS: 25 fake items / claim object contract / 25/25 succeeded / "
          "warmup-full isolation / mark_failed 均验证通过")
    return 0


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        rc = asyncio.run(harness_smoke())
    else:
        rc = asyncio.run(main())
    raise SystemExit(rc)
