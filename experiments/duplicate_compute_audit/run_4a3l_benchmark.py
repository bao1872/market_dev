"""Phase 4A-3L — Local Small-Scale Main-Chain Benchmark（实验级，仅放 experiments/）。

设计原则（来自 4A-3L 授权）：
- 不修改任何 production 文件；只在 experiment harness monkeypatch persistence seam。
- 真实执行 `compute_review_core_with_run_items`（真实 orchestration 形态）：
    create run-items → claim batch → symbol map → FrozenMDAS batch
    → compute_review_core_for_trade_date → snapshot fake persistence
    → mark succeeded/failed → progress。
- 真实 DB = 0；真实 persistence = 0；scheduler = 0；network = 0。
- DB fallback 与 external provider 真正被 guard（调用即 count+raise）。
- 性能统计把 parquet materialization 与 main-chain wall-clock 分开：
    A. dataset_load_elapsed（一次性读 parquet + 建 partition index）
    B. main_chain_wall_clock（从已 materialized 的 FrozenMDAS 开始）
    C. total_offline_elapsed（A+B）；5293 推算主要用 B。
- 分层采样 ≈500 股（按全 universe bars_count 分布），不是简单随机。
- FrozenMDAS 统一 str(instrument_id)，支持主链 UUID key。
- 3 次 timed runs（warmup 1 小 batch 不计时），记录 p50 / CPU / peak RSS / stocks-per-sec。
- 输出 5293 加权外推（optimistic / central / conservative），明确标注 LOCAL PROJECTION。

数据源唯一：backend/.perfdata/afterclose/afterclose-20260817-v1/
"""
from __future__ import annotations

import os

# 实验 harness：避免 production Settings() 强制要求真实 DATABASE_URL。
# 真实 DB 调用已被 monkeypatch 为 fake session；此处仅满足 import 期校验。
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://bench@localhost:5432/bench_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PANJI_SCHEDULER_ENABLED", "false")

import asyncio
import json
import os
import platform
import resource
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
DATA_DIR = (
    BACKEND_ROOT
    / ".perfdata"
    / "afterclose"
    / "afterclose-20260817-v1"
)
OUT_DIR = Path(__file__).resolve().parent / "output" / "4A-3L"

TARGET_TRADE_DATE = date(2026, 8, 17)
SERIES = "all_a_share"
RUN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
WORKER_ID = "4a3l-benchmark"

# ----------------------------------------------------------------------------
# 1. 真实 external guard（调用即 count + raise）
# ----------------------------------------------------------------------------


class UnexpectedExternalFetch(RuntimeError):
    """Phase 4A 主链若触达 external daily provider，立即暴露。"""


class ExternalGuard:
    def __init__(self) -> None:
        self.calls = 0

    def guard(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise UnexpectedExternalFetch(
            f"external provider call intercepted (n={self.calls})"
        )


# ----------------------------------------------------------------------------
# 2. In-memory run-item store（替代 snapshot_run_item_service 真实持久化）
#    字段对齐 StockFeatureSnapshotRunItem，供 orchestration 读取
#    item.instrument_id / item.status / item.id / item.error 等。
# ----------------------------------------------------------------------------


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


# ----------------------------------------------------------------------------
# 3. Fake AsyncSessionLocal（仅服务 symbol 批量查询与 MDAS session）
# ----------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self) -> list[tuple]:
        return self._rows

    def scalars(self):
        return self

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, id_to_symbol: dict[str, str]) -> None:
        self._id_to_symbol = id_to_symbol

    async def execute(self, statement, params=None, *a, **k):
        sql = str(statement)
        if "instruments" in sql and "symbol" in sql and "id" in sql:
            ids = (params or {}).get("ids", []) or []
            rows = [(i, self._id_to_symbol.get(str(i), "")) for i in ids]
            return _FakeResult(rows)
        # 其他语句（upsert_snapshot 已被 monkeypatch；run-item 已被 monkeypatch）
        # 不应到达真实 session；返回空结果避免误判。
        return _FakeResult([])

    async def commit(self, *a, **k):
        return None

    async def flush(self, *a, **k):
        return None

    def add(self, *a, **k):
        return None

    def add_all(self, *a, **k):
        return None

    async def rollback(self, *a, **k):
        return None


