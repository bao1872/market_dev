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

## 14. Round 3B-B1 — LIVE-PATH INTEGRITY CLOSURE（2026-08-16）

最小 live-wiring / evidence 修正，不重新设计 120-bar / IPO / source / canonicalization / qfq / delisting / Scope。

### FIX 1 — REAL INSTRUMENT ID
- `_to_sample_inst` 改用真实 ORM 主键 `Instrument.id`（UUID），不再用不存在的 `instrument_id`。
- 不新增 alias/property；真实 `Instrument` ORM contract regression 覆盖。

### FIX 2 — REAL PYTDX ADAPTER WIRING
- `_bars_loop(session, adapter_for_run)` 显式接收 adapter；所有 partition 调用用它。
- 注入路径传 injected adapter；真实路径传 `with PytdxAdapter() as real_adapter` 的 real_adapter。
- 禁止 global adapter fallback；monkeypatch PytdxAdapter 为 sentinel context manager，
  断言 run_single_obs 收到 sentinel，真实 branch wiring 被测试覆盖。

### FIX 3 — POPULATION CONTRACT
- 简化合同：`Backfill population(T) = CURRENT CANONICAL SH/SZ A-SHARE SET ∩ listing_date <= T`。
- 新增 experiment-local `resolve_backfill_population_at(session, T)`：复用
  `feature_snapshot_service.get_active_a_share_instruments`（current canonical anchor）+
  SQL（`Instrument.id IN canonical AND market in SH/SZ AND stock_symbol_sql_filter AND
  listing_date IS NOT NULL AND listing_date <= T`）。
- 不修改 `instrument_lifecycle_service`；status 不作历史 lifecycle rule。

### LISTING-DATE COVERAGE PRE-FLIGHT
- 真实 live run 前 `check_listing_date_coverage`：total/present/missing；missing==0 否则
  `STOP` + `LISTING_DATE_COVERAGE_GAP`，不自动 fallback，本轮不全市场 sync。

### FIX 4 — BOARD CONTRACT
- 冻结 labels：`SH_MAIN / SZ_MAIN / CHINEXT / STAR`；创业板 300/301/302...→`CHINEXT`（禁 `SZ_GEM`）。
- 四类 board 测试锁定。

### FIX 5 + ADJUSTMENT LINEAGE — LANE B PROJECTION
- `previous_close_raw = lane_b["raw_close_Tm1"]`；`previous_close_pit_qfq = lane_b["qfq_close_Tm1"]`。
- `adjustment_as_of`（= target，该 historical T 的 PIT qfq anchor）与 `adj_factor_hash` **分开**；
  `compute_lane_b` 输出 `adjustment_as_of` + `adj_factor_hash`；runner 各自投影。

### FIX 6 — QFQ DEGRADED DIRECT TEST
- `test_auction_history_semantics_validation.py` 新增直接 `compute_lane_b` regression：
  degraded=True → `PIT_ADJUSTMENT_DEGRADED` + `pit_gap=None` + raw/qfq close + adjustment lineage preserved；
  healthy control → `COMPUTED` + pit_gap valid。

### FIX 7 — COMPLETED RESUME SAFETY（三态）
- `_partition_resume_decision(existing_manifest, expected_metadata)`：
  `NO_EXISTING→RERUN`；`COMPLETED+match→SKIP`；`COMPLETED+mismatch→BLOCK`（raise
  `CompletedPartitionMetadataMismatch`）；`RUNNING/FAILED→RERUN`。
- runner 顺序：先 resolve population 得 expected_eligible，再读 existing manifest 决定。

### FIX 8 — ROOT MANIFEST AGGREGATION
- `_accumulate_partition_quality(root, manifest, data_quality)` 累计 eligible/member_rows/
  source/canonical aggregate/lane counts/pit_gap counts；fresh 与 resume-skip 的 completed bar 都累计。
- resume 后 root totals 与首次一致（regression 覆盖）。

### FIX 9 — PARTITION RECONCILIATION
- 机械 reconcile：`member_rows_written == sum(frozen source)`；`UNKNOWN source → FAILED`；
  `COMPLETE count == sum(frozen canonical)`；source-incomplete canonicalization 必须 None；
  `RUN_ERROR → FAILED`。不引入新业务 status。

