# CHANGE-20260816-001 — Auction Historical 09:25 Canonicalization Contract Freeze (+ Round 2B-A / Round 3A-1 Corrective Closure)

- **日期**: 2026-08-16
- **类型**: contract-freeze + experiment-runner implementation（Exploration）
- **领域**: Auction 历史竞价源 (`pytdx` historical transaction) 09:25 canonical record 归一化
- **关联 PRD**: `docs/prd/75-auction-analysis.md`（§AU-04-4）
- **关联实验**: `experiments/pytdx_auction_history/`（runner + tests + replay）
- **决策证据**: run_live2（REAL ENVIRONMENT Round 1 RETRY）+ Round 2A MULTIPLE_0925 Semantics Closeout + Round 2B-A / Round 3A-1 corrective closure
- **状态**: `implemented_unconfirmed`（代码 + 测试通过，待用户/ChatGPT 远端 SHA 审核）

## 1. Why（背景）

MULTIPLE_0925 raw multiplicity 曾被错误地等同于 business ambiguity。

run_live2 + Round 2A 证据显示：每 eligible source-day 恰好存在 **1 条 positive-volume canonical 09:25 row**，
其余为 **zero-volume auxiliary row**（`vol=0`）。61 个 MULTIPLE cases 全部为
`(8,0) + (2,>0)` 稳定配对（**observational only**：`buyorsell_raw=8` 仅是 source evidence / diagnostics，
不构成任何 authoritative 业务语义；合同 owner 只有 `volume validity / raw_vol`，**不把 8 定义为 sentinel**）。
价格 60/61 完全相同（唯一差异 601012/2026-08-11 为 1 分 tick 级，来自 zero-volume auxiliary row，非 canonical price owner）。

因此原 `raw canonical row count == 1` 的 FOUND 判定不再成立；必须引入独立的 canonicalization 层。

## 2. Decision（合同）

唯一 positive-volume record（`raw_vol > 0` 且 raw_vol valid）拥有 historical auction price / volume。
正式规则**只基于 `raw_vol` 的有效性**，**不依赖任何 `buyorsell` numeric code 业务语义**（当前无 authoritative source）。
`buyorsell_raw` 仅作 source diagnostics 保留。

Raw volume 三态分类（Round 2B-A closure，**不依赖 buyorsell**）：
`POSITIVE` = finite numeric > 0；`ZERO` = finite numeric == 0；`INVALID` = None / 非有限 / 负数 / 无法解析。

- CASE A 存在任何 INVALID volume → `INVALID_VOLUME_0925`：price/volume/amount=None（INVALID 优先，不能假定 zero auxiliary）
- CASE B 0 INVALID 且恰好 1 positive row → `CANONICAL`：price=positive row raw_price；volume_shares=raw_vol×100
- CASE C 0 INVALID 且 0 positive row → `NO_VOLUME_BEARING_0925`：price/volume/amount=None
- CASE D 0 INVALID 且 >1 positive row → `MULTIPLE_VOLUME_BEARING_0925`（真正 ambiguity）：price/volume/amount=None，保留全部 raw

Volume unit：LOT × 100 shares（已接受）。Amount：`DERIVED_PRICE_X_NORMALIZED_VOLUME` = price × volume_shares。

Raw status（source multiplicity）与 Canonicalization status（business usability）**两层分离**：
`raw_multiple_count` 不计入 source failure；`INVALID` volume rows **不计入** auxiliary zero volume。

## 3. Evidence

- run_live2：295 eligible / 234 FOUND / 61 MULTIPLE / 356 raw canonical rows（234+122 对账一致）
- Round 2A：`{2: 61}` 恰好 2 行；`buyorsell=(8,2): 61`；`vol=0` 永远在 8-record；60/61 same price
- Replay（本轮）：295 eligible → **canonical=295 / no_volume=0 / multi_positive=0**；
  61 raw-multiple 全部 canonicalized 为 CANONICAL；601012/2026-08-11 selected price=12.94（positive row）

