# CHANGE-20260812-005 — Round 2B Experimental Filter Minimal PRD Closure

- **日期**: 2026-08-12
- **类型**: docs-only（需求收口 / PRD–Code Alignment）
- **领域**: 复盘模块 / Filter Engine / Experimental Filter Redesign（Exploration / Shadow）
- **关联 PRD**: `docs/prd/70-review.md`（新增 §8.0）
- **关联 Maps**: `docs/maps/70-review.md`（未修改；Map 仍描述 legacy 实现，待实现验收后单独授权同步）
- **前置外部验收**: ROUND 1C = PASS、ROUND 2A = PASS；Round 2B-0 Design Audit = READY_FOR_ROUND_2B
- **状态**: `prd_confirmed`（docs-only；不进入 Implementation，未写代码 / 未建表 / 未 migration / 未改 API / 前端）

## 1. 背景与目的

Round 2B-0 Design Audit 已完成并通过外部审计，但外部审计对其设计做两处修正：

1. **修正 Round 2B-0 legacy audit 错误结论**：Round 2B-0 报告称"当前 dev Filter 已抛弃 P/Q/U/C/V
   分位输入"——该判断错误。真实 `filter_engine.py` / `filter_definitions.py` 仍直接消费 `P/Q/U/C/V`
   payload、`historyPercentile120d`、`delta1d` 及其历史分位衍生量；PRD 已标记其为 `legacy
   implementation baseline` + `IMPLEMENTATION_REDESIGN_REQUIRED`。本次 closure 不把 legacy
   implementation 描述成已完成 Observation Evidence migration。

2. **Phase-1 archetype 削减**：Round 2B-0 原建议三个 archetype，外部审计修正为两个；Concentration
   archetype 暂缓（DEFER）。

本轮目标：在 `docs/prd/70-review.md` §8 Filter Engine 增加最小 `Experimental Filter Redesign Contract`
（建议 §8.0），**不新增独立 Candidate Layer chapter**，不新增永久产品层。

## 2. PRD 修改内容（docs/prd/70-review.md）

新增 **§8.0 Experimental Filter Redesign Contract（Round 2B，Exploration / Shadow）**，含 11 个子节：

- §8.0.1 定位：`CandidateResult` 是 Filter Engine redesign 的 shadow / exploration 实验结果，
  **不是新永久 domain layer**；不得新增 `Evidence → Candidate → Filter → Signal` 永久产品层。
- §8.0.2 输入 / 输出 / 目的：输入仅 L2-A Objective Evidence；输出 temporary `CandidateResult`；
  目的为验证透明 Evidence conditions 是否值得进入未来正式 Filter / Signal。
- §8.0.3 `CandidateResult` 语义（transient）：最低语义字段 + 禁止 score/grade/rank/recommendation/
  bullish-bearish/opportunity-risk conclusion。
- §8.0.4 缺失语义：MATCHED / NOT_MATCHED / NOT_EVALUABLE 三态；mandatory unavailable → NOT_EVALUABLE
  （不当 NOT_MATCHED）；optional unavailable 仍可 evaluate；missing 不当 0。
- §8.0.5 Historical Evidence：真实 PIT history 不足 60 → historical percentile 不得 mandatory；
  ready 时 optional；insufficient_history 不阻塞 Phase-1；不修改 min60、不制造历史。
- §8.0.6 Threshold = Experiment Configuration：PRD 不写具体数字阈值；由 implementation experiment
  config 显式传入；不要求 generic version platform / YAML rule engine / database config platform。
- §8.0.7 Phase-1 archetype（两个，anchor-based）：
  - A. `BREADTH_EXPANSION`：Trend breadth + Price breadth 在明确 historical anchor 相对当前的
    anchor-based broadening pattern（显式 `d1.delta` / `d3.delta` / `d5.delta`，禁止模糊 "D5 > D3 > D1"，
    不声称逐日单调扩张）。
  - B. `PARTICIPATION_CONFIRMATION`：participation change 与 breadth change 同步确认（显式
    `current` / `d1.delta` / `d3.delta` / `d5.delta`）。
  - `momentum_expanding_ratio` / `price_return_mean` 不作独立 archetype，仅允许 optional supporting Evidence。
- §8.0.8 Concentration 暂缓（Phase-1 DEFER）：`price_raw_hhi` 未按 member count normalized，不能跨 Scope
  用统一 absolute current threshold；PRD 已区分 concentration_state_high/change/abnormal；本轮不实现
  normalized HHI，不改 L1。
- §8.0.9 Scope activation：Phase-1 真实实验 `concept` + `industry_l1`；`industry_l2/l3` 架构兼容 smoke；
  `Market` / `Major Index` / `Style` 不激活。
- §8.0.10 Persistence / Consumer：NO NEW TABLE / NO migration / NO Candidate persistence / NO Signal
  persistence / NO Discovery consumer / NO API / NO Frontend；runtime evaluate only。
- §8.0.11 Legacy 共存：legacy `filter_definitions.py` / `filter_engine.py` / `review_signal_service.py`
  本轮不改，继续存在；本 §8.0 不把 legacy 描述成已完成 Observation Evidence migration。

## 3. 边界（未做）

- 不写 backend code / frontend / API / migration / DB write / CI。
- 不修改 `filter_definitions.py` / `filter_engine.py` / `review_signal_service.py`。
- 不新增独立 Candidate Layer chapter，不新增永久产品层。
- 不修改 Maps（待实现验收后单独授权同步）。
- 不实现 normalized HHI，不修改 L1。

## 4. Requirement Traceability（PRD 条款）

| Requirement | PRD clause | current implementation status |
|---|---|---|
| Evidence-only input | §8.0.2 | legacy 仍消费 P/Q/U/C/V payload（REDESIGN REQUIRED）；新 Experimental 仅 L2-A |
| Experimental threshold | §8.0.6 | threshold 由 experiment config 传入，PRD 不写数字 |
| CandidateResult transient semantics | §8.0.3 | 非永久业务层；禁止 score/rank/recommendation |
| No score | §8.0.3 | 禁止 score/grade/rank/0-100 |
| Missing semantics | §8.0.4 | MATCHED / NOT_MATCHED / NOT_EVALUABLE 三态 |
| Historical optional | §8.0.5 | historical percentile 非 mandatory；不阻塞 Phase-1 |
| No persistence | §8.0.10 | NO NEW TABLE / NO migration / runtime only |
| No consumer activation | §8.0.10 | NO Discovery consumer / NO API / NO Frontend |
| Legacy coexistence | §8.0.11 | legacy Filter 继续存在，未改 |
| Concentration defer | §8.0.8 | PRICE_CONCENTRATION_DIVERGENCE Phase-1 DEFER |
| Two Phase-1 archetypes | §8.0.7 | BREADTH_EXPANSION + PARTICIPATION_CONFIRMATION |

## 5. Verification

- 仅 docs-only 修改；`git diff` 仅含 `docs/prd/70-review.md` + `docs/changes/2026/CHANGE-20260812-005-*.md`
  + `docs/changes/INDEX.md`。
- 未触碰任何代码 / schema / API / frontend。
- governance / docs checks 按 repo convention 运行（无代码改动，仅文档一致性）。

## 6. 后续

- Round 2B implementation：新增 `scope_candidate_experiment.py`（pure evaluator）+ 可选薄 service；
  21 项 PURE_UNIT_TEST；小规模 replay（5-10 trading days, concept + industry_l1）。
- 实现验收后再单独授权同步 Maps。
- consumer cutover（Signal/Discovery）属后续 Round，不在 Phase-1。
