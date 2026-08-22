"""Phase 4A-2 — FrozenMDAS Exact Parity Gate。

完全离线（REMOTE_DB_ACCESS=FALSE, NETWORK=FALSE, PRODUCTION_CODE_DIFF=ZERO）。

数据源唯一：backend/.perfdata/afterclose/afterclose-20260817-v1/

Part A: FrozenMDAS（实验级，复用 production _build_daily_aggregation）
Part B: Offline Replay Policy（allow_backfill=False, expected=target_trade_date）
Part C: 105 个确定性样本（100 normal + 5 boundary）
Part D: MDAS exact parity（reference vs candidate，bars + metadata exact）
Part E: Full Review-Core Snapshot parity（每只股票两个独立 CoreRunContext，parameter_hash 相同）
Part F: 禁止 fallback 偷跑（_fetch_bars_from_db 计入 0）

输出：output/4A-2/{sample_manifest,mdas_parity,snapshot_parity,failures.jsonl}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from frozen_mdas import FrozenMDAS

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
DATA_DIR = (
    BACKEND_ROOT
    / ".perfdata"
    / "afterclose"
    / "afterclose-20260817-v1"
)
OUT_DIR = REPO_ROOT / "experiments" / "duplicate_compute_audit" / "output" / "4A-2"

TARGET_TRADE_DATE = date(2026, 8, 17)
ADJUSTMENT_AS_OF = date(2026, 8, 17)
SNAPSHOT_RUN_ID = "2b7c5877-7d36-4396-84c3-7186dc911073"
RUN_CALCULATED_AT = "2026-08-17T15:00:00+08:00"
PARAMETER_HASH = "5284aa5fec7f58ce65ee7fcf416be5620baa21a345e93c7e942995ef654705aa"

_BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]

# runtime-only 字段（受时钟/cache 影响，不纳入 business contract 比较）
_RUNTIME_ONLY_FIELDS = {"as_of", "freshness_seconds", "cache_hit"}

# 互斥计数器（Part F）
_DB_CALLS = 0
_EXTERNAL_CALLS = 0


def _monkeypatch_fallbacks():
    """Part F：禁止 _fetch_bars_from_db 偷跑。若被调用 → 计数 + 抛错。"""
    import app.services.feature_snapshot_service as fss

    _orig = fss._fetch_bars_from_db

    async def _patched(session, instrument_id, trade_date, *args, **kwargs):
        global _DB_CALLS
        _DB_CALLS += 1
        raise RuntimeError(
            f"UNEXPECTED DB FETCH in 4A-2 offline replay: {instrument_id}"
        )

    fss._fetch_bars_from_db = _patched
    return _orig


# ---------------------------------------------------------------------------
# 独立 reference df 构造（不调用 FrozenMDAS，确保两条路径独立）
# ---------------------------------------------------------------------------

def _build_reference_dfs(inst_id: str):
    bars = pd.read_parquet(DATA_DIR / "bars_daily_raw.parquet")
    factors = pd.read_parquet(DATA_DIR / "adj_factors.parquet")
    b = bars[bars["instrument_id"] == inst_id].sort_values("trade_date").copy()
    f = factors[factors["instrument_id"] == inst_id].sort_values("trade_date").copy()
    daily_df = b.copy()
    daily_df["trade_date"] = pd.to_datetime(daily_df["trade_date"])
    daily_df = daily_df.set_index("trade_date").reindex(columns=_BAR_COLUMNS)
    for c in _BAR_COLUMNS:
        daily_df[c] = pd.to_numeric(daily_df[c], errors="coerce")
    factor_df = f.copy()
    factor_df["trade_date"] = pd.to_datetime(factor_df["trade_date"])
    factor_df["adj_factor"] = pd.to_numeric(factor_df["adj_factor"], errors="coerce")
    factor_df = factor_df[["trade_date", "adj_factor"]]
    return daily_df, factor_df


# ---------------------------------------------------------------------------
# Part C — 样本选取
# ---------------------------------------------------------------------------

def _select_samples() -> list[dict]:
    elig = json.loads(
        (DATA_DIR / "eligible_universe.json").read_text(encoding="utf-8")
    )
    ids = elig["sorted_ids"]
    # 从 bars 数据计算每只 bars_count
    bars = pd.read_parquet(DATA_DIR / "bars_daily_raw.parquet")
    counts = bars.groupby("instrument_id").size().to_dict()

    inst_meta = pd.read_parquet(DATA_DIR / "instruments.parquet")
    sym_map = dict(zip(inst_meta["id"], inst_meta["symbol"]))
    sym_to_id = dict(zip(inst_meta["symbol"], inst_meta["id"]))

    samples: list[dict] = []

    # 100 normal：historical universe ∩ bars_count >= 250，按 bars_count 排序等距抽样
    eligible_normal = [i for i in ids if (counts.get(i, 0)) >= 250]
    eligible_normal.sort(key=lambda i: counts.get(i, 0))
    n = 100
    step = len(eligible_normal) / n
    picked = set()
    for k in range(n):
        idx = min(int(k * step), len(eligible_normal) - 1)
        iid = eligible_normal[idx]
        if iid in picked:
            # 向后找下一个未选
            for j in range(idx + 1, len(eligible_normal)):
                if eligible_normal[j] not in picked:
                    iid = eligible_normal[j]
                    break
        picked.add(iid)
        samples.append({
            "instrument_id": iid,
            "symbol": sym_map.get(iid),
            "bars_count": counts.get(iid, 0),
            "boundary_reason": "normal",
        })

    # 5 boundary
    non_zero = [(i, counts.get(i, 0)) for i in ids if counts.get(i, 0) > 0]
    non_zero.sort(key=lambda x: x[1])
    min_inst, min_cnt = non_zero[0]
    max_inst, max_cnt = non_zero[-1]
    # 最接近 60 bars
    near60 = min(non_zero, key=lambda x: abs(x[1] - 60))
    # 最接近 250 bars
    near250 = min(non_zero, key=lambda x: abs(x[1] - 250))

    boundaries = [
        ("920305 0 bars", sym_to_id.get("920305", "920305"), 0),
        (f"frozen shortest ({min_cnt} bars)", min_inst, min_cnt),
        (f"closest to 60 bars ({near60[1]})", near60[0], near60[1]),
        (f"closest to 250 bars ({near250[1]})", near250[0], near250[1]),
        (f"longest history ({max_cnt} bars)", max_inst, max_cnt),
    ]
    for reason, iid, cnt in boundaries:
        samples.append({
            "instrument_id": iid,
            "symbol": sym_map.get(iid),
            "bars_count": cnt,
            "boundary_reason": reason,
        })
    return samples


# ---------------------------------------------------------------------------
# 比较工具
# ---------------------------------------------------------------------------

def _compare_bars(a: pd.DataFrame, b: pd.DataFrame) -> list[str]:
    mismatches: list[str] = []
    if list(a.columns) != list(b.columns):
        mismatches.append(f"columns {list(a.columns)} != {list(b.columns)}")
        return mismatches
    if len(a) != len(b):
        mismatches.append(f"len {len(a)} != {len(b)}")
        return mismatches
    if not a.index.equals(b.index):
        mismatches.append("index not equal")
        return mismatches
    for col in a.columns:
        av = a[col]
        bv = b[col]
        if av.dtype != bv.dtype:
            mismatches.append(f"dtype {col}: {av.dtype} != {bv.dtype}")
            continue
        amask = av.isna()
        bmask = bv.isna()
        if not amask.equals(bmask):
            mismatches.append(f"NaN mask {col} differs")
            continue
        if not (av[~amask] == bv[~bmask]).all():
            mismatches.append(f"values {col} differ")
    return mismatches


def _compare_metadata(a, b) -> list[str]:
    mismatches: list[str] = []
    fields = [f for f in a.__dataclass_fields__ if f not in _RUNTIME_ONLY_FIELDS]
    for f in fields:
        av = getattr(a, f)
        bv = getattr(b, f)
        # DataFrame 字段单独比较
        if isinstance(av, pd.DataFrame) or isinstance(bv, pd.DataFrame):
            if not (isinstance(av, pd.DataFrame) and isinstance(bv, pd.DataFrame)):
                mismatches.append(f"{f}: type mismatch")
                continue
            mm = _compare_bars(av, bv)
            for m in mm:
                mismatches.append(f"{f}.{m}")
            continue
        if av != bv:
            mismatches.append(f"{f}: {av!r} != {bv!r}")
    return mismatches


# ---------------------------------------------------------------------------
# Part D — MDAS exact parity
# ---------------------------------------------------------------------------

async def _mdas_parity_for(session, inst_id: str, frozen: "FrozenMDAS"):
    from app.services.market_data_aggregation_service import (
        _build_daily_aggregation,
    )
    from app.core.time import SHANGHAI_TZ

    now = datetime(2026, 8, 17, 15, 0, 0, tzinfo=SHANGHAI_TZ)

    # Reference path：独立构造 daily_df/factor_df + production _build_daily_aggregation
    ref_daily, ref_factor = _build_reference_dfs(inst_id)
    ref_result = await _build_daily_aggregation(
        session, inst_id, ref_daily, ref_factor,
        TARGET_TRADE_DATE, now, timeframe="1d", adj="qfq",
        include_realtime=False, completed_only=True,
        start=None, end=TARGET_TRADE_DATE, limit=None, warmup_bars=0,
        adjustment_as_of=ADJUSTMENT_AS_OF, allow_backfill=False,
    )

    # Candidate path：FrozenMDAS.get_bars_batch
    cand_results = await frozen.get_bars_batch(
        session, [inst_id], timeframe="1d", adj="qfq",
        include_realtime=False, completed_only=True,
        end_date=TARGET_TRADE_DATE, adjustment_as_of=ADJUSTMENT_AS_OF,
    )
    cand_result = cand_results[inst_id]

    bar_mm = _compare_bars(ref_result.bars, cand_result.bars)
    meta_mm = _compare_metadata(ref_result, cand_result)
    return bar_mm + meta_mm


# ---------------------------------------------------------------------------
# Part E — Snapshot parity
# ---------------------------------------------------------------------------

async def _snapshot_parity_for(session, inst_id: str, symbol: str, frozen: "FrozenMDAS"):
    from app.services.feature_snapshot_service import (
        compute_review_core_for_trade_date,
    )
    from app.services.core_run_context import (
        CoreRunContext,
        resolve_core_run_context,
    )
    from app.services.core_run_context import ReleasedConfigResolver

    # 两个独立 CoreRunContext（fresh A / fresh B），parameter_hash 必须相同
    # 使用完整 frozen universe（5293 sorted_ids），与 4A-1R2 expected_core_run_context 同构 → hash=5284aa5f...
    released_cfg = json.loads(
        (DATA_DIR / "released_core_config.json").read_text(encoding="utf-8")
    )
    elig_doc = json.loads(
        (DATA_DIR / "eligible_universe.json").read_text(encoding="utf-8")
    )
    full_universe = elig_doc["sorted_ids"]

    class _LocalResolver(ReleasedConfigResolver):
        async def resolve_released_dsa_config(self, trade_date):
            eff = released_cfg.get("effective_dsa_config", {})
            algo = released_cfg.get("algorithm_versions", {})
            return {
                "dsa_version": algo.get("dsa", released_cfg.get("version")),
                "dsa_build_hash": algo.get("dsa_build_hash", released_cfg.get("build_hash")),
                "dsa_effective_config": eff,
            }

    # Reference context A
    ctx_a = await resolve_core_run_context(
        trade_date=TARGET_TRADE_DATE,
        snapshot_run_id=SNAPSHOT_RUN_ID,
        eligible_instrument_ids=full_universe,
        resolver=_LocalResolver(),
        run_mode="after_close",
        universe_version="v1",
    )
    # Candidate context B（独立实例）
    ctx_b = await resolve_core_run_context(
        trade_date=TARGET_TRADE_DATE,
        snapshot_run_id=SNAPSHOT_RUN_ID,
        eligible_instrument_ids=full_universe,
        resolver=_LocalResolver(),
        run_mode="after_close",
        universe_version="v1",
    )
    assert ctx_a.parameter_hash == ctx_b.parameter_hash, (
        f"parameter_hash A/B mismatch: {ctx_a.parameter_hash} / {ctx_b.parameter_hash}"
    )
    assert ctx_a.parameter_hash == PARAMETER_HASH, (
        f"parameter_hash {ctx_a.parameter_hash} != frozen 5284aa5f..."
    )

    uid = uuid.UUID(inst_id)

    # Reference path：production reference MDAS result → compute_review_core_for_trade_date
    ref_daily, ref_factor = _build_reference_dfs(inst_id)
    from app.services.market_data_aggregation_service import (
        _build_daily_aggregation,
    )
    from app.core.time import SHANGHAI_TZ
    now = datetime(2026, 8, 17, 15, 0, 0, tzinfo=SHANGHAI_TZ)
    ref_mdas = await _build_daily_aggregation(
        session, inst_id, ref_daily, ref_factor,
        TARGET_TRADE_DATE, now, timeframe="1d", adj="qfq",
        include_realtime=False, completed_only=True,
        start=None, end=TARGET_TRADE_DATE, limit=None, warmup_bars=0,
        adjustment_as_of=ADJUSTMENT_AS_OF, allow_backfill=False,
    )
    ref_snap = await compute_review_core_for_trade_date(
        session, uid, TARGET_TRADE_DATE,
        primary_bars=ref_mdas.bars,  # 可能是空 DataFrame（920305）→ 不触发 _fetch_bars_from_db
        primary_source_bar_hash=ref_mdas.source_bar_hash,
        primary_adj_factor_hash=ref_mdas.adj_factor_hash,
        instrument_symbol=symbol,
        source_run_id=uuid.UUID(SNAPSHOT_RUN_ID),
        run_calculated_at=RUN_CALCULATED_AT,
        core_context=ctx_a,
    )

    # Candidate path：FrozenMDAS result → compute_review_core_for_trade_date
    cand_results = await frozen.get_bars_batch(
        session, [inst_id], timeframe="1d", adj="qfq",
        include_realtime=False, completed_only=True,
        end_date=TARGET_TRADE_DATE, adjustment_as_of=ADJUSTMENT_AS_OF,
    )
    cand_mdas = cand_results[inst_id]
    cand_snap = await compute_review_core_for_trade_date(
        session, uid, TARGET_TRADE_DATE,
        primary_bars=cand_mdas.bars,
        primary_source_bar_hash=cand_mdas.source_bar_hash,
        primary_adj_factor_hash=cand_mdas.adj_factor_hash,
        instrument_symbol=symbol,
        source_run_id=uuid.UUID(SNAPSHOT_RUN_ID),
        run_calculated_at=RUN_CALCULATED_AT,
        core_context=ctx_b,
    )

    mismatches = _compare_snapshot(ref_snap, cand_snap)
    return mismatches


# snapshot business payload 中属于运行时墙钟、不纳入 frozen business contract 的字段
_SNAPSHOT_RUNTIME_ONLY = {
    "fp_calculated_at",  # FirstPyramid 计算墙钟，每次调用微秒级不同
    "calculated_at",     # 通用计算墙钟
}


def _strip_runtime(d):
    """递归移除运行时墙钟字段（含嵌套）。"""
    if isinstance(d, dict):
        return {
            k: _strip_runtime(v)
            for k, v in d.items()
            if k not in _SNAPSHOT_RUNTIME_ONLY
        }
    if isinstance(d, list):
        return [_strip_runtime(v) for v in d]
    return d


def _as_jsonable(v):
    """若字段是 JSON 字符串则解析为 dict/list，否则原样返回。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return v