## 4. 实现变更（runner）

`experiments/pytdx_auction_history/auction_history_semantics_validation.py`：
- 新增纯函数 `canonicalize_auction_0925(canonical_records)` + `Auction0925Canonicalization` dataclass
- 新增常量 `AUCTION_LOT_MULTIPLIER=100`、`CANON_STATUS_*`（`CANON_STATUS_INVALID_VOLUME`）、`AMOUNT_SOURCE_*`、`VOLUME_CLASS_*`
- `classify_raw_volume(raw_volume_value)`：三态分类 POSITIVE / ZERO / INVALID，**不依赖 buyorsell**
- `_normalize_raw_transaction` 增加 safe normalization：`volume_parse_status`（OK/ABSENT/NON_FINITE/MALFORMED），malformed vol 不崩 runner、不伪装成 0
- `extract_from_full_day` 增加 `raw_canonical_record_count` / `positive_volume_record_count` / `zero_volume_record_count` / `invalid_volume_record_count` / `auxiliary_zero_volume_record_count`（auxiliary 严格等于 valid numeric raw_vol==0）
- `run_single_observation` / `run_corporate_observation`：计算 canonicalization（四态），Lane A/B 改由 `CANONICAL` 门控（取代原 raw `FOUND`）
- `compute_amount_evidence` 改为 price × normalized volume（DERIVED 已接受）；移除覆盖它的 0-arg stub
- data quality / 06 / 11 增加 `invalid_volume_count` / `invalid_volume_record_count` 与 canonical 四态计数

## 5. 测试

`tests/test_auction_history_semantics_validation.py`：
- Round 2B：新增 C1–C8 + integration（+11），覆盖 single / zero-aux / different-price aux / two-positive / all-zero / invalid-vol / amount / multiplicity-not-ambiguity + Lane gate + extract counts
- Round 2B-A corrective closure：C6 改为断言 `INVALID_VOLUME_0925`；新增 tri-state 分类、`negative`/`malformed` INVALID、以及 3 个 **TRUE observation integration** 测试（直接调用 `run_single_observation` 真实穿过 extraction→canonicalization→Lane A/B→amount：raw-multiple→CANONICAL+Lane A/B COMPUTED、multi-positive→blocked、invalid→INVALID_VOLUME_0925）
- 整文件 collect-only **56 collected**；全量运行 **56 passed / exit 0**（无 pytest INTERNALERROR token；仅有 py_mini_racer GC 期 `__del__` 噪声，不影响结果）

## 6. 禁止项（未触碰）

production backend / frontend / migration / API / scheduler / worker / 重新请求 pytdx /
120-day backfill / threshold calibration。run_live2 原 evidence 未改。

## 7. Round 3A-1 Corrective Closure — Price Validity

- **性质**: MINIMAL CONTRACT CORRECTION（仅关闭 silent missing-price→0 风险）
- **原始 defect**: `_normalize_raw_transaction` 中 `raw_price = float(rec.get("price", 0.0) or 0.0)` 把 missing / empty / None / `""` / `0` 全部静默归一为 `0.0`，伪装成真实 auction price = 0，导致 `auction_price=0 / gap≈-100% / amount=0` 的 silent bad fact。
- **修复合同**:
  - raw price normalization 不再 `missing → 0.0`；`raw_price: Optional[float]`，`price_parse_status ∈ {OK, ABSENT, NON_FINITE, MALFORMED}`，原始 `source_record` 不变。
  - 新增 `is_valid_auction_price(price)`: price is not None AND finite AND price > 0。
  - canonicalization 第 4 步（恰好 1 positive-volume row）之后增加 price 校验：invalid → `INVALID_PRICE_0925`（price/volume/amount=None, amount_source_type=None, Lane A/B=None）；valid → `CANONICAL`。
  - **volume precedence 不变**：INVALID_VOLUME_0925 → MULTIPLE → NO_VOLUME → 1 positive（再查 price）。
  - **zero-volume auxiliary row price 不参与 price validity gate**：auxiliary invalid price 不阻止 canonicalization（601012 12.95 vs 12.94 场景保持 CANONICAL 12.94）。
  - 仅区分 valid vs invalid，不另设 `ZERO_PRICE` / `NEGATIVE_PRICE` / `MALFORMED_PRICE` 业务 status。
