# 个股详情行情唯一真源与周期合同 V1

> 变更编号：CHANGE-20260724-004
> 分支：fix/stock-detail-market-data-v1
> 状态：待集成（未 merge、未部署）

---

## 1. 唯一行情真源

个股详情页（`/stock/:symbol`）的唯一行情真源为 `/chart-snapshot` 端点返回的 `ChartSnapshotResponse`。

**禁止行为：**
- 详情页调用 `/quote` 端点
- 详情页导入或调用 `useRealtimeQuote`
- 前端 `mergeRealtimeQuoteIntoBars()` 或任何 quote→bar 兜底
- 顶部价格、K线、指标使用不同 `as_of` 的数据源

**ChartSnapshotResponse 扩展字段：**
```typescript
interface ChartSnapshotResponse {
  // 原有字段...
  quote: QuoteSnapshot | null
  bars: BarFrame[]
  indicators: IndicatorResult | null
  market_session: MarketSession
  as_of: string
  actual_latest_bar_time: string | null
  expected_latest_bar_time: string | null
  freshness_state: 'fresh' | 'partial' | 'stale' | 'unavailable'
  data_source: 'db' | 'hybrid' | 'pytdx' | 'degraded'
  is_partial: boolean
  degraded_reason: string | null
}
```

---

## 2. 周期合同（生产数据链）

| 周期 | DB 历史 | Pytdx 实时尾部 | 聚合方式 |
|------|---------|---------------|---------|
| 1d   | DB 日线 | `fetch_today_daily_bars`（原生日线） | 按交易日去重，实时覆盖 DB，前复权一次 |
| 1w   | DB 日线 | `fetch_today_daily_bars` | **先合并今日 partial daily，再聚合周线** |
| 1mo  | DB 日线 | `fetch_today_daily_bars` | **先合并今日 partial daily，再聚合月线** |
| 15m  | DB 15m  | `fetch_15min_bars`（原生 15m） | 同时间戳覆盖 |
| 1h   | DB 60m  | `fetch_60min_bars`（原生 60m） | 同时间戳覆盖 |
| 1m   | DB 1m   | `fetch_minute_bars`（原生 1m） | 同时间戳覆盖 |

**禁止的生产聚合链：**
- 1m → 15m（`_aggregate_minute_to_target`）
- 1m → 60m
- 1m → 1d（`_aggregate_minute_to_daily`）

**completed_only 语义：**
- `completed_only=True` 强制 `include_realtime=False`
- 不调用任何 `fetch_*` 函数
- `is_partial=False`，`data_source="db"`

---

## 3. Quote 与展示周期解耦

**规则：** `ChartSnapshotResponse.quote` 的 OHLC 字段必须由最新日线/当日行情事实生成，不能使用所选 1w/1mo bar 的聚合开高低收。

| 展示周期 | current_price | open/high/low/volume | prev_close | change_pct |
|---------|--------------|---------------------|-----------|-----------|
| 1d/15m/1h/1m | 最新 close | 从最新 bar 派生 | 从倒数第二根 bar 派生 | 计算值 |
| 1w/1mo | 最新 close（仍有效） | `null`（聚合值非当日） | `null` | `null` |

顶部 quote 与展示周期解耦，但 `as_of` 与 snapshot 一致。

---

## 4. freshness_state 状态机

| 状态 | 条件 | 前端文案 |
|------|------|---------|
| fresh | 交易时段 + 实时数据存在 + `actual_latest_bar_time` 接近 `expected_latest_bar_time` | 实时行情 |
| partial | 交易时段 + 实时数据存在但周期未完成 | 当期未完成 |
| stale | 交易时段 + 实时目标周期返回空（应有数据但无返回） | 数据延迟 |
| unavailable | 非交易时段 + 无任何数据 | 行情不可用 |
| （非交易时段有收盘数据） | 非交易时段 + DB 数据存在 | 最近收盘 |

**禁止行为：**
- 实时目标数据为空时返回普通 `db` 状态
- 使用泛化"行情回退"文案

---

## 5. Pytdx 并发控制

- `PytdxAdapter._io_lock` 必须为 `threading.RLock`（可重入）
- `connect`/`reconnect`/`disconnect`/`_fetch_with_retry` 及直接 `self.api` 调用必须在锁内
- 禁止自锁（同线程嵌套获取）和漏锁（直接访问 `self.api` 无锁）

---

## 6. 响应式刷新

**触发条件（每次只触发一次 invalidate）：**
- `market_session` 变化（通过 `useMarketSessionReactive` hook）
- 页面 `hidden → visible`（`visibilitychange` 事件）
- 切换股票（`instrumentId` 变化）
- 切换周期（`timeframe` 变化）

**禁止行为：**
- 失效循环（invalidate 触发重新渲染 → 再次 invalidate）
- 重复触发（同一事件多次 invalidate）

---

## 7. 布局合同

| 来源 | originScope | 布局 | 来源列表 |
|------|------------|------|---------|
| /market 选股 | market | `200px minmax(0,1fr)` 双列 | 显示行情来源 |
| 自选监控 | watchlist | `200px minmax(0,1fr)` 双列 | 显示自选来源 |
| 直接访问（深链/书签/通知） | direct | `minmax(0,1fr)` 单列 | 无来源列表，显示"直接访问" |

**URL 构造：** 统一使用 `buildStockDetailUrl(symbol, opts)`，禁止手拼 `/stock/:symbol`。

---

## 8. include_realtime=False 边界

- 盘后 MFCS 及研究计算使用 `include_realtime=False`
- `include_realtime=False` 时不得调用任何 `fetch_*` 函数
- 盘后流程不受实时行情改造影响
- `completed_only=True` 隐含 `include_realtime=False`
