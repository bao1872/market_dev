# CHANGE-20260816-003 — FULL-MARKET 120-BAR MEMBER FACT BACKFILL (Round 3B-B)

## 1. WHY

建立真正用于全市场 Auction Member Fact 历史回补的 orchestration runner。核心是锁死
**120 BAR = 截至 as_of 的最近 120 个官方 A 股交易日 / daily bars**（不是 120 自然日）。

## 2. 120-BAR CONTRACT（锁死，NOT calendar days）

- `120 bar` ≠ 过去 120 个自然日。
- `120 bar` = 截至 `as_of = 2026-08-14` 的最近 120 个官方交易日。
- 通过 project official trading calendar：`previous_trading_dates(session, as_of, 120)`。
- **禁止** `date - timedelta(days=120)` / 固定自然日期区间 / 6 个月近似。
- `earliest_bar_date` 由 official calendar ACTUAL 决定。
- `bar_index`：1（最老）→ 120（= 2026-08-14），按时间升序；非 calendar-day offset。

## 3. POPULATION RULE

- 当前 canonical SH/SZ A-share identity owner + `listing_date <= T`。
- 对交易日 T：只有 `listing_date <= T` 才允许生成 Auction Member Fact。
- 窗口中途 IPO 的股票不会为了凑满 120 条向上市前扩展（最多约 T−listing 的 bars）。
- **delisting lifecycle OUT OF SCOPE**（delisting_date / historical stale instrument /
  UniverseMembership / 退市 source 不研究）。
- `Instrument.status` 不得作为历史 eligibility 条件。

## 4. QFQ DEGRADED FAIL-CLOSE（HARD GATE）

- `pit_gap` 只有在 qfq result available AND `qfq_res.degraded == False` AND PIT previous close
  valid 时才允许输出有效值。
- `qfq_res.degraded == True` → `pit_gap = None`，`lane_b_status = PIT_ADJUSTMENT_DEGRADED`。
- **不 fallback** 到 raw previous close / factor=1 / latest qfq。
- 修正位置：`auction_history_semantics_validation.compute_lane_b()`（canonical Lane B owner），
  不在 runner 再造第二套判断。

## 5. ARCHITECTURE

- 新增 tracked runner：`experiments/pytdx_auction_history/full_market_member_fact_backfill.py`，
  职责仅 orchestration。
- **复用**（禁止复制算法）：`previous_trading_dates` / `run_single_observation` / official calendar /
  current canonical SH/SZ identity owner / `Instrument.listing_date` / MDAS /
  `AdjustmentFactorService` / `PytdxAdapter` / existing auction canonicalization /
  existing PIT qfq / existing volume/price validity / existing source-incomplete contract。
- **禁止复制**：09:25 extraction algorithm、pagination algorithm、canonicalization、
  PIT gap formula、qfq formula、volume unit formula。
- Execution partition = **TRADE BAR / TRADE DATE**（Auction 是横截面分析）：
  `for T in bar_dates: instruments = resolve(...listing_date<=T); for inst in instruments: run_single_observation(...)`。

## 6. PARTITION / OUTPUT

- 目录：`experiments/pytdx_auction_history/output/member_fact_120bar/2026-08-14/<run_id>/`
  - `manifest.json`（根）
  - `bars/YYYY-MM-DD/member_facts.jsonl` + `data_quality.json` + `partition_manifest.json`
- 一个 `instrument × trading bar T` → 一行 member fact（IDENTITY / SOURCE / AUCTION / LANE A /
  LANE B / LINEAGE-QUALITY）。
- **MEMORY**：不一次 120×5000 全放 RAM；每 bar resolve→run→write→reconcile→release。
- **RAW EVIDENCE**：不保存全天 transaction tape，只保存 exact 09:25 evidence + canonicalization counts。

## 7. RESUME

- 单位 = ONE BAR / ONE TRADE DATE。
- 只有 COMPLETED + metadata（trade_date/bar_index/baseline_sha/as_of/eligible）fully match 才 skip。
- RUNNING / FAILED / metadata mismatch → 整 bar 重跑；**不覆盖** completed metadata mismatch。

## 8. RECONCILIATION

- 根级：official_bar_count == 120，bar_dates unique == 120，latest_bar == 2026-08-14。
- 每 bar：eligible_instruments == member_rows_written；无 RUN_ERROR。
- COMPLETE + source incomplete == member_rows_written。否则 partition FAILED。

## 9. DATABASE / EVIDENCE

- Member Fact backfill = **FILE EVIDENCE ONLY**。
- 本轮不新建 member-fact DB table / migration / API / frontend / publication pointer。
- `Instrument.listing_date` 可按已批准 owner 更新；Auction historical rows 先不落生产业务表。

## 10. FAKE ORCHESTRATION / TESTS

- 正式 live 前 fake：3 trading bars × 3~5 instruments（1 中途 IPO / 1 source incomplete /
  1 qfq degraded）跑完整 partition / manifest / resume / reconciliation，必须 PASS。
- 新增 `tests/test_full_market_member_fact_backfill.py`：B1..B13。
- 原 Auction 语义测试（69）+ lifecycle + pytdx adapter 全 PASS。

## 11. LIVE FULL-MARKET RUN

- 本轮**不执行** 120 bars × full market live source fetch（待 ChatGPT 审核 120-bar calendar
  semantics / IPO filtering / qfq degraded fail-close / partition-resume / projection 后再启动）。

## 12. PRD

- `docs/prd/75-auction-analysis.md` L766：`120 valid trading days` → `120 valid trading bars
  （120 个官方交易日/session，非自然日）`。极小文字纠正，不改其它业务合同。

## 13. STATUS

- RUNNER = **READY**（tests PASS；live 120-bar 未运行）。
- DELISTING lifecycle = OUT OF SCOPE。
- FULL 120-BAR LIVE BACKFILL = NOT RUN（下一轮经审核后启动）。