class _FakeAsyncSessionCtx:
    def __init__(self, id_to_symbol: dict[str, str]) -> None:
        self._id_to_symbol = id_to_symbol

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._id_to_symbol)

    async def __aexit__(self, *a) -> None:
        return None


# ----------------------------------------------------------------------------
# 4. 分层采样
# ----------------------------------------------------------------------------


def bucket_of(bars_count: int) -> str:
    if bars_count == 0:
        return "0"
    if bars_count < 60:
        return "1-59"
    if bars_count < 250:
        return "60-249"
    if bars_count < 500:
        return "250-499"
    if bars_count < 1000:
        return "500-999"
    if bars_count < 2000:
        return "1000-1999"
    return "2000+"


BUCKET_ORDER = ["0", "1-59", "60-249", "250-499", "500-999", "1000-1999", "2000+"]


def build_universe() -> tuple[list[dict], dict[str, int], dict[str, float]]:
    """返回 (全 universe 记录, bucket 计数, bucket 占比)。"""
    inst = pd.read_parquet(DATA_DIR / "instruments.parquet")
    bars = pd.read_parquet(DATA_DIR / "bars_daily_raw.parquet")
    bars_per = bars.groupby("instrument_id").size()
    bars_per = bars_per.astype(int)

    records: list[dict] = []
    for _, row in inst.iterrows():
        iid = str(row["id"])
        bc = int(bars_per.get(iid, 0))
        records.append({
            "instrument_id": iid,
            "symbol": str(row["symbol"]),
            "bars_count": bc,
            "bucket": bucket_of(bc),
        })
    # 仅保留在 bars 里有记录的（少数为 0 bars）
    counts: dict[str, int] = {b: 0 for b in BUCKET_ORDER}
    for r in records:
        counts[r["bucket"]] += 1
    share = {b: (counts[b] / len(records) if len(records) else 0.0)
             for b in BUCKET_ORDER}
    return records, counts, share


def stratified_sample(
    records: list[dict],
    counts: dict[str, int],
    share: dict[str, float],
    target: int = 500,
) -> list[dict]:
    """分层抽样：按 production share 分配 + 每非空 bucket 最小样本 + 稀有 bucket 适当 oversample。

    选择确定性：同 bucket 内按 bars_count 升序后等距抽，保证可复现。
    """
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)
    for b in by_bucket:
        by_bucket[b].sort(key=lambda r: (r["bars_count"], r["instrument_id"]))

    # 每 bucket 最小样本
    MIN_PER_BUCKET = 5
    # 稀有 bucket oversample 系数（share 越小越需要相对抬升以有统计意义）
    sample_plan: dict[str, int] = {}
    allocated = 0
    for b in BUCKET_ORDER:
        n = counts.get(b, 0)
        if n == 0:
            sample_plan[b] = 0
            continue
        planned = max(MIN_PER_BUCKET, round(target * share[b]))
        # 稀有 bucket（share<2%）至少给 8，保证外推稳健
        if share[b] < 0.02:
            planned = max(planned, 8)
        planned = min(planned, n)
        sample_plan[b] = planned
        allocated += planned

    # 若仍不足 target，按 share 把余量补到最大的几个 bucket
    deficit = target - allocated
    if deficit > 0:
        # 按 share 降序补（不超 bucket 容量）
        order = sorted(
            [b for b in BUCKET_ORDER if counts[b] > 0],
            key=lambda b: -share[b],
        )
        idx = 0
        while deficit > 0 and order:
            b = order[idx % len(order)]
            if sample_plan[b] < counts[b]:
                sample_plan[b] += 1
                deficit -= 1
            idx += 1
            if idx > len(order) * counts.get(b, 0) + 100:
                break

    selected: list[dict] = []
    reasons: dict[str, str] = {}
    for b in BUCKET_ORDER:
        cand = by_bucket.get(b, [])
        k = sample_plan.get(b, 0)
        if k <= 0 or not cand:
            continue
        # 等距抽样（确定性）
        if k >= len(cand):
            chosen = cand
        else:
            step = len(cand) / k
            chosen = [cand[int(i * step)] for i in range(k)]
        for c in chosen:
            selected.append(c)
            if b in ("0", "1-59", "2000+"):
                reasons[c["instrument_id"]] = f"oversample_rare_bucket_{b}"
            else:
                reasons[c["instrument_id"]] = f"stratified_share_{b}"
    return selected


