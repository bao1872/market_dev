# S1 — Scope Data & Semantic Baseline 总结

- **实验**：Scope Observation Model — Round S1（Scope Data & Semantic Baseline）
- **分支**：`exp/scope-observation-model-v1`（基于冻结 `dev` `DEV_BASE_SHA=6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0`）
- **窗口**：2026-02-09 ~ 2026-08-10（120 交易日）
- **上一实验状态**：`exp/review-market-observation-v1` remote HEAD = `71674b3` 已完成 Round 3；其自动生成结论（BREADTH=MISSING / PARTICIPATION=MISSING / 9 archetype=CLEAR / P/Q/U/C restructure）**不作为本轮事实输入**。允许使用 Round1/2 已审计真实数据与 Current Review factual component map。
- **生产 DB**：READ ONLY（所有查询均为 SELECT，`SET TRANSACTION READ ONLY` 语义，无 DDL/DML）。

## 1. Scope membership inventory 结论

| scope_family | PIT 语义 | 历史 membership 可用 | 覆盖 |
|---|---|---|---|
| market | CONFIRMED_PIT（state 横截面） | true | 120 日（365 distinct dates） |
| industry | CONFIRMED_PIT | false（first_definition_effective_from=08-01；latest_definition_effective_from=08-05；pre-08-01 historical membership unavailable） | 3 版本日（08-01/08-03/08-05） |
| concept | CONFIRMED_PIT | false（first_definition_effective_from=08-01；latest_definition_effective_from=08-05；pre-08-01 historical membership unavailable） | 3 版本日（08-01/08-03/08-05） |
| style | SNAPSHOT_BUT_UNVERIFIED | false（universe_memberships 空） | 0 |
| index | SNAPSHOT_BUT_UNVERIFIED | false（universe_memberships 空） | 0 |

> 说明：industry/concept 的 `history_end=2026-08-05` **不**表示 membership 仅有效到 08-05。真实语义是 PIT bootstrap 从 2026-08-01 开始，definition effective_from 观测到 08-01/08-03/08-05，pre-08-01 historical membership unavailable；已生效 definition 在 effective_to=NULL 时继续覆盖 08-05 之后日期。120 日 board experiment DATA_BLOCKED 的真正原因是**缺少 2026-08-01 之前的 PIT membership history**，而不是 08-05 之后没有 PIT membership。

## 2. Eligible scope families（120 日）

- **market**：FULL_120D_ELIGIBLE（由 `first_pyramid_history_daily_state` 每日期 state 横截面构成，5277 instrument/日，严格 PIT，无回填）。

## 3. Blocked scope families（120 日）

- **industry / concept**：PARTIAL_HISTORY_ONLY。`board_definition_versions` + `board_membership_history` 仅覆盖 2026-08-01/08-03/08-05 三个 effective_from（646/646/23 板，membership 154391 行）。**禁止用当前成员回填 120 日**。
- **style / index**：NOT_AVAILABLE。`universe_definitions` 有 4 行（2 major_index + 2 style），但 `universe_memberships` 表为空（0 行），`population_status=blocked_external_population`。

## 4. Trend facts（§8）

- **categorical state**：`regime_value`（1/0/-1 → up/neutral/down）、`fp_trend_direction`（上行/下行/震荡）、`trend_transition`、`segment_direction`。
- **continuous descriptor**：`regime_strength`、`dsa_dir_bars`、`dsa_vwap_dev_pct`、`segment_slope`、`segment_change_pct`。
- **transition source**：`trend_transition`（需确认 freshness/date 证明今日新发生）。
- **explanatory**：`regime_strength`、`dsa_dir_bars`、`dsa_vwap_dev_pct`。

## 5. Structure facts（§9）

- **categorical state**：`swing_bias`、`internal_bias`、`structure_alignment`（共振/背离）。
- **persistent state/event**：`latest_ob_active`、事件方向 + `active_swing_ob_count`/`active_internal_ob_count`。
- **fresh event today**：`latest_bos_direction` + `latest_bos_freshness`、`latest_choch_direction` + `latest_choch_freshness`、`latest_ob_direction` + `latest_ob_freshness`。
- **关键区分**：不能把 latest bearish CHoCH 直接当作 today bearish transition，必须用 freshness==0（或对应 freshness 阈值）证明今日发生。

## 6. Momentum facts（§10）

- **State**：`momentum_direction`（expanding/contracting/flat，来自 sqzmom_val 符号）、`volatility_phase`。
- **Transition source**：`momentum_change`（enhancing/weakening，来自 sqzmom_delta）、`sqzmom_delta`。
- **continuous descriptor**：`sqzmom_val`、`sqzmom_delta`。

## 7. Chip / Participation semantic inventory（§11）