### FIX 10 — RUNTIME CODE SHA
- 移除模块级 stale `BASELINE_SHA`；`run_backfill(code_sha=...)` 用 runtime code SHA（live = `git rev-parse HEAD`；
  测试显式注入）。manifest/rows/resume 统一 `code_sha`。`code_sha` 改变 → existing COMPLETED 必须 BLOCK。

### VERIFICATION（全部 PASS）
- backfill 新测试 **23** collected/passed ×2；Auction semantics **71** collected/passed ×2（原 69 + 2 FIX 6）；
  lifecycle 29 + pytdx adapter 2 = 31 passed；git diff --check PASS。
- LIVE 120-BAR FULL-MARKET = **NOT RUN**。

### CHANGED FILES（Round 3B-B1）
- `M experiments/pytdx_auction_history/full_market_member_fact_backfill.py`
- `M experiments/pytdx_auction_history/tests/test_full_market_member_fact_backfill.py`
- `M experiments/pytdx_auction_history/auction_history_semantics_validation.py`（仅 adjustment_as_of lineage）
- `M experiments/pytdx_auction_history/tests/test_auction_history_semantics_validation.py`（仅 FIX 6 direct qfq degraded regression）

PRD 不再修改（120-bar wording 已正确）。

## 15. Round 3B-D — PERFORMANCE + EXECUTION GOVERNANCE CLOSURE（2026-08-16）

性能根因确认：validation pipeline（`run_single_observation` → `fetch_full_day_transactions_paginated`
从 offset=0 逐页抓全天逐笔 + full-day volume evidence + per-symbol MDAS get_bars×2）适合 source
validation，不适合 600k+ 历史 Member Facts。正式 backfill **不再调用** `run_single_observation()`
/ `fetch_full_day_transactions_paginated`（保留为 source-validation reference implementation，不删除）。

### PYTDX CONNECTION CONTRACT（锁死）
- ONE PROCESS = ONE `PytdxAdapter` INSTANCE = ONE NORMAL HEALTHY CONNECTION。
- 禁止 per-stock/per-bar/per-function `PytdxAdapter()`、nested `with`、`get_pytdx_adapter()` 第二条连接。
- 禁止 multiprocessing / pytdx process/thread pool / 并发共享多 Tdx sockets（本轮不考虑多进程方向）。
- [B1] `connect()` 幂等：已连接则 NO-OP（不重复扫描 server list / 新建 socket）。
- [B2] 只读诊断：`successful_connect_count` / `reconnect_count`（首次连接 success +1；真实 source
  failure 后 disconnect→reconnect success 再 +1 且 reconnect_count +1）。
- [B3] `get_history_transaction_page(symbol, trade_date, offset, count)` thin managed owner：
  market_from_code / YYYYMMDD / _io_lock / 复用已有 managed connection / retry-reconnect /
  context-rich RuntimeError；不做 canonicalize / 解释 09:25 / volume / Auction business logic。

### TARGETED 09:25 SOURCE FETCH（kernel）
- 新增 `auction_member_fact_backfill_kernel.py`：
  - `fetch_auction_0925_targeted(adapter, symbol, trade_date, offset_hint=None)`：
    hint-first + exponential search + boundary binary search，只覆盖 09:25:00～09:25:59 window，
    不线性扫全天；C3 边界安全读取 adjacent page（previous/target/next）按 raw record identity 去重。
  - `build_historical_member_fact(...)` 纯函数：target source → canonicalization → Lane A → Lane B
    → Member Fact；不 new PytdxAdapter / 不调用 MDAS / 不查 DB / 不分页全天。
- 冻结 backfill-specific `source_status`（不谎称 full-day COMPLETE）：
  `TARGET_WINDOW_COMPLETE`（仅表示 09:25 minute 被完整 bracket/覆盖）/
  `SOURCE_EMPTY` / `SOURCE_ERROR` / `TARGET_SEARCH_STALLED` / `TARGET_SEARCH_LIMIT_REACHED`。
