# CHANGE-20260816-001 — Auction Historical 09:25 Canonicalization Contract Freeze (+ Round 2B-A Corrective Closure)

- **日期**: 2026-08-16
- **类型**: contract-freeze + experiment-runner implementation（Exploration）
- **领域**: Auction 历史竞价源 (`pytdx` historical transaction) 09:25 canonical record 归一化
- **关联 PRD**: `docs/prd/75-auction-analysis.md`（§AU-04-4）
- **关联实验**: `experiments/pytdx_auction_history/`（runner + tests + replay）
- **决策证据**: run_live2（REAL ENVIRONMENT Round 1 RETRY）+ Round 2A MULTIPLE_0925 Semantics Closeout + Round 2B-A corrective closure
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

## 7. 后续

经 ChatGPT 审核远端 SHA 后，再决定是否正式启动 120-day full-market historical research backfill。