- **实现变更（runner）**:
  - 新增常量 `CANON_STATUS_INVALID_PRICE="INVALID_PRICE_0925"` 与 `PRICE_PARSE_STATUS_*`
  - `is_valid_auction_price()` helper；`_normalize_raw_transaction` price safe normalization
  - `NormalizedAuctionTransaction`：`raw_price` 改 `Optional[float]`，新增 `price_parse_status`
  - `canonicalize_auction_0925` 第 4 步 price gate
  - `run_single_observation` / `run_corporate_observation` 增加 `obs["invalid_price_count"]`（per symbol）
  - `compute_data_quality_summary` / 06 CSV / 11 `canonicalization_contract` 增加 `invalid_price_count` / `INVALID_PRICE_0925`
- **测试（PURE）**: 新增 `is_valid_auction_price` helper、normalize missing/malformed/nan/inf、`single positive price=None/0/-1/malformed → INVALID_PRICE_0925`、aux invalid price 不污染、以及 1 个 **TRUE observation integration**（`run_single_observation` 真实穿过 → INVALID_PRICE_0925，Lane A/B/amount=None，无 derived amount）。
- **Replay（immutable run_live2）**: 应用新 canonicalizer；INVALID_PRICE 预期可能为 0，由 replay 实际证明；601012/2026-08-11 selected price=12.94 保持。
- 整文件 collect-only 重新记录；全量 ×2 仍 pass / exit 0。

## 8. Round 3A-2A Corrective Closure — Source-Incomplete Must Not Canonicalize

- **性质**: MINIMAL WIRING CORRECTION + EXISTING EVIDENCE RE-ANALYSIS（不重新请求 source；不重跑 120-day；不开始 full-market backfill）
- **baseline SHA**: origin/dev == f13780b8d3a5a8a042be6283cabc38f9cde00a5f（HEAD == origin/dev 已确认）
- **原始 defect**: `run_single_observation` / `run_corporate_observation` 无论 `full_day_status` 如何，都会无条件调用 `canonicalize_auction_0925(extraction.records)`。因此 `EMPTY` / `SOURCE_ERROR` / `PAGINATION_STALLED` / `PAGINATION_LIMIT_REACHED` 的 source-incomplete observations 被错误赋予了 `NO_VOLUME_BEARING_0925` / `INVALID_PRICE` 等 business canonicalization status，污染源数据质量 denominator（temporal120 中 2 个 EMPTY 被错计为 `no_volume_bearing_count: 2`）。
- **修复合同（FIX 1）**:
  - 只有 `full_day_status == COMPLETE` 才允许进入 `canonicalize_auction_0925()`。
  - 对于 `EMPTY` / `SOURCE_ERROR` / `PAGINATION_STALLED` / `PAGINATION_LIMIT_REACHED`：`canonicalization_status = None`、`auction_price_raw` / `auction_volume_raw_lots` / `auction_volume_shares` / `auction_amount` / `auction_amount_source_type` / `lane_a` / `lane_b` 全部 = `None`、`canonicalization_reason = "SOURCE_DAY_INCOMPLETE"`。
  - **不得**产生 `NO_VOLUME_BEARING_0925` / `INVALID_PRICE` / `INVALID_VOLUME` / `MULTIPLE_VOLUME`。
  - **不新增** `SOURCE_INCOMPLETE_CANONICALIZATION_STATUS`：`source incomplete` 不是 canonicalization status，其 source truth 已由 `full_day_status` 表达。
  - 区分两类：(a) `COMPLETE` + canonical 09:25 rows + 全部 zero valid volume → 真正的 business `NO_VOLUME_BEARING_0925`；(b) `EMPTY` source-day → `canonicalization_status = None`。