- 只有 `source_status == TARGET_WINDOW_COMPLETE` 才允许 canonicalize。
- C6/C7 offset hint cache：`offset_hints_by_symbol`（上一 bar resolved offset → 下一 bar warm
  probe）；每 bar COMPLETED 后 atomic 写 `offset_hints.json`，resume 加载。hint 只影响 transport
  efficiency，不改变 source/canonical result。

### BATCH MDAS
- 每 bar 只做两次 batch contract：`adj=none ×1` + `adj=qfq ×1（adjustment_as_of=T）`，
  `completed_only=True, allow_backfill=False`（strict DB-only，不触发第二个 pytdx 连接）。
- [E3] `get_bars_batch(... allow_backfill=True default)` 完整把合同传到 `_build_daily_aggregation`
  （新增 `allow_backfill` 参数）：`allow_backfill=False` 且 DB historical daily 数据不足时
  **不得调用 external daily provider**（fetch_daily_bars zero calls），按 MDAS fail-closed 返回
  empty/degraded evidence。
- [E4] batch 诊断写入 partition data_quality：`raw_batch_repository_query_count`（≈2）/
  `qfq_batch_repository_query_count`（≈3）/ `raw_batch_symbol_count` / `qfq_batch_symbol_count`。

### POPULATION LOAD-ONCE + LISTING COVERAGE GATE
- `load_population_once(session)`：startup 一次读取 CURRENT CANONICAL SH/SZ identity +
  listing_date 放入内存；120 bars 只做 `filter_population_at` in-memory `listing_date <= T`。
- 修复 gate denominator：先统计 `current_shsz_total`（current canonical AND SH/SZ AND stock-symbol
  identity），再统计 `listing_date_present / listing_date_missing`；不能先过滤 listing 再证明 missing=0。
- missing symbols 显式写入 root manifest（`listing_date_unavailable_count` /
  `listing_date_unavailable_symbols`），不 silent exclusion；不 first-bar/today fallback / guess IPO。
- 已知真实 DB 约 6 个 SH/SZ current identities 仍无 listing_date，不阻塞研究。

### STREAM OUTPUT + PROGRESS + ATOMIC FINALIZE
- `member_facts.jsonl.tmp` 每完成一个 Member Fact stream append（不整 bar 放 RAM）。
- 每 100 stocks 或 30 秒 atomic 写 `progress.json`（trade_date/bar_index/eligible/processed/percent/
  elapsed_seconds/pytdx_requests/pytdx_requests_per_symbol/source/canonical/lane_b/current_symbol 等）。
- COMPLETED：fsync → `.tmp` atomic rename → `member_facts.jsonl` → partition_manifest.status=COMPLETED；
  失败 `.tmp` 保留、status=FAILED；resume 仍 whole-bar rerun。

### RUN-LEVEL LOCK
- `run.lock`（O_CREAT | O_EXCL）：同一 run_id 只允许一个 writer；owner PID active →
  `RUN_ALREADY_ACTIVE`；stale（owner 不存在）须显式 `--recover-stale-lock` 才继续。

### TRACKED CLI
- 彻底禁止 untracked launcher（`_live_backfill_runner_3bc.py`）。
- `full_market_member_fact_backfill.py` argparse：`--mode benchmark|live` / `--as-of` / `--run-id` /
  `--benchmark-bars` / `--benchmark-symbols` / `--output-root` / `--recover-stale-lock` /
  `--require-listing-coverage`。`code_sha` 自动取 `git rev-parse HEAD`，禁止 CLI 伪造。
- CLI 只允许一次 `with PytdxAdapter()`；健康 run 诊断 `adapter_instance_count=1 /
  successful_connect_count=1 / reconnect_count=0`。

### PERFORMANCE INSTRUMENTATION（Part K）
- root/partition manifest 增加：`pytdx_request_count` / `pytdx_target_search_cold_count` /
  `pytdx_target_search_hint_count` / `avg_requests_per_symbol` / `max_requests_per_symbol` /
  `successful_connect_count` / `reconnect_count` / `raw_mdas_batch_queries` /
  `qfq_mdas_batch_queries` / `processing_seconds` / `symbols_per_second`。仅 performance evidence，
  不改变业务结论。

