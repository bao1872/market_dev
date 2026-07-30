# CHANGE-20260730-012：P0 收口—盘中监控1秒/结构图片/第一金字塔/列表排序/全市场行情扫描/实时K线修复/复盘基线

状态：进行中（代码+目标纯单元测试+Ruff+TSC+ESLint 通过；PG 集成测试待 CI；浏览器验收待用户手工）  
日期：2026-07-30  
类型：behavior + contract + architecture + data  
领域：盘中监控/行情体验/量化模型/盘后编排/复盘  
负责人：TRAE

相关 PRD：

- `../../prd/10-market-data.md`：MDAS 缓存契约、latest_daily_quote
- `../../prd/20-quant-model.md`：第一金字塔 99 字段
- `../../prd/30-after-close.md`：Core→Board→Review 编排（新增 RV-AC-01~04）
- `../../prd/40-market-stock-experience.md`：右栏唯一第一金字塔、列表排序、错误显示
- `../../prd/50-watchlist-intraday.md`：盘中监控 1 秒
- `../../prd/70-review.md`：复盘 PRD V1.0（整体替换）

相关 Maps：

- `../../maps/10-market-data.md`：MDAS Redis 序列化
- `../../maps/30-after-close.md`：复盘 pointer 与 run 关系（新增）
- `../../maps/40-market-stock-experience.md`：Review 跳转合同（新增）
- `../../maps/70-review.md`：复盘模块实现状态（整体替换）

相关 Rules：

- `../../../rules/70-trae-cn.md`：页面验收要求（新增）

相关提交或 PR：

- 待 push dev 后填写

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮完成 8 项 P0 闭环：盘中监控改为 1 秒事件判定、结构事件图片 ready 合同统一、第一金字塔替代旧状态观察、列表排序字段注册表与 422/500 错误显示、全市场行情质量扫描与修复基础设施（migration 075 + 持久化 run/items + 受控 CLI）、MDAS Redis 序列化遗漏 latest_daily_quote 修复（契约 v4→v5）、复盘开发基线 PRD/Map/数据合同设计、目标测试与文档更新。

## 2. 背景与问题

变化前的关键问题：

1. **盘中监控 30 秒粒度过粗**：盘中事件判定与 Bars/Indicators/ChartSnapshot 共用 30 秒轮询，无法及时捕获报价触碰结构位/筹码位的事件。
2. **结构事件无图片**：截图就绪合同仍要求 `bb_upper` 存在，但监控 Manifest 已删除 Bollinger 事件，导致 Capture 等待旧字段永不就绪。
3. **第一金字塔无限加载**：`FirstPyramidPanel` 在 `data.symbol !== symbol` 时永远显示"加载中"，无错误提示、重试、超时、符号规范化。
4. **列表排序 422**：前端对任意排序同时发送 `sort` 和 `fp_sort`，但后端 `sort` 白名单不含 `fp_*` 字段，导致 422 错误被统一显示为"行情列表加载失败"。
5. **行情数据缺口不可见**：MDAS 只能发现尾部缺失，无法发现内部断层；神州高铁等股票存在跨数月跳 Bar。
6. **实时 K 线退化**：MDAS `BarAggregationResult.latest_daily_quote` 在 Redis 序列化时丢失，cache hit 后 `quote=null`、`freshness_state=unavailable`，价格区域退回最后一根 DB Bar。
7. **复盘页无基线**：Board V1 只是趋势/结构/动量/量能分布，不是完整复盘引擎；P/Q/U/C/V 聚合变量、三类偏差筛选器、两级扫描、板块归因、个股验证、信号追踪、次日确认均未实现。

## 3. 变化前

- 盘中监控：`run_monitor_scheduler_worker` 固定 `await asyncio.sleep(30)`，与 Bars/Indicators/ChartSnapshot 共用 30 秒。
- 截图就绪：`StockResearchWorkspace` 使用 `feishuLayersReady` 检查 `bb_upper` 及旧 Bollinger 依赖。
- 第一金字塔：`MarketRightPanel` 含 `AtomicFactsPanel`、`moreOpen`、更多观察；`StockDetailPage` 含 `AtomicFactsDrawer`、`eventPanelCollapsed`、`localStorage` 旧开关。
- 列表排序：`MarketWorkspacePage` 同时发送 `sort` 和 `fp_sort`；错误统一显示"行情列表加载失败"。
- 行情质量：无全市场扫描表/CLI；MDAS 仅尾部补齐。
- MDAS Redis：`_serialize_result` payload 不含 `latest_daily_quote`；`_deserialize_result` 构造时不传 `latest_daily_quote`，默认 None；契约版本 v4。
- 复盘：`docs/prd/70-review.md` 仅为草案占位；无 P/Q/U/C/V、筛选器、归因、追踪合同。