- **FIX 2 (Data Quality denominator)**: `compute_data_quality_summary` 的 canonicalization count 只应自然来自 pagination `COMPLETE` observations；修正 wiring 后 source-incomplete 不再进入。Reconciliation 新增：`COMPLETE == CANONICAL + NO_VOLUME + MULTIPLE_VOLUME + INVALID_VOLUME + INVALID_PRICE`（+ 任何其它明确 COMPLETE-but-not-canonical 状态）。
- **FIX 3 (derive_live_status)**: 删除无效的 `status_key` 参数，最小修正三个 dimension：
  - `auction_source_evidence`：eligible = 全部 observations；COMPLETE 仅当 `full_day_status == COMPLETE` 且 `extraction_status` ∈ {FOUND, MULTIPLE_0925, NONCANONICAL_0925_TIME, MISSING_0925}。
  - `price_open_evidence`：eligible = `canonicalization_status == CANONICAL`；COMPLETE 要求每个 eligible 的 `lane_a is not None AND lane_a.status == "COMPUTED"`（all COMPUTED→COMPLETE / 部分→PARTIAL / 0→INSUFFICIENT）。
  - `volume_unit_evidence`：继续基于 `daily_volume_ratio is not None`。
  - Corporate 逻辑本轮不重构。
- **FIX 4 (Tests)**: 新增 T1（EMPTY→canonicalization_status is None，非 NO_VOLUME_BEARING）、T2（SOURCE_ERROR→不 canonicalize）、T3（CANONICAL 但 lane_a=None→price_open_evidence != COMPLETE）、T4（全部 lane_a.status=COMPUTED→price_open_evidence=COMPLETE）。
- **实现变更（runner）**: `run_single_observation` / `run_corporate_observation` 的 canonicalization 进入 `if full_day.status != "COMPLETE"` 门控（else 分支计算）；Lane A/B 与 amount_evidence 改用 `obs["canonicalization_status"]` / `obs["auction_amount_source_type"]` 等已落字段，消除 `canon` 越界；`derive_live_status` 重写三 dimension。
- **测试**: 整文件 collect-only **69 collected**；全量 **69 passed / exit 0**；`git diff --check` PASS。
- **TEMPORAL120 EVIDENCE RE-ANALYSIS（不改原 artifact，输出至 `output/temporal120/2026-08-14/contract_closure/`）**:
  - routine attempts 3480 = 3478 COMPLETE + 2 EMPTY（300142/2026-03-17、300142/2026-03-18，原错计 NO_VOLUME）。
  - COMPLETE routine：raw FOUND 2821 / MULTIPLE 657；canonicalization CANONICAL 3478 / NO_VOLUME 0 / MULTI_POSITIVE 0 / INVALID_VOLUME 0 / INVALID_PRICE 0。
  - Reconciliation（修正后）：3478 COMPLETE == 3478 CANONICAL；2 EMPTY 不进入 canonical denominator。
  - price/open：eligible（routine+COMPLETE+CANONICAL）3478；`lane_a.status == COMPUTED` 3478；`price_exact_match == True` 3478；exact_match_rate = 1.0；diff_abs / diff_rel median/p90/p95/max 全为 0.0；mismatch 0。
- **结论**: source-incomplete contract 与 COMPLETE-zero-volume 已明确区分；120-day temporal stability evidence 仍 SUFFICIENT；等待 ChatGPT 审核是否进入 full-market backfill。

## 9. 后续

经 ChatGPT 审核远端 SHA 后，再决定是否正式启动 120-day full-market historical research backfill。