### TESTS（Part J：P1-P22 + 回归）
- 新增 `backend/tests/test_pytdx_adapter_v3.py`：P1 connect 幂等 / P2 healthy 连续 N 页
  successful_connect_count==1 reconnect_count==0 / P3 page 失败走 managed reconnect-retry。
- 新增 `experiments/pytdx_auction_history/tests/test_auction_member_fact_backfill_kernel.py`：
  P4 cold search 找到 09:25 / P5 跨 page boundary 完整 / P6 hinted==cold raw records /
  P7 warm 请求数显著低于 cold / P8 仅 TARGET_WINDOW_COMPLETE 才 canonicalize /
  P9 source empty 非 business zero。
- 更新 `experiments/pytdx_auction_history/tests/test_full_market_member_fact_backfill.py`（B1-B13 +
  FIX 1-10 适配新接口 + P10-P22）：
  P10 kernel 不调用 fetch_full_day_transactions_paginated / P11 无 full-day volume evidence /
  P12 每 bar 只 MDAS batch none×1+qfq×1 / P13 batch allow_backfill=False DB missing 不触发
  external provider / P14 population load-once / P15 coverage denominator / P16 listing 缺失显式记录 /
  P17 stream tmp append / P18 atomic rename / P19 RUN_ALREADY_ACTIVE / P20 stale lock 显式 recover /
  P21 resume 加载 offset hints / P22 resume fresh/skipped root totals 一致。
- 回归：Auction semantics（69）+ kernel（P4-P9）+ backfill（B+FIX+P，36）+ pytdx adapter（P1-P3 等）+
  instrument lifecycle（29）+ MDAS batch（strict DB-only 回归）全部 PASS。

### VERIFICATION
- experiments/tests 全目录 **113 passed**；backend pytdx/MDAS/lifecycle 回归 **77 passed, 4 skipped**。
- `git diff --check` PASS。
- LIVE 120-BAR FULL-MARKET = **NOT RUN**（待 ChatGPT independent audit 后才允许）。
- PRD：NONE（业务语义无变化；120-bar wording 已正确）。

### CHANGED FILES（Round 3B-D）
- `M backend/app/core/pytdx_adapter.py`（B1 幂等 / B2 诊断 / B3 managed history page）
- `M backend/app/services/market_data_aggregation_service.py`（get_bars_batch → _build_daily_aggregation allow_backfill）
- `A experiments/pytdx_auction_history/auction_member_fact_backfill_kernel.py`
- `M experiments/pytdx_auction_history/full_market_member_fact_backfill.py`
- `A backend/tests/test_pytdx_adapter_v3.py`
- `A experiments/pytdx_auction_history/tests/test_auction_member_fact_backfill_kernel.py`
- `M experiments/pytdx_auction_history/tests/test_full_market_member_fact_backfill.py`
- `M docs/changes/2026/CHANGE-20260816-003-full-market-120-bar-backfill.md`

不要修改：Instrument model / Scope / Review / frontend / API；不删除
`auction_history_semantics_validation.py` 的 source-validation functions。

## 16. Round 3B-D1 — TARGETED SOURCE CORRECTNESS + WARM HINT CLOSURE（2026-08-16）

3B-D canary FAIL 后的窄修复轮。只修 transport/search layer 正确性与性能，
**不修改 canonical business contract**（CANONICAL_AUCTION_TIMES / LOT ×100 / amount /
Lane A / Lane B / qfq degraded / population / listing_date / MDAS / Review / Scope / frontend 均不变）。

### ROOT CAUSE 1 — REAL PYTDX TIME = HH:MM（kernel 曾假设 HH:MM:SS）
- 真实 pytdx historical transaction `time` 是 **HH:MM**（如 "09:25"），不是 "09:25:00"。
- 新增 transport-level `_normalize_source_minute(value) -> str | None`：只用于 search/ordering，
  **不改写 raw source record**（raw evidence 原样保留）。
  - `"09:25" → "09:25"`；`"09:25:00" → "09:25"`；`"09:25:37" → "09:25"`；
    合法 HH:MM / HH:MM:SS → HH:MM；None / empty / malformed / invalid clock → None。
- 冻结 `TARGET_MINUTE = "09:25"` 为 raw string ordering owner；废弃
  `TARGET_WINDOW_START="09:25:00"` / `TARGET_WINDOW_END="09:25:59"` 作为 ordering owner。
