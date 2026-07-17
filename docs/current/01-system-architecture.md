# 01 系统架构

## 1. 总体架构

```text
React Browser
  → Nginx Frontend
  → FastAPI Backend
  → PostgreSQL / Redis

Python Workers:
  bars_scheduler
  strategy_scheduler
  calendar_scheduler
  monitor_scheduler
  strategy_batch
  outbox
  delivery
  after_close_orchestrator
  capture service
```

PostgreSQL 是正式业务状态来源。Redis 只保存可重建缓存、锁和短期协调状态。

## 2. 代码主入口

| 职责 | 文件 |
|---|---|
| FastAPI 应用 | `backend/app/main.py` |
| 统一 Worker 入口 | `backend/app/worker.py` |
| 前端路由 | `frontend/src/App.tsx` |
| 生产编排 | `docker-compose.prod.yml` |
| 指标根数与契约 | `backend/app/constants/indicator_contract.py` |
| 策略资产 | `backend/app/strategy_assets/manifests/` |
| 权限上下文 | `backend/app/services/access_control_service.py` |
| Worker 用户资格 | `backend/app/services/eligible_user_service.py` |

## 3. 后端依赖方向

```text
API / Worker Orchestrator
        ↓
Application / Domain Service
        ↓
Repository / Strategy Runtime / External Adapter
        ↓
PostgreSQL / Redis / External Service
```

- API 负责认证、权限依赖、参数校验、响应，不复制业务规则；
- Service 负责业务状态、事务、资格、幂等和编排；
- Repository 负责数据库访问，不判断订阅和产品语义；
- Strategy Runtime 负责行情输入和指标计算，不决定用户权限；
- Adapter 负责 Pytdx、Mootdx、飞书、Redis、截图浏览器等外部系统。

## 4. 模块边界

| 模块 | 边界 |
|---|---|
| access | 用户、角色、订阅、Plan、资格、配额 |
| market_data | 行情、交易日历、聚合、数据新鲜度 |
| screening | DSA selector、StrategyRun、发布批次 |
| watchlist | 用户自选和额度 |
| monitoring | 完成 Bar 评估、状态、事件 |
| notifications | NotificationMessage、Outbox、Delivery、渠道 |
| capture | Capture Token、截图 worker、图片 URL |
| jobs | SchedulerJobRun、worker heartbeat、任务恢复 |
| admin | 管理 API、审计、运维页面 |
| indicator | 全局技术指标（SQZMOM_LB、SMC）纯函数计算；位于 `backend/app/strategy_assets/algorithms/features/`，不是 Service；SMC 按需启用（`include_smc=False` 默认），不进入 DSA/Node/Capture/监控/选股；FVG 完全排除（不计算、不返回、不缓存、不渲染）；SMC Pine 语义核心 `smc_pine_core.py`（唯一核心，生产+测试共用），`smc_indicator.py` 为薄包装委托层；Pine 原语（`pine_rma`/`pine_atr`/`pine_cumulative_mean_range`/`pine_highest/lowest`/`pine_crossover/crossunder`）；warmup ≥500 根（1d 用 `full_daily_bars` 全量日线）；SMC 完整历史只在核心内计算，**view adapter（`smc_view_adapter.py`，CHANGE-20260716-001）输出有界 DTO 并重基准索引**，Redis/API 不得返回约 12000 根完整 time 和全部 pivots；前端 `smcToDisplay` 按时间过滤展示区事件；**Pine 语义对齐（CHANGE-20260715-006 → CHANGE-20260716-001 crossover 修正）**：`pine_rma` 严格复现 `ta.rma`（`bar_index < length-1` 返回 `na`，`==length-1` 写 SMA 种子，之后 Wilder 递推）；首个 pivot 在 `i==size` 检测（`i >= size` 非 `i > size`）；**crossover/crossunder level_curr/level_prev 快照**（每 Bar 快照六个 pivot level，swing/internal 独立，不互相覆盖；crossover=`close_curr > level_curr && close_prev <= level_prev`，NaN→False）；EQH/EQL DTO 三时间点（anchor/second_pivot/confirmed，second_pivot 为视觉线端点，confirmed 因果/回放使用）；`swing_bias` 直接返回 `state.swing_trend.bias`（{1,-1,0}，前端不猜测）；OB slice `[start:end)` end-exclusive；**required_inputs（CHANGE-20260716-001）**：`indicator_service` 为注册策略建立 `_REQUIRED_INPUTS` 映射，只加载当前周期和实际依赖，避免 1d 请求无条件读取 750 天 15m/1m |

### 4.1 市场数据 SSOT 与复权唯一出口（CHANGE-20260717-002）

**`MarketDataAggregationService` (MDAS) 是行情读取 + 复权应用 + 周/月聚合的唯一出口**（详见 `docs/analysis/market-data-ssot-adjustment-v2.md`）：

- **职责边界**：
  - **MDAS**：读取 raw bars（经 repository 私有 `_query_*`）→ 调用 `AdjustmentFactorService` 获取权威因子序列 → 应用复权（`adj_factor._apply_adj_factor_core`，仅一次）→ 周/月"日线完成复权后经 `kline_aggregator` 聚合" → 返回 bars + 诊断字段（hash/contract_version/as_of/completed_through/degraded）。
  - **`AdjustmentFactorService`**：权威因子序列管理。`get_factor_series(session, instrument_id, as_of=date)` 返回**只含 `trade_date <= as_of` 的截断因子序列**（point-in-time 语义）；`rebuild_factor_series` 从最早受影响日期完整重建（禁止只更新最近 5 根）；失败不得用 1.0 伪装成功，返回 degraded + 原因；成功后精确失效该股票 MDAS/indicator 缓存。
  - **`bar_repository`**：仅负责 raw OHLCV / 公司行为因子的 DB 读写和上游拉取，**不负责复权应用**。
  - **`adj_factor`（计算模块）**：纯计算，由 AdjustmentFactorService/MDAS 包装调用，业务层禁止直接导入。
  - **`kline_aggregator`**：周/月聚合出口，仅 MDAS 导入。
