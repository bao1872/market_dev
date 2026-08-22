#!/usr/bin/env python3
"""Phase 4A-0 — AfterClose Frozen Main-Chain Dataset: Contract Freeze.

只做 contract 调查 + dataset spec 定义，**不连远程 DB、不拉大数据**。

范围（与已批准的 Phase 4A 计划一致）：
- 只覆盖 computing_features 主链：compute_review_core_with_run_items
- 不含 Review/scope/Chip；不加 15m/boards/scope/concept/industry membership
- daily bars 禁止固定 250/400 根：冻结真实生产 MDAS 批读 contract

本脚本输出 dataset spec + 基于既有 frozen 的契约核查证据：
1. 复用 review-source-c5c686e-v1 的 manifest 元数据（eligible universe）
2. 用 _load_bars() 计算 daily 历史长度分布（佐证生产 daily 窗口 >> 400）
3. 冻结 MDAS 批读 contract / 15m 无关性 / released config 来源 / offline harness 隔离点
4. 确定 target_trade_date / sample 建议

约束：PRODUCTION_CODE_DIFF = ZERO；只读消费既有 frozen；不建库不连远程。

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/spec_4a0.py
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from audit_closure import _load_bars

EXPERIMENT_DIR = Path(__file__).resolve().parent
MANIFEST_JSON = (
    Path(__file__).resolve().parents[2]
    / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "manifest.json"
)
SAMPLE_MANIFEST = EXPERIMENT_DIR / "output" / "3.4A-0" / "sample_manifest.jsonl"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "4A-0"

# 真实生产 MDAS 批读 contract（经代码核验，见 4A-0 evidence）
MDAS_DAILY_LOOKBACK_CAL_DAYS = 5000  # _DEFAULT_DAILY_LOOKBACK_DAYS；无 start/limit 时回退值


def main() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. 复用既有 manifest 元数据（eligible universe / target 相关）----
    man = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    universe_rule = man.get("instrument_universe_rule", "")
    scope_rule = man.get("scope_universe_rule", "")
    analysis_asof = man.get("analysis_asof_date")
    bar_lookback_cal = man.get("bar_lookback_calendar_days")

    # ---- 2. daily 历史长度分布（从既有 frozen 全量统计）----
    bars_by_id = _load_bars()
    lens = np.array([len(df) for df in bars_by_id.values()], dtype=np.int64)
    hist = {
        "n_instruments_in_frozen": int(len(lens)),
        "bars_per_instrument": {
            "p50": int(np.percentile(lens, 50)),
            "p90": int(np.percentile(lens, 90)),
            "p99": int(np.percentile(lens, 99)),
            "max": int(lens.max()),
        },
        "n_with_bars_ge_400": int((lens >= 400).sum()),
        "note": (
            "既有 review-source-c5c686e-v1 冻结日线 bar_lookback_calendar_days=400，"
            "故冻结 bars 长度受此上限约束（full-history DSA 无法看到媲美生产 "
            "5000-cal-day contract 的历史长度分布）"
        ),
        "max_observed_daily_bars": int(lens.max()),
    }

    # ---- 3. 确定 target/day 建议：用 analysis_asof 最近完整交易日 ----
    # 真实 eligible universe 由 get_active_a_share_instruments()（status=active + 6位数字 symbol,
    # A股，排除指数/基金/ETF）定义；(既有 frozen review_fact_universe=8272, snapshot_rows=5293)
    target_trade_date = analysis_asof  # Phase 4A-1 需从远程确认目标交易日

    spec = {
        "phase": "4A-0",
        "title": "AfterClose Frozen Main-Chain Dataset - Contract Freeze",
        "audit_code_sha": head,
        "scope": {
            "only": "compute_review_core_with_run_items",
            "excluded": [
                "review / scope aggregation",
                "chip_consensus",
                "refreshing_daily / syncing_boards / checking_coverage / publishing / "
                "auction_anchor / computing_review / enqueue_chip_job",
            ],
        },
        "dataset_def": {
            "root_dir": "backend/.perfdata/afterclose/<dataset_version>/",
            "files": {
                "manifest.json": "dataset 元数据 / hashes / coverage",
                "bars_daily_raw.parquet": "未复权 raw daily bars（覆盖生产查询窗口）",
                "adj_factors.parquet": "PIT adjustment factors（factor effective date <= target_trade_date）",
                "instruments.parquet": "最小字段 id/symbol/listing_date",
                "released_core_config.json": "目标交易日真实 released CoreRunContext 配置",
                "eligible_universe.json": "get_active_a_share_instruments 结果",
                "expected_mdas_contract.json": "FrozenMDAS 输出合同基准",
            },
            "explicitly_excluded_files": [
                "bars_15m.parquet",
                "boards.parquet",
                "board_memberships.parquet",
                "scope_memberships.parquet",
                "concept_memberships.parquet",
                "industry_memberships.parquet",
            ],
        },
        # ---- 经代码核验的关键契约结论 ----
        "verified_contracts": {
            "mdas_daily_lookback": {
                "constant": "_DEFAULT_DAILY_LOOKBACK_DAYS",
                "value_calendar_days": MDAS_DAILY_LOOKBACK_CAL_DAYS,
                "contract": (
                    "compute_review_core_with_run_items 批读调用未传 start_date/limit: "
                    "timeframe=1d, adj=qfq, include_realtime=False, completed_only=True, "
                    "end_date=trade_date, adjustment_as_of=trade_date; "
                    "_resolve_date_range 无 start/limit → end_date - 5000 calendar days"
                ),
                "implication": (
                    "frozen daily bars 必须覆盖 [target_trade_date - 5000 cal days, "
                    "target_trade_date]；不得人为裁成 250/400 bars（此前 "
                    "review-source 用 bar_lookback_calendar_days=400 未覆盖生产历史长度分布，"
                    "full-history DSA #2 可能拿到远多于 400 根）"
                ),
            },
            "no_15m_secondary": {
                "value": True,
                "contract": (
                    "盘后 review core = daily-core only；compute_review_core_with_run_items "
                    "主链只调用 get_bars_batch(timeframe='1d')；15m 属 chip_consensus（deferred "
                    "异步 job，不在主链）；main 链不消费 15m"
                ),
                "implication": "Phase 4A 不拉 15m，不建 bars_15m.parquet",
            },
            "no_boards_scope_membership": {
                "value": True,
                "contract": (
                    "主链直接核心输入仅：eligible instrument ids / id→symbol / daily raw bars / "
                    "PIT adj factors / released CoreRunContext / run-item state；"
                    "scope/concept/industry membership 属 Review/scope aggregation，本阶段不跑"
                ),
                "implication": "不提前冻结 boards / scope / concept / industry membership",
            },
            "session_factory_di_partial": {
                "fully_isolates_db": False,
                "_sf_controlled_points": [
                    "config 解析（1349）",
                    "create_run_items（1359）",
                    "claim_items（1383）",
                    "compute 每股独立事务（1434）",
                    "get_run_progress（1545）",
                ],
                "hard_coded_AsyncSessionLocal_points": [
                    "instrument_id→symbol batch query（1397）",
                    "MDAS get_bars_batch（1414）",
                    "mark_item_failed / first_pyramid failed（1477）",
                    "mark_item_succeeded（1494）",
                    "mark_item_failed / 单股失败（1519）",
                ],
                "contract": (
                    "compute_review_core_with_run_items(session_factory=fake) 只隔离 _sf 控制点"
                    "（config 解析 / run-items / 逐股独立事务 / 最终 coverage），"
                    "不能完整离线：symbol 批查、MDAS 批读与 mark_item_* 仍硬编码 AsyncSessionLocal。"
                    "Phase 4A-2/4A-3 需 harness monkeypatch：app.db.AsyncSessionLocal / "
                    "snapshot_run_item_service.create_run_items/claim_items/mark_item_*"
                    "/get_run_progress / feature_snapshot_service.upsert_snapshot / _get_mdas"
                    "(→FrozenMDAS) / released_config_resolver(→FrozenReleasedConfigResolver)；"
                    "production code diff = 0"
                ),
            },
            "released_config_source": {
                "resolver": "SqlAlchemyReleasedConfigResolver",
                "contract": (
                    "查 dsa_selector released StrategyVersion.manifest.parameters；"
                    "scheduled 模式无 released 时 fail-closed（禁止回退代码常量）；"
                    "resolve_core_run_context 冻结 run_calculated_at/eligible hash/released DSA "
                    "config/market-data contract/adjustment contract/parameter hash/execution contract"
                ),
                "frozen_override": "FrozenReleasedConfigResolver 返回目标交易日冻结的 released_"
                                  "core_config.json",
            },
            "eligible_universe_source": {
                "function": "get_active_a_share_instruments(session)",
                "contract": "status='active' AND symbol ~ r'^\\d{6}$'（A股, 排除指数/基金/ETF）",
                "frozen_ref": {
                    "existing_review_fact_universe": (
                        man.get("source_readiness", {})
                        .get("first_pyramid_history", {})
                        .get("coverage", {})
                        .get("review_fact_universe")
                    ),
                    "existing_snapshot_rows": (
                        man.get("source_readiness", {})
                        .get("first_pyramid_history", {})
                        .get("coverage", {})
                        .get("snapshot_rows")
                    ),
                    "note": "Phase 4A-1 需重新只读导出 get_active_a_share_instruments 的真实 eligible 集",
                },
            },
        },
        "target_and_sample": {
            "target_trade_date": str(target_trade_date),
            "note": (
                "以既有 frozen 最近的已发布交易日为初选；4A-1 从远程只读确认目标交易日与 "
                "5000-cal-day 窗口；目标交易日应选取当日 stock_core/released config 实际发布日"
            ),
            "sample_suggestion": {
                "parity_gate_4A2": "100 normal + 5 boundary（与 Phase 3 同口径）",
                "wall_clock_4A3": "完整 eligible universe（生产 batching/claim/compute 总 wall-clock）",
            },
        },
        "evidence": {
            "universe_rule": universe_rule,
            "scope_rule": scope_rule,
            "existing_frozen_lookback_calendar_days": bar_lookback_cal,
            "daily_bars_length_histogram": hist,
            "note_baseline_vs_4A3": (
                "Phase 3 serial projection 18-23min 基于 ~415 bars 外推；4A 使用真实生产 "
                "daily-history contract 后 full-history DSA 可能拿到远多于 415 根，真实结果"
                "可能明显更慢，这反映了此前 frozen 未覆盖生产历史长度分布"
            ),
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "dataset_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 4A-0 Contract Freeze ===")
    print(f"audit_code_sha={head}")
    print(f"target_trade_date(初选)={target_trade_date}")
    print(f"existing frozen lookback={bar_lookback_cal} cal days; production contract=5000 cal days")
    print(f"daily bars len p50/p90/p99/max = "
          f"{hist['bars_per_instrument']['p50']}/{hist['bars_per_instrument']['p90']}/"
          f"{hist['bars_per_instrument']['p99']}/{hist['bars_per_instrument']['max']}")
    print(f"n_instruments_in_frozen={hist['n_instruments_in_frozen']}")
    print("contracts verified: "
          "mdas_daily_lookback=5000 | no_15m=True | no_boards=True | "
          "session_factory_partial=True | released_config=SqlAlchemyReleasedConfigResolver")
    print("dataset spec written:", OUTPUT_DIR / "dataset_spec.json")


if __name__ == "__main__":
    main()