- `_page_time_range` / `_page_entirely_before` / `_page_entirely_after` / `_before`
  全部基于 normalized minute 排序：entirely-before = max < "09:25"；entirely-after = min > "09:25"；
  contains = min <= "09:25" <= max。
- non-empty page 无任何可解析 source time → **fail closed** `TARGET_SEARCH_STALLED` +
  `error_code/error_message = INVALID_OR_UNORDERABLE_SOURCE_TIME`，不得假定 before/after/empty。
- `_collect_window_records` 按 `_normalize_source_minute(record["time"]) == TARGET_MINUTE`
  收集整个 09:25 minute；随后仍走 existing `_normalize_raw_transaction` →
  `classify_transaction_time` → `canonicalize_auction_0925`。kernel 不复制业务判断。

### ROOT CAUSE 2 — CACHE HIT 被误计为 Pytdx request
- `_fetch()` 原为 `counter += 1; if offset in page_cache: return` → page_cache 命中
  错误计入 `page_count`。
- 修正为 `if offset in page_cache: return page_cache[offset]`，然后才检查 budget / counter += 1 /
  `adapter.get_history_transaction_page(...)`。
- `Targeted0925Result.page_count` 现在 = **REAL adapter page calls**；`MAX_TARGETED_PAGES`
  也只限制真实 source requests。新增断言 `page_count == len(adapter.request_log)`，
  重复访问 cached offset 不增加两者。

### ROOT CAUSE 3 — WARM HINT 无真正 local fast path
- 删除「hint 在 before side 就 lo=0 重新 full binary search」的 warm-path 行为。
- **F1**：`offset_hint is not None` 时不得一开始无条件 `_fetch(0)`，从 hint 页开始。
- **F2**：`H = floor(hint/PAGE_SIZE)*PAGE_SIZE`，local bracket：
  - Fast path A：`_before(H) and not _before(H-P)` → boundary = H；
    收集 H-2P / H-P / H（H、H-P 已在 cache），正常 REAL requests <= 3。
  - Fast path B：`not _before(H) and _before(H+P)` → boundary = H+P；
    收集 H-P / H / H+P，正常 REAL requests <= 3。
- **F3**：local bracket 不成立时按方向从 H 向外扩展（H±P、H±2P、H±4P…），建立局部
  bracket 后只在 bracket 内 binary search。**禁止** hint 偏差即重置 offset=0 全局搜索。
  仅 hint 完全不可用 / 越界 / 无法建立局部 bracket 才 cold fallback（offset=0 + exponential + binary）。
  正确性优先于 <=3：大漂移允许 >3，但稳定日 warm 必须 <=3。
- **TARGET RECORD COMPLETENESS**：boundary B = 最小 offset（page 已在 target minute 前
  或 source exhausted）；收集 B-2P / B-P / B 并 raw identity（time/price/vol/buyorsell）去重；
  不得找到第一条 09:25 即停。
- **SEARCH STALL DETECTION**：内容级 SHA-1 指纹 `_page_content_fingerprint`（整页
  (time,price,vol,buyorsell) 哈希），替换原「首尾 time + 长度」指纹——真实数据连续 page
  可整页同分钟，首尾 time 相同造成假 TARGET_SEARCH_STALLED；内容哈希只在 adapter 对
  不同 offset 返回字面相同内容（病态重复）时触发停滞。

### ROOT CAUSE 4 — OFFSET HINT 只在整 run 结束后持久化
- 每个 partition `status == COMPLETED` 且 reconciliation 完成后，立即 **atomic write**
  `offset_hints.json`（含 run_id / as_of / code_sha / hints），再进入下一 bar。
- atomic 写 = `.tmp`（fsync）→ `os.replace`，避免半写文件被 resume 读到。
- run 最后可再写一次，但不允许只有 final write：bar 57 完成、bar 58 中途进程退出后，
  resume 仍能加载 bar 57 最新 hint。