- 当前数据**不存在明确的芯片成本/筹码共识字段**。
- **PARTICIPATION 倾向**：`volume_percentile_20`、`review_amount_percentile200`、`volume_zscore_20`、`current_vs_prev_volume_mean_ratio`、`current_vs_prev_amount_mean_ratio`、`fp_segment_volume_ratio`。
- **BOTH_POSSIBLE（最大语义重叠）**：`volume_ratio_20`、`review_volume_ratio20`、`review_amount_ratio20` —— 既可用作个体成交活跃度（CHIP 倾向），也可作为 Scope 横截面参与度（PARTICIPATION 倾向）。
- **S1 不下最终 verdict**（SAME_AXIS/DISTINCT_AXES），只记录证据，待 S2/S3 用真实横截面与时间序列行为判定。

## 8. Concentration feasibility（§12）

| HHI | 状态 | 依据 |
|---|---|---|
| price contribution HHI | **AVAILABLE** | `review_return_1d` 由 bars_daily close 计算（member_change_hhi 已在 registry 定义） |
| amount contribution HHI | **AVAILABLE** | `review_amount` 由 bars_daily.amount 提供（近窗口 ~99.9% 完整） |
| event contribution HHI | **PARTIAL** | 事件方向/freshness 可用，但事件语义（bullish/bearish 归一化权重）需先冻结 |

## 9. Selected 3–5 scopes + selection rule（§13）

- **Selection rule**：仅从 CONFIRMED_PIT 或能严格构造 PIT 的 Scope 中选；用 member count quantile + history completeness 客观选择，不人工按观点挑。因 industry/concept PIT 仅观测到 08-01/08-03/08-05 三个 definition effective_from（pre-08-01 historical membership unavailable，已生效 definition 在 effective_to=NULL 时覆盖 08-05 之后），5 个代表交易日取在 08-03~08-07 以严格成立 PIT。
- 选择结果：**market**（PIT 5277）、**黄金概念**（PIT 80，fp_row_count=80）、**MicroLED概念**（PIT 80，fp_row_count=79）、**锂电池概念**（PIT 594，fp_row_count=593）、**钢铁-钢铁-特钢**（industry，PIT 12，fp_row_count=12）。
  - MicroLED：`pit_member_count=80` / `fp_row_count=79`（1 个 PIT 成员当日无 fp 行）。
  - 锂电池：`pit_member_count=594` / `fp_row_count=593`（1 个 PIT 成员当日无 fp 行）。
  - 两者不得混淆：`member_count` 一律表示 PIT membership，`fp_row_count` 表示当日有 fp 行的 members。
- 覆盖 PIT member_count 低（12）/中（80）/高（594）/全市场（5277），以及 industry/concept/market 不同 family。

## 10. State/Breadth sanity-check result（§14，S1 Correction 已修正）

- 对 5 scopes × 5 代表交易日（2026-08-03~08-07）做最小聚合。
- **Trend**：regime up/neutral/down distribution；**Structure**：swing up/neutral/down、internal up/neutral/down；**Momentum**：expanding/flat/contracting。
- **S1 Correction §1 per-trade-date PIT resolution**：对每个 scope_id×trade_date 独立 resolve `definition.effective_from <= trade_date AND (effective_to IS NULL OR effective_to > trade_date)`，再对 membership 同样过滤（与 `board_membership_service.resolve_board_membership_at()` 一致）。4 个 board scopes 在 08-03~08-07 均 resolve 到 2026-08-03 版本（`members:*`，legacy 08-01 版本 effective_to=08-03 已关闭），**无 membership change on 08-05**。输出新增审计字段 `membership_definition_effective_from` / `membership_version` / `pit_member_count`。
- **S1 Correction §2 有效 denominator**：区分 `pit_member_count`（PIT members）与 `fp_row_count`（该日有 fp 行的 members）与 `axis_valid_count`（具有该 axis 有效 state 的 members）。本数据中 axis_valid_count == fp_row_count（所有 fp 行的 categorical 字段均非 NULL）；sum-ratio 契约以 axis_valid_count 为 denominator，**不**直接用 pit_member_count 或 "count(state rows)" 冒充 valid_members。
- **S1 Correction §3 categorical zero semantics**：
  - `swing_bias=0` = 合法 neutral（1=上行/0=震荡/-1=下行），不是 invalid。market 有 53 个 `swing_bias=0` 成员，修正后 `swing_neutral=53`（旧版误记为 0 并导致 swing 求和 < denominator）。
  - `internal_bias=0` 同理为合法 neutral；本数据中无 `internal_bias=0`，故 `internal_neutral=0` 为真实值。
  - `momentum_direction` 的 flat 为合法状态（expanding/flat/contracting），不得丢成 invalid；本数据中无 flat，故 `momentum_flat=0` 为真实值。
  - invalid 仅用于 NULL / unsupported / history insufficient / 正式 readiness contract 判 invalid。
- **验证结果**：
  - denominator = 该日 axis_valid_count（market 5277、gold 80、micro 79、lio 593、steel 12）。
  - **regime / swing / internal / momentum 互斥分类求和 = axis denominator**（全部 scope/date 通过，`*_sum_ok=True`）。