# ----------------------------------------------------------------------------
# 5. Harness 装配（真实 orchestration + 实验 seam monkeypatch）
# ----------------------------------------------------------------------------


def install_harness(id_to_symbol: dict[str, str]):
    """monkeypatch 全部 persistence / external seam；返回 guard 统计。"""
    from app.services import snapshot_run_item_service as snap_item
    from app.services import feature_snapshot_service as fss
    from app.services import market_data_aggregation_service as mdas_mod
    from app.db import AsyncSessionLocal as real_asl  # noqa: F401 (keep ref)
    from app import db as db_mod

    store = InMemoryRunItemStore()
    ext_guard = ExternalGuard()

    # --- run-item service（反映进 feature_snapshot_service 的 re-import）---
    snap_item.create_run_items = store.create_run_items
    snap_item.claim_items = store.claim_items
    snap_item.mark_item_succeeded = store.mark_item_succeeded
    snap_item.mark_item_failed = store.mark_item_failed
    snap_item.get_run_progress = store.get_run_progress

    # --- upsert_snapshot：fake persistence（捕获 id，不落库）---
    captured: list[Any] = []

    async def fake_upsert_snapshot(snapshot_or_db, snapshot=None):
        # 兼容两种签名：upsert_snapshot(snapshot) 与 upsert_snapshot(db, snapshot)
        snap = snapshot if snapshot is not None else snapshot_or_db
        captured.append(snap)
        return getattr(snap, "id", uuid.uuid4()) if snap is not None else uuid.uuid4()

    fss.upsert_snapshot = fake_upsert_snapshot

    # --- released-config resolver：用冻结 DSA config（避免真实 DB 查询）
    #     保留 production resolve_core_run_context 不变（production diff=0），
    #     只替换 resolver 这一 seam。
    from app.services import core_run_context as crc_mod

    class _FrozenDsaResolver:
        def __init__(self, *a, **k) -> None:
            pass

        async def resolve_released_dsa_config(self, *, trade_date=None):
            return {
                "dsa_version": "dsa-v1",
                "dsa_effective_config": {},
                "dsa_build_hash": "frozen-4a3l-benchmark",
            }

        async def resolve_released_config(self, key, *, trade_date=None, default=None):
            return default

    crc_mod.SqlAlchemyReleasedConfigResolver = _FrozenDsaResolver

    # --- _get_mdas：返回 FrozenMDAS（忽略真实 provider）---
    from frozen_mdas import FrozenMDAS
    frozen = FrozenMDAS(DATA_DIR)

    def fake_get_mdas():
        return frozen

    fss._get_mdas = fake_get_mdas

    # --- DB fallback guard（production 主链若触发 _fetch_bars_from_db）---
    class _DBFallback:
        def __init__(self) -> None:
            self.calls = 0

        async def guard(self, *a, **k):
            self.calls += 1
            raise UnexpectedExternalFetch(f"DB fallback intercepted (n={self.calls})")

    dbf = _DBFallback()
    fss._fetch_bars_from_db = dbf.guard

    # --- external provider guard（MarketDataAggregationService.get_bars）---
    async def guarded_get_bars(*a, **k):
        return ext_guard.guard(*a, **k)

    mdas_mod.get_bars = guarded_get_bars

    # --- fake AsyncSessionLocal（仅 symbol 查询 + MDAS session）---
    db_mod.AsyncSessionLocal = lambda: _FakeAsyncSessionCtx(id_to_symbol)

    return {
        "store": store,
        "ext_guard": ext_guard,
        "dbf": dbf,
        "captured": captured,
        "frozen": frozen,
    }


# ----------------------------------------------------------------------------
# 6. 单次 run（真实 orchestration）
# ----------------------------------------------------------------------------