def _compare_snapshot(a, b) -> list[str]:
    mismatches: list[str] = []
    # 比较 business payload 字段
    for field in [
        "structural_payload", "temporal_payload", "summary_payload",
        "source_primary_bar_time", "source_secondary_bar_time",
        "degraded_reasons",
    ]:
        av = _as_jsonable(getattr(a, field, None))
        bv = _as_jsonable(getattr(b, field, None))
        if isinstance(av, dict) and isinstance(bv, dict):
            # 排除运行时墙钟字段（递归）后逐 key 比较
            if json.dumps(_strip_runtime(av), sort_keys=True, default=str) != json.dumps(
                _strip_runtime(bv), sort_keys=True, default=str
            ):
                mismatches.append(f"{field}: JSON payload differs")
        elif av != bv:
            mismatches.append(f"{field}: {av!r} != {bv!r}")
    return mismatches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    from app.services.feature_snapshot_service import (
        compute_review_core_for_trade_date,
    )
    _monkeypatch_fallbacks()

    samples = _select_samples()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sample_manifest.json").write_text(
        json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"selected {len(samples)} samples")

    # 用 dummy session（离线，所有 DB 调用都被 monkeypatch 拦截）
    class _DummySession:
        pass

    session = _DummySession()
    frozen = FrozenMDAS(DATA_DIR)

    mdas_failures = []
    snap_failures = []
    failures_jsonl = []

    for s in samples:
        iid = s["instrument_id"]
        sym = s["symbol"]
        # Part D
        try:
            mdas_mm = await _mdas_parity_for(session, iid, frozen)
        except Exception as e:
            mdas_mm = [f"EXCEPTION: {type(e).__name__}: {e}"]
        if mdas_mm:
            mdas_failures.append(iid)
            for m in mdas_mm:
                failures_jsonl.append({
                    "phase": "MDAS", "instrument_id": iid, "symbol": sym,
                    "mismatch": m,
                })
        # Part E（仅 Part D PASS 后）
        if not mdas_mm:
            try:
                snap_mm = await _snapshot_parity_for(session, iid, sym, frozen)
            except Exception as e:
                snap_mm = [f"EXCEPTION: {type(e).__name__}: {e}"]
            if snap_mm:
                snap_failures.append(iid)
                for m in snap_mm:
                    failures_jsonl.append({
                        "phase": "SNAPSHOT", "instrument_id": iid, "symbol": sym,
                        "mismatch": m,
                    })

    mdas_total = len(samples)
    snap_total = mdas_total - len(mdas_failures)

    mdas_report = {
        "exact": mdas_total - len(mdas_failures),
        "total": mdas_total,
        "failures": mdas_failures,
    }
    snap_report = {
        "exact": snap_total - len(snap_failures),
        "total": snap_total,
        "failures": snap_failures,
    }
    (OUT_DIR / "mdas_parity.json").write_text(
        json.dumps(mdas_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "snapshot_parity.json").write_text(
        json.dumps(snap_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "failures.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in failures_jsonl),
        encoding="utf-8",
    )

    print("\n=== 4A-2 Gate ===")
    print(f"MDAS:      {mdas_report['exact']}/{mdas_report['total']} exact")
    print(f"Snapshot:  {snap_report['exact']}/{snap_report['total']} exact")
    print(f"DB calls:        {_DB_CALLS}  (must be 0)")
    print(f"external calls:  {_EXTERNAL_CALLS}  (must be 0)")
    print(f"mdas failures:   {mdas_failures}")
    print(f"snap failures:   {snap_failures}")

    gate_pass = (
        mdas_report["exact"] == mdas_total
        and snap_report["exact"] == snap_total
        and _DB_CALLS == 0
        and _EXTERNAL_CALLS == 0
    )
    print(f"\nPhase 4A-2: {'PASS' if gate_pass else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