- **复权规则**：
  - 原始 bar 在 repository/DB 层**保持不复权**；qfq 只在 MDAS 出口应用**一次**。
  - **不信任 bar 自带 `adj_factor` 列**（pytdx hybrid bar / 15m/60m/1m 行内旧值可能为 1.0），始终使用权威因子序列 `merge_asof` 结果 `_adj`。
  - **日内（15m/60m/1m）**：同一交易日映射同一权威日线因子；**周/月**：日线完成复权后聚合，禁止 raw 聚合后再复权。
  - **公式**：`qfq_price = raw_price × factor(bar_date) / factor(as_of)`；`adjustment_as_of` 锚定请求业务日（None=最新），盘后/历史回算 `as_of=trade_date`，禁止未来除权事件泄漏。
- **架构守护**：`backend/tests/test_market_data_ssot_architecture.py` 5 个 AST 测试禁止业务模块导入 repository 私有查询/直接导入 adj_factor/导入 kline_aggregator/自行 resample 周/月（例外：`strategy_assets/algorithms/` 算法内部特征计算）。

## 5. 端到端链路

### 盘后趋势选股

```text
bars_scheduler
→ 行情更新与覆盖率
→ queued StrategyRun
→ strategy_batch
→ DSA Runtime
→ StrategyResult
→ 完整性门禁
→ published_run_id
→ /screener 查询
```

### 盘中监控与通知

```text
user_watchlist_items
→ eligible_user_service
→ monitor_scheduler
→ 最新两根 completed 1m Bar
→ MonitorEvaluation
→ StrategyEvent
→ EventRecipient
→ Outbox
→ MessageDelivery
→ Feishu / message center
```

### 个股详情截图分享

```text
用户/管理员触发分享
→ stock_detail_feishu_service
→ 文字 NotificationMessage + Outbox
→ create_capture_token
→ worker-capture 访问 /capture/stock/:symbol
→ Capture Snapshot API
→ 图片 NotificationMessage + Outbox
→ Outbox Relay
→ Delivery Worker
→ Feishu Platform App
```

### 板块分类同步与筛选（CHANGE-20260716-007 + PR #77 收口）

```text
盘后 after_close_orchestrator syncing_boards 步骤
→ wencai_board_provider（pywencai 唯一数据源，asyncio.to_thread 包装）
→ BoardSnapshot 内存快照（boards + memberships）
→ 绝对门禁 + 相对门禁校验
→ 单事务原子切换（TRUNCATE+INSERT，异常 rollback 保留旧数据）
→ market_boards / market_board_memberships 表
→ Redis board_sync:status（TTL 7 天）+ SchedulerJobRun.metadata_json.board_sync_result（PR #77 收口第二轮）
→ /market/boards 读 DB + Redis（Redis 缺失回退 job metadata，DB 有数据无状态源时 source="unknown"）
→ 前端 BoardFilterCombobox 行业关键词 ilike + 概念精确匹配
```

- **唯一入口**：`after_close_orchestrator.py` 的 `syncing_boards` 步骤是板块同步唯一入口；`worker.py` 17:00 独立 qstock 任务已删除；`mode=dsa_only` 跳过。
- **Node 运行时**：`backend/Dockerfile` 必须安装 `nodejs`，pywencai `get_token()` 通过 `subprocess.run(['node', ...])` 计算反爬 token。
- **BOARD_SYNC_ENABLED 严格解析（PR #77 收口第二轮）**：`config.py::_resolve_board_sync_enabled()` 使用 truthy/falsy 集合严格解析，非法值 fail-fast，同时处理 bool 与 str 类型；优先级「环境变量 > CONFIG_FILE > 默认 False」。
- **事件 + metadata 持久化（PR #77 收口第二轮）**：`_record_board_sync_outcome()` 在 success/failure/skip 三分支同时写入 `job_run_events` 和 `SchedulerJobRun.metadata_json.board_sync_result`，保证 Redis 缺失时仍可从 job metadata 回退。
- **Redis + DB 状态回退（PR #77 收口第二轮）**：`/market/boards` 优先读 Redis，Redis None 时回退 `market_stocks_service._get_board_sync_status_from_job()` 查询近期 job metadata；DB 有数据但无任何状态源时 `source="unknown"`（非 None）。
- **软失败**：同步失败不覆盖旧数据、不阻断 DSA/快照/发布链路；写 `job_run_event` + metadata 记录失败原因。
- **筛选语义**：industry 关键词 ilike 匹配完整路径任意一级（NFKC + trim + 转义）；concept 精确匹配（PR #77 收口第二轮：concept 也应用 `_normalize_keyword()` NFKC + trim 后再 `==`）；industry + concept AND；market stocks/StrategyRunResults/行情/自选/Excel 复用同一 `board_filter_helper`。

## 6. 实验隔离

实验可以共享只读基础行情，但必须隔离策略版本、结果标识、运行键、结果表或 schema。实验不得覆盖正式 published results、生产用户数据，也不得与生产 Worker 争抢任务。