### TESTS（PART I/J：T1-T10 + hint resume）
- 重写 `tests/test_auction_member_fact_backfill_kernel.py` 为真实 HH:MM source shape：
  - T1 REAL_HHMM_CANONICAL：raw `{"time":"09:25",price:10,vol:100}` → targeted.records 包含、
    CANONICAL、`auction_volume_shares == 100*100`。
  - T2 HHMM_BOUNDARY：09:30/09:25/09:24 minute ordering；09:25 page 不得判 entirely_before。
  - T3 HHMM_CROSS_PAGE：09:25 raw records 跨两 page 完整且不重复（不同 price/vol）。
  - T4 HHMMSS_COMPATIBILITY：09:25:00 仍被 existing semantics contract 接受。
  - T5 CACHE_NOT_REQUEST：cache hit 不增加 page_count / adapter.request_log。
  - T6 COLD_EQUALS_WARM：real-style HH:MM pages cold records == warm records。
  - T7 EXACT_BOUNDARY_HINT：offset_hint == prior boundary → REAL adapter calls <= 3。
  - T8 ONE_PAGE_SHIFT_HINT：hint == boundary±P → <= 3。
  - T9 LARGE_DRIFT_SAFE：hint 漂移 >1 page 允许 >3，但 records == cold 且 <= MAX_TARGETED_PAGES。
  - T10 CANONICALIZATION_NOT_100_PERCENT_NO_VOLUME：real-style positive 09:25 fixture 必须 CANONICAL。
- `tests/test_full_market_member_fact_backfill.py` 新增 mid-run interruption resume 测试
  （PART J / P23）：bar1 COMPLETED 后 per-partition atomic hint 已落盘；bar2 抛 BaseException
  模拟进程中断 → hints 保留；resume 加载后 bar2 first symbol `used_hint == True`。

### VERIFICATION（全部 PASS）
- experiments（semantics + kernel + runner）全目录 **120 passed**；kernel tests **12 passed ×2**。
- backend pytdx adapter + MDAS + instrument lifecycle **87 passed, 4 skipped**。
- `git diff --check` PASS。
- LIVE 120-BAR FULL-MARKET = **NOT RUN**（待 canary gate + ChatGPT audit）。

### CHANGED FILES（Round 3B-D1）
- `M experiments/pytdx_auction_history/auction_member_fact_backfill_kernel.py`
  （minute normalization / page ordering / window collection / request counter / warm local
  fast path / content fingerprint / unorderable fail-closed）
- `M experiments/pytdx_auction_history/full_market_member_fact_backfill.py`
  （per-partition atomic offset hint persistence）
- `M experiments/pytdx_auction_history/tests/test_auction_member_fact_backfill_kernel.py`
  （T1-T10 真实 HH:MM shape）
- `M experiments/pytdx_auction_history/tests/test_full_market_member_fact_backfill.py`
  （P23 mid-run interruption hint resume）
- `M docs/changes/2026/CHANGE-20260816-003-full-market-120-bar-backfill.md`

### PRD
- **NONE**（canonical business contract 未变化；仅 transport/search layer 修复）。

不要修改：Instrument model / Scope / Review / frontend / API；不删除
`auction_history_semantics_validation.py` 的 source-validation functions。

## 17. Round 3B-D2 — TARGET-PAGE-FIRST PERFORMANCE CLOSURE（2026-08-16）

### WHY THIS ROUND EXISTS

3B-D1 warm 算法即使优化到理想状态也接近 3 physical Pytdx requests / symbol 的成本下限；
真实 canary 实测 Bar2 avg=3.04、Bar3 avg=3.22，投影 5190×120 ≈ 20h，**超过 Performance
Contract（full 120-bar <= 12h）**。继续把 3.22 优化成 3.00 没有工程价值——**boundary-first
的成本下限本身不足以满足 12h scale budget**。

因此本轮不做 F3 微调，而是结构性改变 warm fast path：**boundary-first → target-page-first**，
把 warm anchor 从「上一交易日 boundary」改为「上一交易日含 09:25 block 的 page
（target_page_offset）」，稳定日 warm 目标降到 1~2 physical requests。

### ARCHITECTURE（不修改 canonical business contract）