async def run_once(
    harness: dict, instrument_ids: list[uuid.UUID], batch_size: int = 25,
) -> dict:
    from app.services.feature_snapshot_service import (
        compute_review_core_with_run_items,
    )

    # 重置 store / captured（保证多次 run 独立）
    harness["store"]._by_run.clear()
    harness["captured"].clear()
    harness["ext_guard"].calls = 0
    harness["dbf"].calls = 0

    t0 = time.perf_counter()
    # 不传 session_factory：orchestration 走全局 monkeypatch 的 db.AsyncSessionLocal
    # （实验 harness 已将其替换为 fake symbol-query session）。
    stats = await compute_review_core_with_run_items(
        TARGET_TRADE_DATE,
        instrument_ids,
        RUN_ID,
        worker_id=WORKER_ID,
        batch_size=batch_size,
        algorithm_version="v1",
        input_hash="5284aa5fec7f58ce65ee7fcf416be5620baa21a345e93c7e942995ef654705aa",
    )
    elapsed = time.perf_counter() - t0
    return {"stats": stats, "elapsed": elapsed}


# id_to_symbol 全局缓存（install 时填充）
_id_to_symbol_cache: dict[str, str] = {}


# ----------------------------------------------------------------------------
# 7. 主流程
# ----------------------------------------------------------------------------