- **技术结论**：State/Breadth 聚合技术上成立，sum ratios 契约满足，denominator 区分正确，categorical 0/flat 语义正确，PIT 无泄漏。

## 11. Tests

- `tests/test_s1_contracts.py`：13 个纯单元测试，全部通过。
  - 原 7 个：CURRENT_ONLY 禁入历史样本 / PIT date 窗口 / denominator=有效成员 / 同一 denominator / 互斥 ratio 求和 / 无 future membership / 无 future facts。
  - S1 Correction 新增 6 个 regression：one snapshot cannot be reused when newer version intervenes / resolve_pit_definition selects latest active / swing_bias=0 is valid neutral / momentum flat is valid state / categorical distribution sums to axis denominator / candidate_axis momentum fields == MOMENTUM。

## 12. Resource usage

- 全部分析采用 DB-native aggregation（SQL SELECT 聚合，每步返回小型结果 ≤ 数十行），未拉取 raw full-market history 进 Python。
- 生产 DB 仅执行只读 SELECT；未写任何 production 数据。

## 13. 生产 DB 零写入确认

- 所有查询均为 `first_pyramid_history_daily_state` / `board_definition_versions` / `board_membership_history` / `universe_*` / `market_boards` / `bars_daily` 的 SELECT 聚合。无 INSERT/UPDATE/DELETE/DDL。✅

## 14. dev 未修改确认

- 当前分支 `exp/scope-observation-model-v1`，基于 `DEV_BASE_SHA=6fc7384`；`dev` 与 `origin/dev` 均保持 `6fc7384` 未被修改。✅

## 15. 回答的问题（§15）

- **Q1**：仅 market 可做可靠 120 日实验。
- **Q2**：industry/concept 仅 PARTIAL_HISTORY_ONLY（PIT 只覆盖近期 3 版本日）；style/index NOT_AVAILABLE（universe_memberships 空）。
- **Q3**：Trend / Structure / Momentum 具备足够 canonical facts 支持 State/Breadth。
- **Q4**：Chip/Participation 最大重叠点是 volume/amount ratio（个体活跃度 vs 横截面参与度）；当前无芯片成本共识字段。
- **Q5**：price HHI / amount HHI AVAILABLE；event HHI PARTIAL（需冻结事件语义）。
- **Q6**：最小 Scope sample 的 State/Breadth 聚合技术上成立（ratio 求和契约满足、denominator 一致、无 future/backfill）。

## 15b. S1 Correction 最终 verdict（外部审计修正后）

- **A. membership feasibility audit 是否仍成立**：成立。market=CONFIRMED_PIT（120 日 state 横截面）；industry/concept=PARTIAL_HISTORY_ONLY（PIT 仅覆盖 08-01/08-03/08-05 版本日）；style/index=NOT_AVAILABLE（universe_memberships 空）。外部审计指出的 PIT 误用已修正为 per-trade-date resolve。
- **B. 近期 board PIT sample 是否真实有效**：有效。4 个 board scopes × 5 日期（08-03~08-07）均独立 resolve 到 2026-08-03 版本 `members:*`（legacy 08-01 版本已关闭），无 membership change on 08-05；`s1_membership_version_check.csv` 记录每日 definition effective_from / membership_version / member_count 审计字段。
- **C. State/Breadth aggregation 技术语义是否成立**：成立。修正 categorical 0/flat 语义后，regime / swing / internal / momentum 的互斥分类求和均等于 axis_valid_count（=fp_row_count），denominator 区分 pit_member_count / fp_row_count / axis_valid_count。
- **D. 120 日 board experiment 是否仍 DATA_BLOCKED**：仍 DATA_BLOCKED。industry/concept 缺少 2026-08-01 之前的 PIT membership history（definition effective_from 仅观测到 08-01/08-03/08-05，pre-08-01 historical membership unavailable），不足以支撑 120 日实验；120 日仅 market 可行。注意：已生效 definition 在 effective_to=NULL 时继续覆盖 08-05 之后日期，DATA_BLOCKED 的原因不是 08-05 之后没有 PIT membership。

## 15c. S1 Correction 产出文件

- `out/scope_observation_component_inventory.csv`：momentum 相关字段 candidate_axis 修正为 `MOMENTUM`（原误标 STRUCTURE/PRICE）。
- `out/s1_scope_state_breadth_sample.csv`：重建，保留 5 scopes × 5 days，per-trade-date PIT + 正确 categorical 语义。
- `out/s1_membership_version_check.csv`：新增，记录 4 个 board scopes × 5 日期 membership 版本审计。
- `out/s1_scope_selection.json`：note 更新为 per-trade-date PIT resolution 说明。
- `tests/test_s1_contracts.py`：新增 6 个 S1 Correction regression 测试。

## 16. 下一阶段

S1 只做 Data & Semantic Baseline。**不进入 S2**，不执行 Observation Collision / Archetype Replay / Minimum Sufficient Set / 最终模型推荐，不修改正式 Review PRD。