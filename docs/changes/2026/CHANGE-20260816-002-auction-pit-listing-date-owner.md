# CHANGE-20260816-002 — Auction PIT Listing-Date Owner (Round 3B-A1-R2)

## 1. WHY

Full-market 120D Auction Member Fact Backfill 需要 PIT listed population 的 **listing boundary**。
之前被 `PIT_INSTRUMENT_OWNER_GAP` 阻塞（Round 3B-A），因为 `get_active_a_share_instruments()`
是今日快照（无 `trade_date`、不含历史 PIT）。本轮只解决"股票在历史交易日 T 是否已上市"的
**上市边界**，作为 Member Fact population 的第一层 owner。

## 2. ACCEPTED FACT（不再重新研究）

- `pytdx get_finance_info()` 提供 `ipo_date`（pytdx raw 字段，YYYYMMDD int）。
- 当前 `Instrument.listing_date` 字段**已存在**（nullable Date），但 `instrument_seed` 写入 `None`，
  且 `PytdxAdapter.get_finance_info` 当前**丢弃** `ipo_date`。
- 问题不是 pytdx 无数据，而是 Panji 没有把 `ipo_date` 接进 `Instrument.listing_date`。

## 3. DECISION

- **Instrument 作为 listing lifecycle owner**（最小 Option A）：不新建 `all_a_share UniverseDefinition`，
  不新建 `all_a_share UniverseMembership`（现有 `UniverseMembership` 继续服务 index/style PIT membership）。
- authoritative source = `pytdx get_finance_info().ipo_date` → 落库 `Instrument.listing_date`。
- 新增独立薄 owner `backend/app/services/instrument_lifecycle_service.py`，职责仅：
  - `normalize_pytdx_ipo_date(value) -> date | None`（纯函数，严格，禁 fallback）
  - `is_listed_a_share_at(symbol, market, listing_date, trade_date)`（纯判定，**不含 status**）
  - `listed_a_share_filter_at(trade_date)`（SQL 过滤：stock_symbol_sql_filter + market in SH/SZ
    + listing_date IS NOT NULL + listing_date <= trade_date）
  - `resolve_listed_a_share_instruments_at(session, trade_date)`（resolver）
  - `sync_listing_dates(session, finance_provider, markets, dry_run)`（幂等、fail-closed 写入语义）
- **不修改** `auction_history_semantics_validation.py`、canonicalization contract、qfq、Scope membership、
  `UniverseMembership`、Review、Auction PRD 语义。

## 4. BOUNDARY — Instrument.status EXCLUDED

`Instrument.status` ∈ {active, inactive, suspended, delisted} 是 **operational state**，
受维护逻辑（如 absence-of-recent-bars → inactive）影响，不能恢复历史 PIT eligibility。
resolver **明确不**包含任何 `status` 过滤：
- `status='inactive'` 但 `listing_date <= T` → 仍包含（与 missing != zero / population≠eligibility 一致）。
- 停牌股票仍属 listed population；其当日 Auction fact 后续由 SOURCE_INCOMPLETE/MISSING/metric eligibility 处理。

## 5. LIMITATION — DELISTING_BOUNDARY_PENDING

本轮 **只** 实现 listing boundary。`resolve_listed_a_share_instruments_at` 当前合同为：
`LISTING_BOUNDARY_CORRECT` / `DELISTING_BOUNDARY_PENDING`。
不宣称 FULL PIT LIFECYCLE COMPLETE。

## 6. 120-DAY DELISTING IMPACT AUDIT（STEP 5）

窗口固定：`2026-02-13` → `2026-08-14`（120D Auction backfill 目标窗口）。

审计方法约束（来自任务）：
- `bars absence` / `status inactive` / `today list absence` 只能用于 **candidate discovery**，
  **不能**直接成为 authoritative `delisting_date`。
- 需要 authoritative source 才能确认 window 内 termination event。

结论（诚实标记）：
- **无 authoritative delisting source 可用**（Round 3B-A1-R 已证明：SH/SZ 官方 exchange 仅以
  人工可读公告页发布上市/退市信息，CNINFO webapi 为不可机器取数的 SPA，第三方 API 明确禁作 authority）。
- 因此 **`confirmed_window_delist_count` 无法在本轮用 authoritative source 确认**。
- 无法排除窗口内存在少量正式终止上市股票；若仅用 `listing_date <= T`，退市后日期会被错误包含。