- **kernel**（`auction_member_fact_backfill_kernel.py`）：
  - warm fast path 从 H = target_page_offset 开始，按 C1-C5 局部判定：
    - C2 SINGLE：单页完整跨 09:25（min < "09:25" < max）→ 1 request；
    - C3 ADJACENT：09:25 在页边沿 → 读相邻 1~2 页 → 2~3 requests；
    - C4 BIDIRECTIONAL：整页都是 09:25 → 两侧各读 1 页 → <=3 requests（正确性优先）；
    - C5 DRIFT：H 整页 before/after 或为空 → 向目标方向局部扩展 1~2 页 → 2~3 requests。
  - fast path 无法 complete 时才进入 **existing boundary algorithm（F2/F3 local bracket +
    boundary binary + B-2P/B-P/B completeness）作为 fallback**（de7998b 保留，不删除）。
  - 新增 `Targeted0925Result.target_page_offset`（下一交易日 warm anchor）+ `search_mode`
    （TARGET_PAGE_SINGLE / ADJACENT / BIDIRECTIONAL / DRIFT / BOUNDARY_FALLBACK / COLD）；
    `resolved_offset` 保留原 boundary 语义。`target_page_offset` 从本次已缓存 pages 中选取，
    不为计算 hint 新增 source request。
- **runner**（`full_market_member_fact_backfill.py`）：
  - `offset_hints.json` 升级为 **v2 versioned structure**：
    `{"version": 2, "hints": {"000001": {"target_page_offset": ..., "boundary_offset": ...}}}`；
    `target_page_offset` 供下一交易日 warm fast path，`boundary_offset` 仅 fallback/debug evidence。
  - **向后兼容**：v1 `symbol -> int` 可解释为 legacy boundary hint，第一次走 boundary fallback，
    完成后升级成 v2 hint，不破坏 resume。
  - runner 聚合 `search_mode_distribution`（six counts），与 `page_count`（REAL adapter calls）
    一起作为性能 evidence。

### WHY NOT 3.22 → 3.00

boundary-first 无论怎么调优，warm 每 symbol 至少要先定位 boundary 再读 target 页，
成本下限接近 3 requests；把 3.22 优化成 3.00 只省 ~7%，仍不足以把 5190×120 投影压到
12h 以内。只有把 warm 请求数降到 1~2 量级（target-page-first）才能真正满足 scale budget。

### TESTS（PART H：T1-T10，deterministic，不 ×2）

- T1 SINGLE 单页 09:24/09:25/09:26 → 1 request + TARGET_PAGE_SINGLE
- T2 CROSSES 页跨 09:25 无 record → 1 request + records=[] + TARGET_WINDOW_COMPLETE
- T3 EDGE target 在边沿 → adjacent → 2 requests + records 完整
- T4 BIDIRECTIONAL 整页 ==09:25 → 两侧 adjacent → <=3 requests + records 完整
- T5/T6 +1/-1 DRIFT → 2 requests + records 完整
- T7 LARGE DRIFT → boundary fallback → result == cold result
- T8 fallback 后 target_page_offset 来自已有 cache，无额外 request
- T9 hint v1 legacy int → resume 可读 → fallback → 升级成 v2 target-page hint
- T10 mid-run resume → 继续使用 persisted target_page_offset

### CHANGED FILES（Round 3B-D2）

- `M experiments/pytdx_auction_history/auction_member_fact_backfill_kernel.py`
  （target-page-first warm path C1-C5 + target_page_offset + search_mode + boundary fallback 保留）
- `M experiments/pytdx_auction_history/full_market_member_fact_backfill.py`
  （offset_hints v2 versioned structure + backward compat + search_mode 聚合）
- `M experiments/pytdx_auction_history/tests/test_auction_member_fact_backfill_kernel.py`（D2-T1..T8）
- `M experiments/pytdx_auction_history/tests/test_full_market_member_fact_backfill.py`（D2-T9..T10）
- `M docs/changes/2026/CHANGE-20260816-003-full-market-120-bar-backfill.md`

### PRD
- **NONE**（canonical business contract 未变化；仅 transport/search layer 结构性性能优化）。

### STATUS
- Round 3B-D2 运行结果（micro / canary / projected full runtime）留在远程 evidence manifest，
  不写入本 CHANGE。FULL 120-BAR LIVE BACKFILL = **NOT RUN**（待 ChatGPT audit）。