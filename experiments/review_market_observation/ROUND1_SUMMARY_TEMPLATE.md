# Round 1 Summary — Raw Data & Primitive Audit

> 本文件是 `audit/round1_summary.json` 的人类可读渲染（**待真实数据填充**）。
> 执行 `bash run_round1.sh` 后，本文件应被自动重写为真实报告。
> 工程侧 commit 时只保留该模板，**不把真实运行证据或真实数据写入仓库**（§AGENTS.md §7.15
> 仅 commit code/tests/schema，真正的审计 JSON/PARQUET/CSV 留在本地 data/audit/）。

---

## 执行谱系 (Frozen Dataset Lineage)

| 项 | 值 |
|---|---|
| 提取执行时间 (UTC)                  | `manifest.extracted_at` |
| code_version (提取脚本 Git SHA)     | `manifest.code_version` |
| schema_hash (69 列 hash)            | `manifest.schema_hash` |
| algorithm_version (outer distinct)  | `manifest.expected_layers.algorithm_versions` |
| history_contract_version            | `manifest.expected_layers.history_contract_versions` |
| target_trade_date_count             | `manifest.target_trade_date_count` |
| actual_trade_date_count             | `coverage_summary.trade_dates.count` |
| trade_date_start                    | `coverage_summary.trade_dates.start` |
| trade_date_end                      | `coverage_summary.trade_dates.end` |
| instrument_count                    | `coverage_summary.rows.instrument_count` |
| row_count                           | `coverage_summary.rows.row_count` |
| data_hash (sha256)                  | `manifest.data_hash` |

## §3 Integrity 审计结论

### 3.1 rows / dates / universe

- `row_count × duplicates(主键唯一性)`  →
- `instrument_count × expected (review_universe_001 = 全 A 股可被 first_pyramid_history 覆盖)`
- `trade_date × count × sorted_asc × is_exact_target`

### 3.2 lineage 一致性（关键交叉矩阵）

```
expected_versions = {algorithm_version} × {history_contract_version outer} × {history_contract_version payload}
```

- 若存在 **非单一 algorithm_version**，则 Warning：说明历史算法在 120 日内发生过切换，对时间序列的可比性有轻度影响，是否进入 PARTIAL 需看 `hc_outer == hc_payload`。
- 若 **hc_outer != hc_payload**：Blocker → canonical lineage 不闭合，结论 **INVALID**。

### 3.3 readiness 覆盖率

- `core_factor_ready`   覆盖率（row-wise） = `{ready_ratio}`
- `valid_for_market_aggregation` 覆盖率 = `{valid_ratio}`
- readiness 低的 **日期清单（<= 5% tail）**：`…`
- readiness 低的 **股票清单（<= 5% tail）**：`…`
- invalid_reason 分布（Top-5）：`…`

## §4 Primitive & Transition 审计摘要

### 4.1 分类状态全局频率（Top 1）

| 原语 | Top 1 状态 | 占比 |
|---|---|---|
| regime_value           | `…`  | `…` |
| swing_bias             | `…`  | `…` |
| internal_bias          | `…`  | `…` |
| structure_alignment    | `…`  | `…` |
| volatility_phase       | `…`  | `…` |
| momentum_direction     | `…`  | `…` |

### 4.2 连续原语统计（5% / 50% / 95%）

| 原语 | p5 | p50 | p95 |
|---|---|---|---|
| regime_strength          | … | … | … |
| dsa_dir_bars             | … | … | … |
| dsa_vwap_dev_pct         | … | … | … |
| sqzmom_val               | … | … | … |
| sqzmom_delta             | … | … | … |
| review_volume_ratio20    | … | … | … |
| review_amount_ratio20    | … | … | … |
| price_position_120d      | … | … | … |

### 4.3 T-1 过渡（Top 3 Transitions）

只列每个原语的 top-3 迁移：

| 原语 | Top Transitions（prev→curr, n, ratio） |
|---|---|
| regime_value        | 1→1, …；0→0, …；-1→-1, … |
| swing_bias          | … |
| internal_bias       | … |
| structure_alignment | 共振→共振, …；背离→背离, …；共振→背离, … |
| volatility_phase    | normal→normal, …；squeeze→normal, …；normal→squeeze, … |
| momentum_direction  | expanding→expanding, …；contracting→contracting, …；… |

## §11 Gate — Findings

所有 findings 在 `audit/integrity_findings.json`，最终 3 个 severity 聚合：

| Severity | Count |
|---|---|
| blocker  | `{N_blocker}`  |
| warning  | `{N_warning}`  |
| info     | `{N_info}`     |

## Final Verdict

```
ROUND 1 VERDICT = [ PASS | PARTIAL | INVALID ]
```

- **PASS**：没有 blocker，没有 warning；可进入 Round 2（Market State & 板块全景）。
- **PARTIAL**：没有 blocker，但存在 warning；进入 Round 2 时须标注 warning 所指向的原语可信度降档。
- **INVALID**：存在 blocker（主键重复、hc 不匹配、future leakage、120 交易日不足、valid 率异常低、schema_hash 漂移）。**在修正 blocker 之前，不得启动 Round 2。**

---

_本模板由 Round 1 代码提交时生成；真实执行报告仅保留在本地 data/ 与 audit/ 目录中，不纳入 Git。_