DECISION（按任务 Decision Rule）：
- 不能断言 `confirmed_window_delist_count == 0` → 不能采用 Case A（直接放行）。
- 按 Case B/C：STOP，等待 ChatGPT 决定增加最小 `delisting_date` 还是采用显式 exclusion evidence。
- 本轮 **不**建设 delisting lifecycle system。

## 7. MIGRATION

**NONE**。本轮仅使用已存在的 `Instrument.listing_date` 字段，不新增 `delisting_date` migration，
不修改 `status` enum，不建 lifecycle history table，不建 all_a_share membership table。

## 8. WRITE SEMANTICS（sync_listing_dates，fail-closed，不静默覆盖）

| 情形 | 行为 |
|---|---|
| source `ipo_date` valid | 写 authoritative `listing_date` |
| source `ipo_date` missing (0/None/空/malformed) | 保持现有值，**不**写 None 覆盖 |
| existing `listing_date` == source | unchanged |
| existing `listing_date` != source（均非 None） | `LISTING_DATE_CONFLICT`，**不**覆盖，记录供决策 |
| existing is None + source valid | 写 source |

幂等：同一 symbol + 同一 `ipo_date` 重复运行无变化。

## 9. TESTS（PURE_UNIT_TEST=1，不连库）

新增 `backend/tests/test_instrument_lifecycle_service.py`（26 passed）：
- L1/L2 `normalize_pytdx_ipo_date`：valid int/string / 0 / None / 空 / 非数字 / 非法日历 / 过短 / 负 / bool
- L3 `status='inactive'` 但 listing<=T → included（证明不含 status）
- L4 listing==T → included；L5 listing>T → excluded
- L6 listing None → excluded（不默认 include）
- L7 非股票（指数 SH000001）/ BJ → excluded
- L8 existing None + valid pytdx → update
- L9 existing == source → unchanged
- L10 existing != source → conflict（不静默覆盖）
- resolver 集成（内存 fake session）：new-listing 前排除 / inactive 仍包含 / BJ 排除
- sync 幂等 + dry_run rollback（内存 fake 还原）

现有 Auction 69-test suite：**69 passed**（无回归）。

## 10. CHANGED FILES

- `backend/app/core/pytdx_adapter.py`（仅扩展 `get_finance_info` 解析 `ipo_date_raw`，复用现有 managed lifecycle）
- `backend/app/services/instrument_lifecycle_service.py`（**新增**）
- `backend/tests/test_instrument_lifecycle_service.py`（**新增**）
- `docs/changes/INDEX.md`（追加本条）

## 11. REAL PROOF STATUS

- 纯单元测试：26 passed（normalization / resolver / sync 语义）✅
- 现有 Auction 69 回归：69 passed ✅
- `git diff --check`：PASS ✅
- **真实 pytdx finance-info 对 >=10 SH/SZ symbols 的实拉取 + earliest/middle/latest resolver count
  + 120D 新上市计数**：**未执行**（本地/CI 禁止连库与 pytdx 生产取数；按 AGENTS.md 须远程验证库
  或用户授权后执行）。本轮代码已就绪，待 ChatGPT 审核 + 授权运行环境后补真实 PROOF。

## 12. NEXT

等待 ChatGPT 独立审核。审核后决定：
- 是否授权真实运行 `sync_listing_dates` + earliest/middle/latest resolver proof；
- 以及 delisting boundary 的最小方案（增加 `delisting_date` 字段 vs 显式 exclusion evidence）。

## 13. STATUS

`implemented_unconfirmed`（代码+纯单元测试 PASS；真实 pytdx 拉取与 120D 窗口 resolver 计数
待授权运行环境；无 migration、未连生产、未 120-day backfill、Auction runner 未改）。

---

## 14. Round 3B-A1-R2A — Runtime Contract Closure (corrective)

### 14.1 DEFECT

`instrument_lifecycle_service.py` 的 `PytdxFinanceInfoProvider` Protocol 与调用处
错误地假设 `get_finance_info` 是 **async**：

```python
class PytdxFinanceInfoProvider(Protocol):
    async def get_finance_info(self, symbol: str) -> dict[str, Any] | None: ...

# 调用处
info = await finance_provider.get_finance_info(inst.symbol)
```

但生产 owner `PytdxAdapter.get_finance_info` 是 **同步** I/O（pytdx `self.api.get_finance_info`
阻塞调用）。真实运行时：

- `await dict` → `TypeError`（dict 不是 awaitable）；
- 该 `TypeError` 被 broad `except Exception` 吞没，伪装成 `source_error`——
  把 **implementation contract defect** 错误标记为 source 失败。

### 14.2 CORRECTED CONTRACT