## 4. 变化内容

### 4.1 盘中监控 1 秒（Stage 2）

- 新增 `INTRADAY_MONITOR_POLL_SECONDS=1` 配置（`backend/app/config.py`）。
- `run_monitor_scheduler_worker` 使用 `config.intraday_monitor_poll_seconds` 控制轮询间隔（`backend/app/worker.py`）。
- 添加 `_cycle_running` 防重入标志：上一周期未完成则跳过，不重入。
- 记录每周期耗时、股票数、事件数、通知数和滞后。
- DSA/SMC/Node 重算由 `monitor_evaluations` 表 exactly-once 去重保证：新 1m bar 完成才重算，否则跳过。
- 午休、休市停止；前端监控状态可每 1 秒刷新，图表仍按完成 Bar 或 10—30 秒刷新。

### 4.2 结构事件图片 ready 合同统一（Stage 3）

- `StockResearchWorkspace.tsx` 使用 `computeCombinedReady`（structure_node 组合视图）替代旧 `feishuLayersReady`。
- 删除 `bb_upper` 及旧 Bollinger 依赖。
- `CaptureStockPage`、`StockDetail capture` 模式和 Worker 共用同一 ready resolver（`captureReady.ts`）。
- Ready 条件：bars/render_frame 匹配 + SMC 层有结果或明确无事件 + Node 层 ready/degraded/unavailable 明确。
- `indicator_view` 常量统一为 `structure_node`（`backend/app/constants/indicator_view.py`）。

### 4.3 第一金字塔替代旧状态观察（Stage 4）

- `MarketRightPanel.tsx` 只保留 `MiniKlineCard` + `FirstPyramidPanel compact`；删除 `AtomicFactsPanel`、`moreOpen`、相关 CSS 和请求，标题改"第一金字塔"。
- `marketRightPanelState.ts` 简化状态接口：只保留 `showPyramid` 和 `sectionOrder`。
- `StockDetailPage.tsx` 删除 `AtomicFactsDrawer`、`eventPanelCollapsed`、`localStorage` 旧开关；右侧唯一状态面板为 `FirstPyramidPanel detail`，capture 时隐藏。
- `FirstPyramidPanel.tsx`：symbol 不一致时显示"标识不匹配"错误并提供重试按钮，不再无限加载。
- `useFirstPyramid` hook 设置 `retry=1`，只读已发布 stock_core 快照；无快照返回结构化 unavailable。
- `useApi.ts` 添加请求超时和明确 loading/error/empty 状态。

### 4.4 列表排序与导出统一（Stage 4 续）

- 新增 `firstPyramidQuerySerializer.ts`：`isFpKey`、`serializeFpFilters`、`serializeFpSort` 纯函数。
- `MarketWorkspacePage.tsx`：字段注册表基础字段→`sort`，`fp_*`→`fp_sort`；禁止同时发送 `sort` 和 `fp_sort`。
- 显示后端 422 detail 和 500 request_id，不再统一"行情列表加载失败"。
- 列表与导出使用同一 `/market/stocks` 查询服务，删除旧 strategy-results 导出口径。

### 4.5 全市场行情扫描与修复（Stage 5）

- **Migration 075**（`backend/alembic/versions/075_market_data_quality.py`）：新增 `market_data_quality_runs` 和 `market_data_quality_items` 表，forward-only。
- **ORM**（`backend/app/models/market_data_quality.py`）：`MarketDataQualityRun` 和 `MarketDataQualityItem`。
- **Service**（`backend/app/services/market_data_quality_service.py`）：`MarketDataQualityService` 含扫描、修复、汇总方法；6 个纯函数（OHLC 校验、量额一致性、重复检测、缺口检测、因子异常、时间排序）。
- **CLI**（`backend/scripts/market_data_quality_cli.py`）：`--scan`/`--repair`/`--scan-and-repair`、`--timeframe`、`--symbols`、`--start`/`--end`、`--batch-size`、`--dry-run`、`--resume`、`--canary`。
- **分类**：NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING/DB_MISSING/FACTOR_MISSING/OK。
- **修复**：只处理 DB_MISSING（上游有但 DB 缺失）；幂等 upsert 原始未复权 OHLCV；随后按现有除权除息 SSOT 重算 adj_factor；禁止把 qfq 价格写入原始表。

