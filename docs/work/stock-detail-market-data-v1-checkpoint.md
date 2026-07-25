# 个股详情布局与行情唯一真源修复 — Checkpoint

> 分支：fix/stock-detail-market-data-v1
> 基线 SHA：a5e380de47cf86c8157db84ca8dbde9db7fca9d7
> 变更编号：CHANGE-20260724-004
> 状态：代码完成，待集成

---

## 修改前调用链

```
详情页入口
├── StockDetailPage
│   ├── useStockResearchData
│   │   ├── useChartSnapshot (chart-snapshot API)
│   │   └── useRealtimeQuote (quote API) ← 已删除
│   ├── ChartCanvas (bars from chart-snapshot)
│   └── QuoteCard (price from quote API) ← 已改为 chart-snapshot
└── MessagesPage
    └── 手拼 /stock/:symbol ← 已改为 buildStockDetailUrl
```

**问题：**
1. 详情页同时调用 `/chart-snapshot` 和 `/quote`，两个数据源 `as_of` 不一致
2. 15m/1h 实时尾部从 1m 聚合，而非 Pytdx 原生周期
3. 1d partial daily 从 1m 聚合，而非 Pytdx 原生日线
4. 1w/1mo 先聚合再补今日，导致最后一根 bar 不含今日数据
5. 1w/1mo quote 使用聚合 OHLC，非当日行情
6. 实时空结果返回普通 db 状态，无 stale 标记
7. PytdxAdapter 使用 Lock 不可重入，嵌套调用自锁
8. freshness 无明确状态机
9. 详情页布局不区分 direct/market/watchlist 来源
10. MessagesPage 手拼 /stock/:symbol

---

## 修改后数据链

```
详情页入口
├── StockDetailPage
│   ├── useStockResearchData
│   │   └── useChartSnapshot (chart-snapshot API，唯一行情真源)
│   │       ├── quote (从 chart-snapshot 派生)
│   │       ├── bars (从 chart-snapshot 派生)
│   │       ├── indicators (从 chart-snapshot 派生)
│   │       ├── freshness_state (fresh/partial/stale/unavailable)
│   │       └── market_session (响应式依赖)
│   ├── ChartCanvas (bars from chart-snapshot)
│   └── QuoteCard (price from chart-snapshot.quote)
└── MessagesPage
    └── buildStockDetailUrl(symbol, {originScope: 'direct'})
```

**数据源周期合同：**
- 1d：DB 日线 + Pytdx `fetch_today_daily_bars`，按交易日去重，前复权一次
- 1w/1mo：先合并今日 partial daily，再聚合周/月线
- 15m：DB 15m + Pytdx `fetch_15min_bars`（原生 15m）
- 1h：DB 60m + Pytdx `fetch_60min_bars`（原生 60m）
- 1m：DB 1m + Pytdx `fetch_minute_bars`（原生 1m）

---

## 修改文件清单

### 后端生产代码（3 文件）
1. `backend/app/api/chart_snapshot.py` — `_derive_quote_from_bars` 增加 timeframe 参数，1w/1mo quote OHLC 为 None
2. `backend/app/core/pytdx_adapter.py` — Lock 改为 RLock，确保可重入
3. `backend/app/services/market_data_aggregation_service.py` — 新增 `fetch_15min_bars`/`fetch_60min_bars`/`fetch_today_daily_bars`，15m/1h/1d 实时尾部使用原生周期；1w/1mo 先合并今日 partial daily 再聚合；实时空结果标记 degraded

### 前端生产代码（8 文件）
4. `frontend/src/api/endpoints.ts` — ChartSnapshotResponse 扩展 quote/freshness_state/market_session 等字段
5. `frontend/src/features/stock-research/StockResearchWorkspace.tsx` — 布局条件列
6. `frontend/src/features/stock-research/useStockResearchData.ts` — 删除 useRealtimeQuote，新增 visibilitychange/market_session 响应式刷新，freshness_state 文案
7. `frontend/src/hooks/useApi.ts` — useMarketSessionReactive hook
8. `frontend/src/navigation/appNavigation.ts` — direct 来源导航
9. `frontend/src/pages/MessagesPage.tsx` — buildStockDetailUrl + originScope=direct
10. `frontend/src/pages/StockDetailPage.tsx` — 单列/双列布局
11. `frontend/src/styles/global.scss` — .no-source-list 单列样式

### 后端测试（4 文件）
12. `backend/tests/test_stock_detail_market_data_contract.py` — 新增 10 项定向测试
13. `backend/tests/test_market_data_aggregation_service.py` — 更新 15m 测试为原生 15m
14. `backend/tests/test_market_data_aggregation_partial_daily.py` — 更新 1d partial daily 测试为原生日线
15. `backend/tests/test_chart_snapshot_realtime_contract.py` — 更新 fake_exchange fixture 为原生周期

### 前端测试（1 文件）
16. `frontend/src/features/stock-research/__tests__/stockDetailMarketDataContract.test.ts` — 新增 10 项定向测试

---

## 测试结果

### 后端 pytest（69 collected / 69 passed / 0 failed / 0 skipped）
- `test_stock_detail_market_data_contract.py`：10 passed
- `test_market_data_aggregation_service.py`：全 passed（含更新后的 15m 测试）
- `test_market_data_aggregation_ssot_architecture.py`：全 passed
- `test_market_data_aggregation_partial_daily.py`：全 passed（含更新后的 1d 测试）
- `test_chart_snapshot_atomic.py`：全 passed
- `test_chart_snapshot_realtime_contract.py`：全 passed（含更新后的 fake_exchange）
- `test_phase_a3_mdas_backfill.py`：全 passed

### 前端（10 tests / 10 passed / 0 failed）
- `tsc --noEmit`：0 errors
- `ESLint`：0 errors（2 pre-existing warnings）
- `stockDetailMarketDataContract.test.ts`：10 passed

### 质量检查
- `ruff check`（7 文件）：All checks passed
- `mypy`（3 后端生产文件）：Success, no issues
- `git diff --check`：clean

---

## include_realtime=False 边界

- `completed_only=True` 强制 `include_realtime=False`，不调用任何 `fetch_*` 函数
- 盘后 MFCS 使用 `include_realtime=False`，不受本次改造影响
- 测试 `test_include_realtime_false_no_fetch_calls` 和 `test_completed_only_forces_no_realtime` 验证此边界

---

## 待集成状态

- 代码完成，工作区 clean
- 未 merge、未 push、未部署
- Phase 8A commit `dd25dfb` 未被修改或 amend
- stash@{0} 为 phase8a-pollution-cleanup（A 组 27 文件）
- 等待用户批准进入集成
