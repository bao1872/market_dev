#!/usr/bin/env python3
"""Phase 4A-0 — AfterClose Frozen Main-Chain Dataset: Contract Freeze.

只做 contract 调查 + dataset spec 定义，**不连远程 DB、不拉大数据**。

范围（与已批准的 Phase 4A 计划一致）：
- 只覆盖 computing_features 主链：compute_review_core_with_run_items
- 不含 Review/scope/Chip；不加 15m/boards/scope/concept/industry membership
- daily bars 禁止固定 250/400 根：冻结真实生产 MDAS 批读 contract

本脚本输出 dataset spec + 基于既有 frozen 的契约核查证据：
1. 从既有 frozen parquet 实测真实历史覆盖度（earliest/latest/span/bars 分布），
   不依赖 manifest.bar_lookback_calendar_days=400（该字段与真实 parquet 跨度不一致，
   不建立 "400 日历日 → 415 bars" 因果）
2. eligible universe 唯一合同 = get_active_a_share_instruments()；旧 manifest 的
   review_fact_universe/snapshot_rows 仅标为遗留元数据，非 Phase 4A eligible 证据
3. 冻结 MDAS 批读 contract / 15m 无关性 / released config 来源 / offline harness 隔离点
4. 正式冻结 target_trade_date=2026-08-17 / sample 建议

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

    # ---- 2. 从既有 frozen parquet 实测真实历史覆盖度（不依赖 manifest 字段）----
    # bars_daily.parquet 实际覆盖 2024-12-02 → 2026-08-17：623 日历日 / 415 交易日。
    # 注意：manifest.bar_lookback_calendar_days=400 与真实 parquet 跨度（623 日历日）不一致，
    # 该字段不是 parquet coverage 的权威定义，仅作为遗留元数据记录，不建立 "400→415" 因果。
    bars_by_id = _load_bars()
    lens = np.array([len(df) for df in bars_by_id.values()], dtype=np.int64)
    min_idx = min(df.index.min() for df in bars_by_id.values())
    max_idx = max(df.index.max() for df in bars_by_id.values())
    global_span_days = int((max_idx - min_idx).days)
    med = int(np.median(lens))
    rep_id = min(
        bars_by_id.keys(),
        key=lambda k: abs(len(bars_by_id[k]) - med),
    )
    rep_df = bars_by_id[rep_id]
    frozen_coverage = {
        "n_instruments_in_frozen": int(len(lens)),
        "actual_global_range": {
            "earliest_trade_date": str(min_idx.date()),
            "latest_trade_date": str(max_idx.date()),
            "calendar_span_days": global_span_days,
            "distinct_trading_days": len(
                set().union(*[set(df.index) for df in bars_by_id.values()])
            ),
        },
        "bars_per_instrument": {
            "min": int(lens.min()),
            "p50": int(np.percentile(lens, 50)),
            "p90": int(np.percentile(lens, 90)),
            "p99": int(np.percentile(lens, 99)),
            "max": int(lens.max()),
        },
        "representative_instrument": {
            "instrument_id": str(rep_id),
            "rows": int(len(rep_df)),
            "first_trade_date": str(rep_df.index.min().date()),
            "last_trade_date": str(rep_df.index.max().date()),
            "bar_calendar_span_days": int((rep_df.index.max() - rep_df.index.min()).days),
        },
        "manifest_bar_lookback_calendar_days": bar_lookback_cal,
        "manifest_field_reliability": (
            "manifest.bar_lookback_calendar_days=400 与真实 parquet 跨度（623 日历日）不一致；"
            "该字段不是 parquet coverage 的权威定义，不建立 '400 日历日 → 415 bars' 因果。"
        ),
        "comparison_to_production": (
            "现有 frozen 实测历史 ≈415 trading bars（~623 日历日）；"
            "production MDAS 无 start/limit 时查询窗口 = 5000 日历日 "
            "（约 3400+ trading bars）。故现有 frozen 不足以代表 production 历史长度分布。"
        ),
    }

    # ---- 3. 冻结 target_trade_date ----
    # TARGET_TRADE_DATE = 2026-08-17（FROZEN）。
    # 4A-1 只能验证该日 remote data / released config 是否存在；不存在则 FAIL CLOSED，
    # 不得自动更换其它日期。换日期 = 4A-0 contract revision + 新 dataset version。
    target_trade_date = analysis_asof  # analysis_asof=2026-08-17
    target_frozen_status = "FROZEN"

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
                    "target_trade_date]；不得人为裁成 250/415 bars。"
                    "现有 review-source frozen 实测 ≈415 trading bars（~623 日历日），"
                    "而 production MDAS 无 start/limit 时查询窗口 = 5000 日历日 "
                    "（≈3400+ trading bars）；故现有 frozen 不足以代表生产历史长度分布，"
                    "full-history DSA #2 可能拿到远多于 415 根"
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
                "rule": "get_active_a_share_instruments(session)",
                "contract": "status='active' AND symbol ~ r'^\\d{6}$'（A股, 排除指数/基金/ETF）",
                "count_and_ids": {
                    "known_now": False,
                    "note": (
                        "Phase 4A-0 不假装已知 exact universe；真实 count / sorted ids / "
                        "universe_hash 由 4A-1 远程只读生成"
                    ),
                },
                "extract_contract_4A1": {
                    "artifact": "eligible_universe.json",
                    "fields": [
                        "target_trade_date",
                        "count",
                        "sorted_ids",
                        "universe_hash",
                    ],
                    "universe_hash": (
                        "deterministic SHA256 over sorted instrument ids；作为 dataset identity"
                    ),
                    "fail_closed": (
                        "查询为空 / 明显异常 / 出现重复 ID → FAIL CLOSED；"
                        "禁止用旧 frozen manifest 的 review_fact_universe(8272)/"
                        "snapshot_rows(5293) 兜底"
                    ),
                },
            },
        },
        "target_and_sample": {
            "target_trade_date": str(target_trade_date),
            "status": target_frozen_status,
            "note": (
                "target_trade_date 已在 4A-0 正式冻结。4A-1 只能验证该日 remote data / "
                "released config 是否存在；不存在则 FAIL CLOSED（禁止自动换成 8/18、8/20 等）。"
                "若确需换日期，须另开 4A-0 contract revision + 新 dataset version。"
            ),
            "sample_suggestion": {
                "parity_gate_4A2": "100 normal + 5 boundary（与 Phase 3 同口径）",
                "wall_clock_4A3": "完整 eligible universe（生产 batching/claim/compute 总 wall-clock）",
            },
        },
        "evidence": {
            "frozen_coverage": frozen_coverage,
            "legacy_frozen_metadata_not_contract": {
                "manifest_instrument_universe_rule": universe_rule,
                "manifest_scope_universe_rule": scope_rule,
                "manifest_review_fact_universe": (
                    man.get("source_readiness", {})
                    .get("first_pyramid_history", {})
                    .get("coverage", {})
                    .get("review_fact_universe")
                ),
                "manifest_snapshot_rows": (
                    man.get("source_readiness", {})
                    .get("first_pyramid_history", {})
                    .get("coverage", {})
                    .get("snapshot_rows")
                ),
                "note": (
                    "以上均为旧 review-source manifest 的遗留统计，**不是** Phase 4A "
                    "eligible universe 合同的证据；Phase 4A contract 仅 "
                    "get_active_a_share_instruments()。"
                ),
            },
            "note_baseline_vs_4A3": (
                "Phase 3 serial projection 18-23min 基于既有 frozen ≈415 trading bars "
                "（~623 日历日）外推；4A 使用真实生产 daily-history contract（5000 日历日，"
                "≈3400+ trading bars）后 full-history DSA 可能拿到远多于 415 根，真实结果"
                "可能明显更慢，这反映此前 frozen 未覆盖生产历史长度分布"
            ),
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "dataset_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 4A-0R Contract Evidence Correction ===")
    print(f"audit_code_sha={head}")
    print(f"target_trade_date={target_trade_date} status={target_frozen_status}")
    print(f"frozen actual range: "
          f"{frozen_coverage['actual_global_range']['earliest_trade_date']} → "
          f"{frozen_coverage['actual_global_range']['latest_trade_date']} "
          f"span={frozen_coverage['actual_global_range']['calendar_span_days']} cal days, "
          f"trading={frozen_coverage['actual_global_range']['distinct_trading_days']}")
    bp = frozen_coverage['bars_per_instrument']
    print(f"daily bars len min/p50/p90/p99/max = "
          f"{bp['min']}/{bp['p50']}/{bp['p90']}/{bp['p99']}/{bp['max']}")
    print(f"n_instruments_in_frozen={frozen_coverage['n_instruments_in_frozen']}")
    print(f"manifest bar_lookback_calendar_days={bar_lookback_cal} "
          "(非 parquet coverage 权威字段，不建立 400→415 因果)")
    print("contracts verified: "
          "mdas_daily_lookback=5000 | no_15m=True | no_boards=True | "
          "session_factory_partial=True | released_config=SqlAlchemyReleasedConfigResolver | "
          "eligible=get_active_a_share_instruments")
    print("dataset spec written:", OUTPUT_DIR / "dataset_spec.json")


if __name__ == "__main__":
    main()