### 4.6 实时 K 线修复（Stage 6）

- `market_data_aggregation_service.py`：
  - `_serialize_result`：payload 新增 `latest_daily_quote` 字段。
  - `_deserialize_result`：构造 `BarAggregationResult` 时传入 `latest_daily_quote=payload.get("latest_daily_quote")`。
  - `_MARKET_DATA_CONTRACT_VERSION`：`v4` → `v5`（cache_key 含版本后缀，旧 v4 缓存自动失效，无需全局 flush）。
- 自测：新增 section 8 验证 `latest_daily_quote` 序列化/反序列化完整保留；修正 section 6 `== "v2"` 为引用常量；section 9 cache_key 后缀断言改为引用常量。
- 测试：`test_market_data_aggregation_service.py` 两处 `== "v4"` 改为 `== "v5"`；新增 `latest_daily_quote` round-trip 断言。

### 4.7 复盘开发基线设计（Stage 7）

- `docs/prd/70-review.md`：整体替换为 PRD V1.0（§0-§22 + 最终原则），含 8 张表 schema、A/B/C 筛选器条件、P/Q/U/C/V 指标合同、12 组 API 合同、5 阶段页面、Phase 0-5 实施顺序。
- `docs/maps/70-review.md`：整体替换为实现状态 Map（核验状态待实现，PRD 章节→状态映射，8 表合同摘要，API 合同摘要，Board V1 边界）。
- `docs/prd/30-after-close.md`：新增"复盘编排"章节（RV-AC-01~04 触发/顺序/隔离恢复/门禁）。
- `docs/maps/30-after-close.md`：新增"复盘 pointer 与 run 关系"章节。
- `docs/maps/40-market-stock-experience.md`：新增"Review 跳转合同"章节。
- `docs/runbooks/after-close-production-run.md`：新增"Review canary / resume / publish"章节。
- `rules/70-trae-cn.md`：新增"页面验收要求"章节（URL/Console/Network 三类证据记录）。

## 5. 变化后

- 盘中监控事件判定 1 秒粒度，结构与筹码关键位在 1m bar/源 hash/自选变化时重算。
- 截图就绪统一使用 `structure_node` 视图，不再依赖 `bb_upper`。
- 第一金字塔成为唯一个股状态表达，无限加载分支消除。
- 列表排序字段路由表建立，422/500 错误显示后端详情。
- 全市场行情质量扫描基础设施就绪（需服务器应用 migration 075 后运行 CLI）。
- MDAS Redis 缓存完整保留 `latest_daily_quote`，契约 v5 自动隔离旧缓存。
- 复盘模块 PRD V1.0 与 Map 落库，为后续 Phase 1-5 开发提供权威合同。

## 6. 影响范围

### 用户行为

- 盘中事件判定更及时（1 秒 vs 30 秒）。
- 结构事件图片正常发送（不再等待旧 `bb_upper`）。
- 第一金字塔加载错误可见，不再无限 loading。
- 列表排序 `fp_*` 字段不再 422，错误信息明确。
- 实时 K 线 cache hit 后 quote 不再为 null。

### API 或契约

- MDAS 契约版本 v4→v5（cache_key 后缀变化，旧缓存自动失效）。
- `BarAggregationResult` Redis 序列化 payload 新增 `latest_daily_quote`。
- 新增 `market_data_quality_runs` / `market_data_quality_items` 表（migration 075）。

### 数据

- Migration 075 forward-only，不修改现有表。
- 全市场行情扫描结果持久化到新表，可 resume。
- 修复只 upsert 原始 OHLCV，不覆盖完整表。

### 前端

- `MarketRightPanel`、`StockDetailPage`、`FirstPyramidPanel`、`MarketWorkspacePage`、`StockResearchWorkspace`、`useApi` 等修改。
- 新增 `firstPyramidQuerySerializer.ts`、`captureReady.ts`。

### 后端

- `config.py`、`worker.py`、`market_data_aggregation_service.py`、`indicator_view.py` 修改。
- 新增 `market_data_quality.py`（model）、`market_data_quality_service.py`（service）、`market_data_quality_cli.py`（CLI）、`075_market_data_quality.py`（migration）。

### Worker 与任务

- 监控 Worker 轮询间隔由 `INTRADAY_MONITOR_POLL_SECONDS` 控制。
- 全市场行情扫描/修复通过持久化 run/items 执行，可 resume，不依赖长 sleep。

### 部署与运行

- 需应用 migration 075。
- 需在服务器运行 `market_data_quality_cli.py` 进行全市场扫描（canary 后全量）。
- 旧 v4 MDAS 缓存因 cache_key 版本变化自动失效，无需手动 flush。