- `PytdxFinanceInfoProvider` Protocol 改为同步接口（`def get_finance_info`，与 `PytdxAdapter` 一致）。
- 调用处改用 `asyncio.to_thread`，与 `instrument_share_sync_service` 调用同一 Pytdx owner 的方式一致：

```python
info = await asyncio.to_thread(
    finance_provider.get_finance_info, inst.symbol
)
```

- **不修改** `PytdxAdapter.get_finance_info`（同步接口已是现有 canonical owner）。
- **不**引入 `maybe_await` / `inspect.isawaitable` / sync-async dual provider / adapter framework /
  worker pool / semaphore。生产合同就是 `SYNC PytdxAdapter + asyncio.to_thread`。

### 14.3 TEST FIX

- `_FakeFinanceProvider.get_finance_info` 改为同步 `def`（与真实生产签名一致）。
- 新增 runtime-contract regression（锁定合同，防回归成 `await provider.get_finance_info`）：
  - **R1**：sync fake provider 返回 `{"ipo_date_raw": 19910403}` → `await sync_listing_dates(...)`
    不抛异常、`finance_success == 1`、`listing_date_inserted == 1`、`source_error == 0`。
  - **R2**：sync provider 对指定 symbol 抛 `RuntimeError("source failure")` → `source_error == 1`，
    不影响其余股票继续同步。
  - **real owner signature**：`inspect.iscoroutinefunction(PytdxAdapter.get_finance_info) is False`
    （不联网，仅断言生产 owner 合同为同步）。
- 删除 `_FakeSession` 重复的 `async def execute` 定义（test hygiene，仅保留最终实现）。

### 14.4 REGRESSION

- lifecycle 单测：**29 passed**（原 26 + R1 + R2 + signature）×2 运行一致 ✅
- Auction 非 PG 单测：**369 passed**（覆盖历史语义回归，无回归）✅
- pytdx adapter 单测：`test_pytdx_adapter_minute_aware/ fetch_count/ market_from_code_bj` **20 passed** ✅
- `git diff --check`：PASS ✅

### 14.5 REAL PYTDX 10-SYMBOL READ-ONLY PROOF（Round 3B-A1-R2A）

代码修复后执行 READ-ONLY source proof（**不写 Instrument / 不写 production DB / 不跑全市场 sync**）：
仅验证 `PytdxAdapter.get_finance_info` → `ipo_date_raw` → `normalize_pytdx_ipo_date`。

样本 12 只（覆盖老股票 / 主板 / 创业板 / 科创板）：

| symbol | market | finance | ipo_raw | normalized |
|---|---|---|---|---|
| 600000 | SH | YES | 19991110 | 1999-11-10 |
| 601398 | SH | YES | 20061027 | 2006-10-27 |
| 000001 | SZ | YES | 19910403 | 1991-04-03 |
| 000002 | SZ | YES | 19910129 | 1991-01-29 |
| 300001 | SZ | YES | 20091030 | 2009-10-30 |
| 300750 | SZ | YES | 20180611 | 2018-06-11 |
| 688001 | SH | YES | 20190722 | 2019-07-22 |
| 688981 | SH | YES | 20200716 | 2020-07-16 |
| 600519 | SH | YES | 20010827 | 2001-08-27 |
| 002594 | SZ | YES | 20110630 | 2011-06-30 |
| 688041 | SH | YES | 20220812 | 2022-08-12 |
| 300059 | SZ | YES | 20100319 | 2010-03-19 |

统计：`attempted = 12`、`finance_success = 12`、`valid_ipo_date = 12`、`missing_ipo = 0`、`source_error = 0`。

证明：生产 Pytdx owner（同步 `get_finance_info` + `asyncio.to_thread`）在真实网络上可用，
`ipo_date_raw` 全部成功归一化为合法 `listing_date`。

### 14.6 CHANGED FILES（Round 3B-A1-R2A）

- `backend/app/services/instrument_lifecycle_service.py`（Protocol 改同步 + `asyncio.to_thread` 调用）
- `backend/tests/test_instrument_lifecycle_service.py`（sync fake provider + R1/R2/signature regression + 删除重复 execute）
- `docs/changes/2026/CHANGE-20260816-002-...md`（本 §14 corrective note）

INDEX 不改。

### 14.7 STATUS（Round 3B-A1-R2A）

`implemented_unconfirmed`（runtime contract 已修正 + 纯单测/整合回归全 PASS + 真实 pytdx READ-ONLY
proof 全 12 样本成功；DB listing sync 与 120D 窗口 resolver 计数仍待 ChatGPT 审核后授权运行）。