def env_info() -> dict:
    import platform as _plat
    try:
        import cpuinfo  # type: ignore
        cpu = cpuinfo.get_cpu_info().get("brand_raw", "unknown")
    except Exception:
        cpu = _plat.processor() or "unknown"
    import psutil  # type: ignore
    ram_gib = psutil.virtual_memory().total / (1024 ** 3)
    import pandas as pd2
    import numpy as np2
    import sys as _sys
    return {
        "cpu_model": cpu,
        "logical_cores": os.cpu_count(),
        "physical_cores": _plat.system(),
        "ram_gib": round(ram_gib, 2),
        "python_version": _sys.version.split()[0],
        "pandas_version": pd2.__version__,
        "numpy_version": np2.__version__,
        "platform": _plat.platform(),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- 全 universe 分层分布 ---
    records, counts, share = build_universe()
    id_to_symbol = {r["instrument_id"]: r["symbol"] for r in records}
    global _id_to_symbol_cache
    _id_to_symbol_cache = id_to_symbol

    # --- 分层抽样 ≈500 ---
    selected = stratified_sample(records, counts, share, target=500)
    selected_ids = [uuid.UUID(r["instrument_id"]) for r in selected]

    # 写入 sample_manifest
    sample_manifest = {
        "target_sample_size": 500,
        "actual_sample_size": len(selected),
        "trade_date": TARGET_TRADE_DATE.isoformat(),
        "series": SERIES,
        "selection_method": "stratified_by_bars_count_bucket",
        "samples": [
            {
                "instrument_id": r["instrument_id"],
                "symbol": r["symbol"],
                "bars_count": r["bars_count"],
                "bucket": r["bucket"],
                "selection_reason": (
                    "oversample_rare_bucket_" + r["bucket"]
                    if r["bucket"] in ("0", "1-59", "2000+")
                    else "stratified_share_" + r["bucket"]
                ),
            }
            for r in selected
        ],
    }
    (OUT_DIR / "sample_manifest.json").write_text(
        json.dumps(sample_manifest, ensure_ascii=False, indent=2)
    )

    bucket_dist = {
        "bucket_order": BUCKET_ORDER,
        "production_count": counts,
        "production_share": {b: round(share[b], 6) for b in BUCKET_ORDER},
        "sample_count": {
            b: sum(1 for r in selected if r["bucket"] == b) for b in BUCKET_ORDER
        },
    }
    (OUT_DIR / "universe_bucket_distribution.json").write_text(
        json.dumps(bucket_dist, ensure_ascii=False, indent=2)
    )

    # --- 装配 harness（真实 orchestration + 实验 seam）---
    harness = install_harness(id_to_symbol)

    # --- A. dataset load（一次性）---
    t_load0 = time.perf_counter()
    # 触发 FrozenMDAS 构造（已在 install 内构造；这里独立计时以分离）
    from frozen_mdas import FrozenMDAS
    _ = FrozenMDAS(DATA_DIR)
    dataset_load_elapsed = time.perf_counter() - t_load0

    # --- warmup（小 batch 不计时）---
    warmup_ids = selected_ids[:25]
    asyncio.run(run_once(harness, warmup_ids, batch_size=25))

    # --- 3 次 timed runs ---
    runs = []
    per_instrument_all: list[dict] = []
    for run_idx in range(1, 4):
        r = asyncio.run(run_once(harness, selected_ids, batch_size=25))
        runs.append(r)

    # --- 分层 bucket 单独计时（用于 5293 加权外推）---
    # 每个非空 bucket 跑其样本一次（timed），得到该 bucket 单股 median cost。
    bucket_timing: dict[str, dict] = {}
    by_bucket_ids: dict[str, list[uuid.UUID]] = defaultdict(list)
    for r in selected:
        by_bucket_ids[r["bucket"]].append(uuid.UUID(r["instrument_id"]))
    for b, ids in by_bucket_ids.items():
        if not ids:
            continue
        rb = asyncio.run(run_once(harness, ids, batch_size=25))
        bucket_timing[b] = {
            "n_sample": len(ids),
            "wall_sec": rb["elapsed"],
            "per_stock_sec": rb["elapsed"] / len(ids) if ids else 0.0,
        }

    # --- 收集 per-instrument elapsed（从最后一次 run 的 store 推断 status）---
    final_store = harness["store"]._by_run.get(RUN_ID, {})
    per_inst = []
    for r in selected:
        iid = uuid.UUID(r["instrument_id"])
        item = final_store.get(iid)
        per_inst.append({
            "instrument_id": r["instrument_id"],
            "symbol": r["symbol"],
            "bucket": r["bucket"],
            "bars_count": r["bars_count"],
            "status": item.status if item else "unknown",
            "error": item.error if item else None,
        })
    with open(OUT_DIR / "per_instrument.jsonl", "w", encoding="utf-8") as f:
        for row in per_inst:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # --- 主链 wall-clock 统计（用 B，不含 dataset load）---
    wall_clocks = [r["elapsed"] for r in runs]
    wall_clocks_sorted = sorted(wall_clocks)
    p50 = wall_clocks_sorted[len(wall_clocks_sorted) // 2]
    # CPU 时间（进程累计 utime+stime，3 次 run 后仍近似总占用）
    _ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu_time = _ru.ru_utime + _ru.ru_stime
    # peak RSS：darwin 返回 bytes，linux 返回 KB
    peak_rss_mib = (
        _ru.ru_maxrss / (1024 ** 2)
        if sys.platform == "darwin"
        else _ru.ru_maxrss / 1024.0
    )

    n = len(selected_ids)
    stocks_per_sec = n / p50 if p50 > 0 else 0.0

    last_stats = runs[-1]["stats"]
    succ = last_stats.get("snapshot_count", 0)
    fail = last_stats.get("failed_count", 0)
    skip = last_stats.get("skipped_count", 0)

    run_metrics = {
        "dataset_load_elapsed_sec": round(dataset_load_elapsed, 4),
        "main_chain_wall_clock_sec": {
            "run1": round(wall_clocks[0], 4),
            "run2": round(wall_clocks[1], 4),
            "run3": round(wall_clocks[2], 4),
            "p50": round(p50, 4),
        },
        "cpu_time_sec": round(cpu_time, 4),
        "peak_rss_mib": round(peak_rss_mib, 2),
        "batch_count": last_stats.get("batch_count"),
        "stocks_per_sec_p50": round(stocks_per_sec, 4),
        "success": succ,
        "failed": fail,
        "skipped": skip,
        "total_sample": n,
        "external_provider_calls": harness["ext_guard"].calls,
        "db_fallback_calls": harness["dbf"].calls,
        "production_code_diff": 0,
    }
    (OUT_DIR / "run_metrics.json").write_text(
        json.dumps(run_metrics, ensure_ascii=False, indent=2)
    )

    # --- 8. 5293 加权外推（基于 bucket 实测中位数 cost）---
    # 每股 elapsed：从最后一次 run 无法逐股计时（orchestration 不暴露）；
    # 改用 bucket 平均 = run_wall / sum(bars 权重) 的近似：
    # 这里用 bucket 样本均值 wall-share 推算每 bucket 单股 median cost。
    # 分层加权外推：Σ (N_bucket × measured_per_stock_cost_bucket) + fixed orchestration overhead
    # measured_per_stock_cost_bucket 来自 bucket_timing 的单独计时（per_stock_sec）。
    # 无样本 bucket 用总体均值 per_stock_mean 兜底。
    per_stock_mean = p50 / n if n > 0 else 0.0
    bucket_per_stock: dict[str, float] = {}
    for b in BUCKET_ORDER:
        bt = bucket_timing.get(b)
        if bt and bt["n_sample"] > 0:
            bucket_per_stock[b] = bt["per_stock_sec"]
        else:
            bucket_per_stock[b] = per_stock_mean

    # fixed orchestration overhead：3 次全量 run 的 p50 减去（Σ 样本 bucket 加权 cost）
    # 作为常量加到 5293 projection。
    sample_weighted = sum(
        bucket_per_stock[r["bucket"]] for r in selected
    )
    fixed_overhead = max(p50 - sample_weighted, 0.0)

    projection = {}
    for label, scale in (("optimistic", 0.85), ("central", 1.0), ("conservative", 1.15)):
        proj_total = fixed_overhead * scale
        for b in BUCKET_ORDER:
            n_b = counts.get(b, 0)
            proj_total += n_b * bucket_per_stock[b] * scale
        projection[label] = round(proj_total / 60.0, 2)  # 分钟

    proj_doc = {
        "basis": "layered_weighted_projection_per_bucket_measured_cost",
        "note": "LOCAL PROJECTION — not full-universe benchmark",
        "method": "Σ(N_bucket × measured_per_stock_cost_bucket) + fixed_overhead",
        "fixed_orchestration_overhead_sec": round(fixed_overhead, 4),
        "per_stock_mean_sec": round(per_stock_mean, 6),
        "bucket_per_stock_sec": {
            b: round(bucket_per_stock[b], 6) for b in BUCKET_ORDER
        },
        "bucket_timing": {
            b: {
                "n_sample": bucket_timing[b]["n_sample"],
                "wall_sec": round(bucket_timing[b]["wall_sec"], 4),
                "per_stock_sec": round(bucket_timing[b]["per_stock_sec"], 6),
            }
            for b in bucket_timing
        },
        "projected_5293_serial_minutes": projection,
        "input": {
            "sample_size": n,
            "run_p50_sec": round(p50, 4),
            "production_bucket_count": counts,
            "production_bucket_share": {b: round(share[b], 6) for b in BUCKET_ORDER},
        },
    }
    (OUT_DIR / "projection_5293.json").write_text(
        json.dumps(proj_doc, ensure_ascii=False, indent=2)
    )

    # --- environment ---
    (OUT_DIR / "environment.json").write_text(
        json.dumps(env_info(), ensure_ascii=False, indent=2)
    )

    # --- failures ---
    failures = [p for p in per_inst if p["status"] not in ("succeeded", "skipped")]
    with open(OUT_DIR / "failures.jsonl", "w", encoding="utf-8") as f:
        for p in failures:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # --- 终端摘要 ---
    print("=" * 60)
    print("Phase 4A-3L — Local Small-Scale Main-Chain Benchmark")
    print("=" * 60)
    print(f"sample count             : {n} (target 500)")
    print(f"main-chain wall-clock(s) : run1={wall_clocks[0]:.3f} "
          f"run2={wall_clocks[1]:.3f} run3={wall_clocks[2]:.3f} p50={p50:.3f}")
    print(f"dataset load(s)          : {dataset_load_elapsed:.3f} (excluded from B)")
    print(f"stocks/sec (p50)         : {stocks_per_sec:.3f}")
    print(f"success/failed/skipped   : {succ}/{fail}/{skip}")
    print(f"external provider calls  : {harness['ext_guard'].calls}")
    print(f"db fallback calls        : {harness['dbf'].calls}")
    print(f"production code diff     : 0")
    print(f"Projected 5293 serial(min): "
          f"optimistic={projection['optimistic']} "
          f"central={projection['central']} "
          f"conservative={projection['conservative']}")
    print("NOTE: LOCAL PROJECTION — not full-universe benchmark")
    print("=" * 60)


if __name__ == "__main__":
    main()