## 7. 迁移与兼容

- **Migration 075**：forward-only，新增两张表，不修改现有表。downgrade 为 `drop_table`。
- **MDAS 契约 v5**：旧 v4 缓存因 cache_key 后缀不匹配自动失效，无需全局 flush。反序列化向后兼容（`payload.get("latest_daily_quote")` 旧缓存返回 None）。
- **盘中监控 1 秒**：`INTRADAY_MONITOR_POLL_SECONDS` 默认 1 秒，可通过环境变量调整。
- **复盘 PRD/Map**：仅文档更新，无代码影响。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| Backend Ruff | 7 个文件 | PASS | `All checks passed!` |
| Backend 纯单元测试 | test_market_data_aggregation_service | PASS 21/21 | `/tmp/stage8_backend_test.log` |
| Backend 纯单元测试 | test_market_data_quality_service | PASS 63/63, 2 skip (PG) | Stage 5 报告 |
| Backend 纯单元测试 | test_chart_snapshot + test_stock_detail_market_data_contract | PASS 40/40 | `/tmp/stage8_backend_test.log` |
| Backend 纯单元测试 | test_capture + test_first_pyramid + test_market_stocks | PASS 132/132, 48 error (PG fixture) | `/tmp/stage8_p0_test.log` |
| Backend 纯单元测试 | test_worker + test_config + test_monitor | PASS 37/37, 17 error (PG fixture) | `/tmp/stage8_worker_test.log` |
| MDAS 自测 | `python -m app.services.market_data_aggregation_service` | PASS | 9 项验证全过（含 latest_daily_quote round-trip） |
| Frontend TSC | `tsc --noEmit` | PASS | 无输出（无错误） |
| Frontend ESLint | 9 个 changed files | PASS | 无输出（无错误） |
| PG 集成测试 | 本地不可运行 | SKIP | AGENTS.md §8 禁止本地 PG 集成；待 CI |
| 浏览器验收 | /market /stock /review | 未验证 | TRAE 不得自动登录 Owner 账户；待用户手工 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD 70-review | 整体替换为 PRD V1.0（§0-§22） |
| PRD 30-after-close | 新增复盘编排章节（RV-AC-01~04） |
| Maps 70-review | 整体替换为实现状态 Map |
| Maps 30-after-close | 新增复盘 pointer 与 run 关系 |
| Maps 40-market-stock-experience | 新增 Review 跳转合同 |
| Runbooks after-close-production-run | 新增 Review canary/resume/publish |
| Rules 70-trae-cn | 新增页面验收要求 |

## 10. 回滚方案

- **代码回滚**：`git revert` 对应提交即可。
- **Migration 075 回滚**：`alembic downgrade 074_board_analysis_v1`（drop 两张表，无业务数据损失，因为是新表）。
- **MDAS 契约 v5 回滚**：将 `_MARKET_DATA_CONTRACT_VERSION` 改回 `v4`；旧 v5 缓存因 cache_key 不匹配自动失效。
- **文档回滚**：`git checkout` 对应文件即可。

## 11. 遗留问题与风险

1. **PG 集成测试未在本地运行**：AGENTS.md §8 禁止本地 PG 集成；需在 CI 临时 Postgres 容器中验证 migration 075、并发 claim、幂等性等。
2. **浏览器验收未完成**：TRAE 不得自动登录 Owner 账户；需用户手工验证 /market 排序、右栏、/stock 详情、实时 K 线。
3. **全市场行情扫描未执行**：需服务器应用 migration 075 后运行 CLI（先 canary 后全量）。
4. **结构事件图片自动链路未验证**：需服务器真实触发一条 SMC 和一条 Node 事件，验证卡片和组合图。
5. **ff-only 合并 main 与正式部署未执行**：需用户确认后执行（涉及服务器 SSH、Docker 镜像构建）。
6. **复盘模块未实现**：本轮仅完成 PRD/Map 设计与数据合同；Phase 1-5 开发待后续。

## 12. 后续变化

- **CHANGE-20260731-xxx**：复盘 Phase 1 后端骨架（migration 076、ORM、scope snapshot、P/Q/U/C/V、run/item、API overview/scopes）。
- **CHANGE-20260731-xxx**：复盘 Phase 2 筛选器与归因（A/B/C 筛选器、signals、attributions、instrument mapping、发布门禁）。
- **CHANGE-202608xx-xxx**：复盘 Phase 3-4 前端与追踪闭环。
