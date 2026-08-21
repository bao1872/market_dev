#!/usr/bin/env python3
"""Phase 3.4A-0 — Dataset + Reproducibility：构建固定 sample_manifest。

只读消费 frozen dataset（review-source-c5c686e-v1）的 parquet，不连远程 DB、
不建新数据集/验证库、不做 PG migration / SQL 实验。PRODUCTION_CODE_DIFF = ZERO。

产出（输出到 experiments/duplicate_compute_audit/output/3.4A-0/）：
- sample_manifest.jsonl：每行一个 instrument，含
  instrument_id / symbol / name / market / bars_count / date range /
  adj_factor_changed / market-board / selection_reason / core_eligible /
  smc_freshness_eligible / bb_extra_eligible
- manifest_meta.json：可复现元信息
  dataset_sha / dataset version / audit_code_sha / experiment_script_sha / checksum

Eligibility 定义（与生产 call-count 对齐）：
- core_eligible        = 存在于 SFS snapshot（stock_feature_snapshots_asof）且 ≥1 bar
- smc_freshness_eligible = bars_count >= 250（structural SMC freshness 触发门槛）
- bb_extra_eligible    = bars_count >= 21（_extract_extra_fields Bollinger #2 门槛）

Usage:
    python build_manifest.py --count 100 [--seed 20260821]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

# --- 路径与常量 ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1"
)
PARQUET_DIR = DATASET_DIR / "parquet"
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4A-0"

SMC_FRESHNESS_MIN_BARS = 250  # structural SMC freshness 触发门槛
BB_EXTRA_MIN_BARS = 21  # _extract_extra_fields Bollinger #2 门槛（_BB_WIN+1）
BOUNDARY_MIN_BARS = 60  # boundary sample 下界（60–249 bars 验证 eligibility 行为）
BOUNDARY_MAX_BARS = 249

_MARKET_STRATA = {"SZ", "SH", "BJ"}


def _read_parquet(name: str) -> pd.DataFrame:
    return pd.read_parquet(PARQUET_DIR / f"{name}.parquet")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
    )


def _load_dataset_meta() -> dict[str, Any]:
    with open(DATASET_DIR / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def _build_instrument_features(
    bars: pd.DataFrame,
    instruments: pd.DataFrame,
    boards: pd.DataFrame,
    memberships: pd.DataFrame,
) -> pd.DataFrame:
    """构造 per-instrument 特征表（以 bars 存在的 instrument 为轴）。"""
    # per-instrument bars 统计
    grp = bars.groupby("instrument_id")
    stats = pd.DataFrame(
        {
            "bars_count": grp["trade_date"].count(),
            "min_trade_date": grp["trade_date"].min(),
            "max_trade_date": grp["trade_date"].max(),
        }
    )
    # adj_factor 是否变化（nunique > 1）
    adj_nunique = (
        pd.to_numeric(bars["adj_factor"], errors="coerce")
        .groupby(bars["instrument_id"])
        .nunique()
    )
    stats["adj_factor_changed"] = adj_nunique.reindex(stats.index) > 1

    # instrument 元信息（market / symbol / name）
    inst = instruments.set_index("id")[["symbol", "name", "market", "status"]]
    features = stats.join(inst)

    # 板块信息：active industry 最具体层级（L3>L2>L1）+ active concept 数量
    active_boards = boards[boards["is_active"]]
    industry = active_boards[active_boards["type"] == "industry"]
    concept = active_boards[active_boards["type"] == "concept"]
    lvl_rank = {"L1": 1, "L2": 2, "L3": 3}
    industry = industry.assign(level_rank=industry["hierarchy_level"].map(lvl_rank))
    industry = industry.sort_values(["level_rank"], ascending=False)
    industry_names = (
        industry.set_index("id")[["name", "hierarchy_level"]]
        .groupby(level=0)
        .agg(lambda s: s.iloc[0])
    )
    concept_ids = set(concept["id"])

    memb = memberships.merge(
        active_boards[["id", "name", "type", "hierarchy_level"]],
        left_on="board_id",
        right_on="id",
        how="inner",
    )

    def _board_summary(iid: str) -> tuple[str, int]:
        rows = memb[memb["instrument_id"] == iid]
        ind_rows = rows[rows["type"] == "industry"]
        if not ind_rows.empty:
            best = ind_rows.sort_values(
                "hierarchy_level", key=lambda s: s.map(lvl_rank), ascending=False
            ).iloc[0]
            industry_name = f"{best['hierarchy_level']}:{best['name']}"
        else:
            industry_name = None
        n_concept = int(rows["board_id"].isin(concept_ids).sum())
        return industry_name, n_concept

    industry_names_l = []
    n_concept_l = []
    for iid in features.index:
        iname, nc = _board_summary(iid)
        industry_names_l.append(iname)
        n_concept_l.append(nc)
    features["industry_board"] = industry_names_l
    features["n_concept_boards"] = n_concept_l
    return features


def _sample_deterministic(
    pool: pd.DataFrame, n: int, rng: random.Random
) -> pd.DataFrame:
    if len(pool) <= n:
        return pool
    idx = list(pool.index)
    rng.shuffle(idx)
    return pool.loc[idx[:n]]


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4A-0 build sample_manifest")
    ap.add_argument("--count", type=int, default=100, help="主 performance sample 数量（全部 ≥250）")
    ap.add_argument("--seed", type=int, default=20260821, help="确定性采样种子")
    args = ap.parse_args()

    # 只读加载 frozen dataset
    bars = _read_parquet("bars_daily")
    instruments = _read_parquet("instruments")
    boards = _read_parquet("boards")
    memberships = _read_parquet("board_memberships_current_snapshot")
    snapshots = _read_parquet("stock_feature_snapshots_asof")
    snap_ids = set(snapshots["instrument_id"].unique())

    features = _build_instrument_features(bars, instruments, boards, memberships)

    # eligibility
    features["core_eligible"] = features.index.isin(snap_ids)
    features["smc_freshness_eligible"] = features["bars_count"] >= SMC_FRESHNESS_MIN_BARS
    features["bb_extra_eligible"] = features["bars_count"] >= BB_EXTRA_MIN_BARS

    # universe 汇总（用于 DoD 证据）
    univ = features[features["core_eligible"]]
    universe_summary = {
        "snapshot_instruments": len(snap_ids),
        "core_eligible_with_bars": int(univ["core_eligible"].sum()),
        "bars_ge_250": int((univ["bars_count"] >= SMC_FRESHNESS_MIN_BARS).sum()),
        "bars_ge_300": int((univ["bars_count"] >= 300).sum()),
        "bars_ge_500": int((univ["bars_count"] >= 500).sum()),
        "bars_60_249": int(
            (
                (univ["bars_count"] >= BOUNDARY_MIN_BARS)
                & (univ["bars_count"] <= BOUNDARY_MAX_BARS)
            ).sum()
        ),
        "bars_lt_60": int((univ["bars_count"] < 60).sum()),
        "adj_factor_changed": int(univ["adj_factor_changed"].sum()),
        "market_mix": univ["market"].value_counts().to_dict(),
        "bars_count_min": int(univ["bars_count"].min()),
        "bars_count_p50": float(univ["bars_count"].median()),
        "bars_count_max": int(univ["bars_count"].max()),
    }

    # 主 sample：core_eligible + bars >= 250，按 market 分层确定性采样
    main_pool = univ[univ["smc_freshness_eligible"]]
    rng = random.Random(args.seed)
    main_rows: list[pd.DataFrame] = []
    for market in _MARKET_STRATA:
        sub = main_pool[main_pool["market"] == market]
        target = max(1, round(args.count * len(sub) / len(main_pool)))
        main_rows.append(_sample_deterministic(sub, target, rng))
    main_sample = pd.concat(main_rows)
    # 保证总数为 args.count（分层取整可能多/少 1）
    if len(main_sample) < args.count:
        extra = _sample_deterministic(
            main_pool[~main_pool.index.isin(main_sample.index)],
            args.count - len(main_sample),
            rng,
        )
        main_sample = pd.concat([main_sample, extra])
    elif len(main_sample) > args.count:
        idx = list(main_sample.index)
        rng.shuffle(idx)
        main_sample = main_sample.loc[idx[: args.count]]
    main_sample = main_sample.copy()
    main_sample["selection_reason"] = "main_ge250"

    # boundary sample：60–249 bars（SMC#2 不触发），确定性取 ≤5 只
    boundary_pool = univ[
        (univ["bars_count"] >= BOUNDARY_MIN_BARS)
        & (univ["bars_count"] <= BOUNDARY_MAX_BARS)
    ]
    boundary_sample = _sample_deterministic(boundary_pool, 5, rng).copy()
    boundary_sample["selection_reason"] = "boundary_60_249"

    samples = pd.concat([main_sample, boundary_sample])
    samples = samples.sort_index().reset_index()

    # 可复现元信息
    ds_meta = _load_dataset_meta()
    audit_code_sha = _git_head()
    script_sha = _sha256_file(Path(__file__).resolve())
    derived_shas = {
        k: v["sha256"]
        for k, v in ds_meta.get("derived_files", {}).items()
        if v.get("sha256")
    }
    meta = {
        "phase": "3.4A-0",
        "purpose": "Dataset + Reproducibility: fixed sample_manifest",
        "dataset_version": ds_meta.get("dataset_dir_name"),
        "dataset_id": ds_meta.get("dataset_id"),
        "dataset_sha": derived_shas,  # 各 parquet 的 sha256（来自 dataset manifest）
        "dataset_capture_git_sha": ds_meta.get("capture_git_sha"),
        "dataset_base_dev_sha": ds_meta.get("base_dev_sha"),
        "audit_code_sha": audit_code_sha,
        "experiment_script_sha": script_sha,
        "sampling": {
            "seed": args.seed,
            "count": args.count,
            "main_pool_ge250": int(len(main_pool)),
            "main_sample_count": int(len(main_sample)),
            "boundary_sample_count": int(len(boundary_sample)),
            "eligibility": {
                "smc_freshness_min_bars": SMC_FRESHNESS_MIN_BARS,
                "bb_extra_min_bars": BB_EXTRA_MIN_BARS,
                "boundary_range": [BOUNDARY_MIN_BARS, BOUNDARY_MAX_BARS],
            },
        },
        "universe_summary": universe_summary,
        "generated_at_utc": pd.Timestamp.now("UTC").isoformat(),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # samples → jsonl（每行一个 instrument）
    cols = [
        "instrument_id",
        "symbol",
        "name",
        "market",
        "bars_count",
        "min_trade_date",
        "max_trade_date",
        "adj_factor_changed",
        "industry_board",
        "n_concept_boards",
        "selection_reason",
        "core_eligible",
        "smc_freshness_eligible",
        "bb_extra_eligible",
    ]
    manifest_path = OUTPUT_DIR / "sample_manifest.jsonl"
    with open(manifest_path, "w", encoding="utf-8") as f:
        for _, row in samples.iterrows():
            rec = {c: (None if pd.isna(row[c]) else row[c]) for c in cols}
            # 原生 bool/None 序列化
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    meta_path = OUTPUT_DIR / "manifest_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print(f"manifest: {manifest_path}")
    print(f"meta:     {meta_path}")
    print(f"main sample: {len(main_sample)}  (all ge250, core_eligible)")
    print(f"  market mix: {main_sample['market'].value_counts().to_dict()}")
    print(f"boundary sample: {len(boundary_sample)}  (60-249 bars)")
    print(f"audit_code_sha: {audit_code_sha}")
    print(f"experiment_script_sha: {script_sha}")
    print("universe_summary:")
    print(json.dumps(universe_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
