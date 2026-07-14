# 盘迹项目开发与文档一致性规则 v2

适用项目：`market_dev` / 盘迹 PanJi\
适用阶段：探索期产品 + 可运行生产验证期\
核心目标：防止 AI/Trae 在新对话、新机器或新分支中误解当前系统，防止已确认业务逻辑被旧代码、旧文档或旧记忆还原。

***

## 一、最高原则

任何修改必须形成闭环：

```
读取文档入口
→ 理解当前系统地图
→ 核对真实代码入口
→ 建立 CHANGE
→ 明确修改范围和不修改范围
→ 修改代码/文档/测试
→ 运行一致性检查
→ 创建 PR
→ 人工 Review 后合并

```

完成标准不是“代码能跑”，而是：

```
代码实现
= 当前设计文档
= 系统地图
= API / 数据契约
= 测试验证
= 部署配置

```

***

## 二、当前文档结构

当前真实文档结构以 v2 为准：

```
docs/
  README.md
  AI-ONBOARDING.md
  RESTORE-CHECKLIST.md
  MAINTENANCE.md
  MIGRATION-MAP.md
  SOURCE-SNAPSHOT.md

  current/
    MANIFEST.md
    00-product-business.md
    01-system-architecture.md
    02-data-api-contracts.md
    03-jobs-integrations-operations.md
    04-frontend-ux.md
    05-testing-acceptance.md
    open-decisions.md
    code-doc-alignment.md

  maps/
    backend-module-map.md
    frontend-route-map.md
    api-route-map.md
    database-model-map.md
    worker-job-map.md
    notification-flow-map.md
    test-coverage-map.md
    deployment-runtime-map.md

  changes/
    CHANGELOG.md
    records/

  archive/
    current-legacy-20260703/

```

旧规则中 `docs/current/00-18` 的结构已经废弃。旧 current 文档只作为历史归档，不再作为当前事实源。

***

## 三、文档职责

### 1. `docs/README.md`

文档系统入口。说明文档地图、阅读顺序、事实源边界和修改流程。

### 2. `docs/AI-ONBOARDING.md`

新对话、新机器、新 agent 必须先读。用于快速恢复项目上下文。

任何 Trae/Codex/ChatGPT 任务开始前，必须先读取：

```
docs/AI-ONBOARDING.md
docs/current/MANIFEST.md

```

### 3. `docs/RESTORE-CHECKLIST.md`

用于判断新环境是否已经正确理解项目。不得跳过。

### 4. `docs/current/MANIFEST.md`

当前设计基线唯一总入口。\
全局基线字段只维护在这里，不再要求每个 current 文档重复维护。

必须包含：

```
设计基线日期
设计确认截止日期
实现核对基线
实现核对分支
最近一致性检查日期

```

### 5. `docs/current/*.md`

只描述当前有效设计，不保存历史。

职责：

```
00-product-business.md              产品定位、用户、核心业务边界
01-system-architecture.md           系统架构、模块边界、技术栈、部署单元
02-data-api-contracts.md            数据模型、API、权限、安全、参数契约
03-jobs-integrations-operations.md  Worker、调度、飞书、Capture、Outbox、部署运维
04-frontend-ux.md                   路由、页面、前端状态、UI 规则
05-testing-acceptance.md            测试层级、验收门禁、CI 策略
open-decisions.md                   真正未决的问题
code-doc-alignment.md               当前代码/文档/生产表现不一致的 Known Gap

```

### 6. `docs/maps/*.md`

这是系统实现地图，不是 PRD。

必须帮助新 agent 回答：

```
代码在哪里？
哪个页面调哪个 API？
哪个 API 调哪个 Service？
哪个 Worker 处理哪个任务？
哪些表保存核心状态？
哪些测试覆盖关键规则？
生产服务如何运行？

```

maps 可以半自动维护，但不能胡编。

### 7. `docs/changes/`

只保存变更历史。\
历史设计、旧方案、修复过程、证据链都放这里，不放 current。

### 8. `docs/archive/`

只保存历史归档，不作为当前事实源。\
旧 `docs/current/00-18` 已归档后，不得再作为修改依据。

***

## 四、事实源优先级

当代码、文档、历史 CHANGE、生产表现冲突时，不得擅自判断谁一定正确。

判断顺序：

```
1. 用户当前明确要求
2. 当前 main 代码
3. docs/current/MANIFEST.md
4. docs/current/*.md
5. docs/maps/*.md
6. 最新 docs/changes/records/*.md
7. 测试与 CI 结果
8. 生产只读验证结果
9. archive 历史文档
10. 旧聊天记忆

```

archive 和旧聊天不能覆盖 current。

***

## 五、每次修改前必须读取

### 通用必读

```
docs/AI-ONBOARDING.md
docs/current/MANIFEST.md
docs/RESTORE-CHECKLIST.md

```

### 修改产品/业务

```
docs/current/00-product-business.md
docs/current/02-data-api-contracts.md
docs/maps/api-route-map.md
docs/maps/database-model-map.md

```

### 修改后端

```
docs/current/01-system-architecture.md
docs/current/02-data-api-contracts.md
docs/maps/backend-module-map.md
docs/maps/api-route-map.md
docs/maps/database-model-map.md

```

### 修改前端

```
docs/current/04-frontend-ux.md
docs/maps/frontend-route-map.md
docs/maps/api-route-map.md

```

### 修改 Worker / 飞书 / Capture / Outbox / Delivery

```
docs/current/03-jobs-integrations-operations.md
docs/maps/worker-job-map.md
docs/maps/notification-flow-map.md
docs/maps/deployment-runtime-map.md

```

### 修改测试 / CI

```
docs/current/05-testing-acceptance.md
docs/maps/test-coverage-map.md

```

### 修改部署

```
docs/current/03-jobs-integrations-operations.md
docs/maps/deployment-runtime-map.md
docker-compose.prod.yml

```

***

## 六、修改前理解说明

Trae 在动手前必须输出：

```
1. 当前任务目标
2. 当前分支和 base commit
3. 已阅读哪些 docs/current 文件
4. 已阅读哪些 docs/maps 文件
5. 当前代码真实入口
6. 当前前端入口
7. 当前 API 入口
8. 当前 Service / Repository / Worker 入口
9. 当前涉及哪些数据表
10. 当前测试覆盖哪些规则
11. 文档和代码是否一致
12. 本次准备修改什么
13. 明确不修改什么
14. 预计更新哪些 docs/current
15. 预计更新哪些 docs/maps
16. 预计新增哪个 CHANGE

```

如果发现冲突，先列出冲突，不得直接编码。

***

## 七、CHANGE 规则

每次修改必须新增：

```
docs/changes/records/CHANGE-YYYYMMDD-NNN.md

```

并更新：

```
docs/changes/CHANGELOG.md

```

CHANGE 必须包含：

```
变更编号
任务名称
需求出处
修改前行为
修改后行为
影响模块
修改文件
文档更新
测试证据
Git 分支
Git Commit
数据库迁移
配置变化
风险
遗留问题

```

不存在“小改不用 CHANGE”。

***

## 八、current 文档修改规则

`docs/current/` 只写当前状态，不写流水账。

错误写法：

```
以前是 A，现在改成 B。

```

正确写法：

```
当前系统使用 B。

```

历史 A 放入 CHANGE 或 archive。

***

## 九、maps 修改规则

如果修改了真实代码结构，必须检查对应 `docs/maps/`。

例如：

```
新增/删除 API              → api-route-map.md
新增/移动 Service          → backend-module-map.md
新增/移动页面              → frontend-route-map.md
改表、字段、约束            → database-model-map.md
改 Worker / 调度           → worker-job-map.md
改飞书、Capture、Outbox     → notification-flow-map.md
改测试覆盖                 → test-coverage-map.md
改 compose / 部署服务       → deployment-runtime-map.md

```

maps 不能变成理想设计，必须描述真实实现位置。

***

## 十、禁止行为

Trae 和开发者禁止：

```
1. 未读 AI-ONBOARDING 和 MANIFEST 就修改；
2. 根据旧 docs/current/00-18 修改当前系统；
3. 根据 archive 恢复旧设计；
4. 根据旧聊天记忆覆盖 current；
5. 只改代码不改文档；
6. 只改 current 不改 CHANGE；
7. 改代码结构不更新 maps；
8. 只更新文档不核对真实代码；
9. 复制旧实现形成第二条路径；
10. 在前端重新实现后端业务规则；
11. 删除测试以适配错误实现；
12. 修改 API 不检查前端调用；
13. 修改数据模型不检查 migration；
14. 修改 Worker 不检查幂等、心跳、重试；
15. 修改权限不检查用户隔离；
16. 把 Mock E2E 说成真实生产 E2E；
17. 把 OPEN 问题写成最终结论；
18. 把临时实验写成永久规则；
19. 直接修改 main；
20. force push 已共享分支；
21. 为通过检查削弱 check_docs_consistency.py。

```

***

## 十一、docs consistency 硬规则

`tools/check_docs_consistency.py` 必须匹配 v2 文档结构。

必须检查：

```
1. docs/current/MANIFEST.md 存在；
2. MANIFEST 包含实现核对基线；
3. 实现核对基线是 40 位 SHA；
4. SHA 是真实 commit；
5. SHA 是当前 HEAD 祖先；
6. docs/current/*.md 存在；
7. docs/maps/*.md 存在；
8. 本地 Markdown 链接有效；
9. 不存在 待填写 占位符；
10. current 文档不得把 feishu_webhook 写成当前方案；
11. open-decisions 不得把 Webhook vs Platform App 写回 OPEN；
12. archive 旧文档不参与 baseline 一致性检查。

```

禁止把检查改成摆设。

***

## 十二、盘迹项目硬规则

### 1. 产品边界

盘迹是 A 股研究、全市场特征计算、自选股盘中监控和消息投递平台。

不做：

```
自动交易
券商账户连接
资金管理
收益承诺
单一指标买卖信号
普通用户修改生产算法参数

```

### 2. 策略规则

当前生产只保留：

```
dsa_selector
watchlist_monitor

```

多策略组合已废弃，不得从旧代码或旧文档恢复。

### 3. DSA 规则

DSA 对全市场 computable universe 计算特征。\
不得在计算阶段按方向、强弱、matched、用户筛选提前删除股票。

发布必须满足严格完整性门禁。\
`partial_failed` 不得发布。

### 4. 自选和监控

有效会员添加自选后自动进入盘中监控。\
不创建 MonitoringPlan。\
到期用户保留历史数据，但不能读取、修改、监控或产生新投递。

### 5. Node Cluster

固定契约：

```
1d = 250 根日线
15m = 250 * 16 = 4000 根
1m = 2 根已完成 Bar

```

图表显示数量、指标输出数量、Node 内部输入数量必须分离。

### 6. 飞书

唯一接入方式：

```
feishu_platform_app

```

禁止恢复：

```
feishu_webhook
FEISHU_WEBHOOK
独立管理员飞书 App
独立管理员接收人配置

```

管理员内测申请通知必须复用管理员用户自己的 active `feishu_platform_app` NotificationChannel。

### 6.1 飞书盘中截图与盘中监控口径（CHANGE-20260710-002 确立）

- 盘中监控触发只依赖**最新已完成 1m bar**：`source_bar_time` 必须来自最新已完成 1m bar（剔除最后一根可能未完成的 bar），禁止用 1d/15m/partial daily 作为监控触发口径；
- 飞书盘中截图业务默认 `timeframe=1d`（日线）：实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；
- Capture API 支持多周期（1d/15m/1h/1w/1mo）是**能力**，不等于飞书业务默认 15m；业务调用方（手动飞书分享 `stock_detail_feishu_service`、自动盘中监控 `monitor_batch_service._send_chart_images_via_outbox`）默认传 1d；
- 修截图、清晰度、缓存（`device_scale_factor` / `disable_cache` / `force_refresh` / `source_bar_time` cache key）**不得改变 `watchlist_monitor` 事件计算口径**；`monitor_batch_service` 计算输入 `bars_daily` / `bars_15min` 必须 `include_realtime=False`；
- `15m` 只作为 API 能力或策略明确声明的辅助上下文（`dsa_selector` latest-event 截图等），不得成为 watchlist_monitor 飞书业务默认周期。

### 7. Capture Token

Capture Token 只能访问 Capture API。\
不能访问普通用户 API。\
不能污染普通 Access Token。

### 7.5 板块同步降级保护

`BOARD_SYNC_ENABLED` 默认 `false`。\
关闭时 `scheduled_board_sync` 跳过执行，记录 `status=skipped` + `reason_code=board_provider_unavailable`，不发起任何 THS 请求。\
`/market/boards` 响应含 `available`（bool）和 `reason_code`（str|null）；`available=false` 时前端禁用行业/概念筛选输入。\
ALIGN-041 OPEN：当前物理机 THS 成分股 403、akshare 无 THS 成分接口；至少一个同花顺语义 provider 真实返回完整目录+成分后方可开启。\
不得增加 akshare、代理、IP 绕过、东方财富混用或新常驻 worker。保留单 provider、事务失败保留旧快照设计。

### 8. Outbox target\_channel\_id

`notification.message.created` payload 含 `target_channel_id` 时，属于用户主动触发/手动指定渠道通知，跳过 `eligible_user_service`。

无 `target_channel_id` 的自动通知仍必须走用户资格过滤。

### 9. Migration

不得修改已发布历史 migration。\
只允许新增前向 migration。\
修改 migration 必须有 upgrade/downgrade/upgrade 验证。

### 10. Alignment

未经测试、CI 或真实运行证据，不得关闭 `code-doc-alignment.md` 条目。\nMock 不能替代真实生产 E2E。

### 11. 测试期部署不备份数据库

测试期部署默认不备份数据库；除非用户明确说“先备份数据库”，否则禁止 `pg_dump` / 大体积备份，禁止写入 `/root/backups` 或 `/root/web_dev/backups`。当前物理机磁盘紧张，优先节省硬盘；有问题直接定位修复。

### 12. Docker 镜像保护

`node:20-alpine` 是受保护基础镜像，拉取很慢。禁止主动删除 `node:20-alpine`。\
禁止 `docker image prune -a`。\
除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。\
普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`。

### 13. 个股详情 K线实时契约

个股详情 K线实时属于后端 `/api/v1/instruments/{id}/bars` 契约，不得只靠 `/quote` 或前端 `mergeRealtimeQuoteIntoBars()` 伪装。

1. `/quote` 实时只代表顶部行情卡片实时，不等价于 K线实时。
2. 交易时段内，`/bars?timeframe=1d&include_realtime=true` 必须返回今日 partial daily bar：
   - `data_source=hybrid`
   - `is_partial=true`
   - `last_live_bar_time` 非空
   - 最后一根 bar 日期为今日
   - close 来自最新已完成 1m bar。
3. 收盘后或非交易时段，不得伪装实时：
   - `is_partial=false`
   - 1d 最后一根应为完整日线
   - quote 可为 `daily_fallback`。
4. 前端 `mergeRealtimeQuoteIntoBars()` 只能作为兜底视觉增强，不能替代后端 partial bar。
5. 任何修改以下文件必须跑 K线实时契约测试：
   - `backend/app/api/bars.py`
   - `backend/app/services/market_data_aggregation_service.py`
   - `backend/app/core/pytdx_adapter.py`
   - `frontend/src/pages/StockDetailPage.tsx`
   - `frontend/src/utils/chart.ts`
6. PR 描述必须回答：
   - quote 是否实时？
   - 1d K线是否有 partial bar？
   - 15m/1h/1m 是否受影响？
   - 前端是否只是展示，是否存在伪造实时？
   - 交易时段和收盘后分别如何验证？

### 14. 用户/管理员壳层与导航拆分

1. 普通用户主入口为 `/market`（行情）；趋势选股 `/screener` 保持独立一级页面。
2. 行情与自选合并：`/market` 渲染 `MarketWorkspacePage`；`/overview` 重定向到 `/market`，`/watchlist` 重定向到 `/market?scope=watchlist`。
3. 普通用户使用 `UserAppShell`（顶栏品牌 + 一级导航行情/趋势选股 + 右上角账户菜单；无左侧栏）；消息、设置、管理后台入口、退出收拢到 `AccountMenu`。
4. 管理后台使用独立 `AdminAppShell`（侧栏管理导航 + 账户菜单），仅承载 `/admin/*`；不得套用普通用户导航。
5. `ProtectedLayout` 只负责认证与 access profile，不再固定渲染同一壳层。
6. `/capture/stock/:symbol` 位于两套壳层之外，只使用 `captureClient`。
7. 导航/路由常量集中于 `frontend/src/navigation/appNavigation.ts`，禁止路径散落。
8. 行情工作区：`/market` 渲染 `MarketWorkspacePage`（**无 K 线**，布局为 `MarketToolbar` + `StrategyDataTable`（复用 `getTrendSelectionColumns` DSA 列定义）+ 可收起 `EventStatePanel`）；`/market` 明确禁止挂载 `StockResearchWorkspace`/`StrategyChart`/任何 K 线组件；`/market` 列表数据来自 `usePublishedRuns` + `useStrategyRunResults`（published DSA run），不再使用 `useMarketStocks`/`MarketStockTable`；`/stock/:symbol` 渲染 `StockDetailPage`（唯一 K 线入口，复用 `useStockResearchData` + `StockResearchWorkspace`）；`useStockResearchData` 只保留 bars/indicators/quote/events 核心查询，详情页专属能力（自选/上下切换/memo/飞书）拆到 `useStockDetailActions`/`useStockDetailFeishu`；`/overview`、`/watchlist`、`/screener` 仅保留兼容重定向。
9. `timeframe` 单一真源：URL → `useStockResearchData`（bars/indicators 请求参数）→ `StockResearchWorkspace`（图表渲染）三者始终使用同一 `DisplayTimeframe`；工具栏切换必须通过 `onTimeframeChange` 回调写回 URL，禁止子组件 `useState` 维护独立 timeframe；图表显示周期不得改变 1d+15m 监控配置或 1m 事件触发口径；`/stock/:symbol` 的 timeframe 也从 URL 解析。
10. 请求门控：`useWatchlistMonitorStatus` 和 `useInstruments` 必须通过 `enabled` 参数按 scope 互斥启用（watchlist scope 只启用 monitor-status，market scope 且搜索词 trim 后 ≥2 字符才启用 instruments）；`useStockResearchData` 不得请求 `MarketWorkspace` 未使用的 watchlist/batchInstruments/stockMemo。
11. URL 状态保留：`/market` URL 契约简化为 `scope/selected`（由 `MarketWorkspacePage` 管理）；`sort/dir/keyword/filters/page/page_size` 由 `StrategyDataTable` 内置 `screenerUrlState` 管理；切换 scope、搜索、筛选、翻页、选行时必须保留其他字段；`selected` 由 `StrategyDataTable` 单击行更新并驱动右栏 `EventStatePanel`；`returnTo` 为来源页 URL（从 `/screener`、`/messages` 进入 `/stock/:symbol` 时携带），返回按钮优先使用，必须经 `normalizeInternalReturnTo` 校验（仅允许 `/screener`、`/market`、`/messages` 前缀，拒绝外部 URL/`javascript:`/双斜杠/非白名单路径）。
12. 行情列表行选择：`StrategyDataTable` 单击数据行（`onRowClick`）更新 URL `selected=<symbol>` 并驱动右栏 `EventStatePanel` 加载该股票 context；点击股票名称/代码链接进入 `/stock/:symbol?returnTo=<编码后的当前 /market URL>`；切换 scope（watchlist 或 market）时保留 sort/keyword/filters/page/page_size，`selected` 可清空。
13. 搜索与筛选门控：`StrategyDataTable` 内置全文搜索（URL `keyword` 参数）和列筛选（URL `filters` 参数，转 `metric_filters` 透传 DSA API）；`scope=market` → `universe=all`，`scope=watchlist` → `universe=watchlist`；行业/概念筛选已移除（DSA API 不支持）；状态筛选已移除（DSA 列表不含形态状态列）。
14. 行情状态文案：`StockResearchWorkspace` 不得在 15m/1h/1w/1mo 显示"日线回退"；非实时非降级时统一显示"行情回退"；partial 文案必须包含当前周期（如"盘中 partial bar（15m）"），禁止所有周期统一显示"日线"。
15. 共享研究核心：`DisplayTimeframe`/`ResearchSource`/`ALLOWED_TIMEFRAMES`/`BARS_COUNT_BY_TIMEFRAME`/`defaultStrategyForSource`/`normalizeDisplayTimeframe`/`normalizeResearchSource` 权威定义在 `frontend/src/features/stock-research/stockResearchTypes.ts`；`marketWorkspaceUrlState.ts` 从该文件导入并重新导出，依赖方向为 market-workspace → stock-research（禁止反向依赖）；`StockResearchWorkspace` 通过 `toolbar`/`rightPanel`/`showRightPanel`/`chartColumnProps` 可选 props 支持详情页结构面板开关和截图模式属性；`/capture/stock/:symbol` 完全独立，不使用 `useStockResearchData`/`StockResearchWorkspace`/`apiClient`。
16. 普通用户事件状态面板：`/market` 右栏 `EventStatePanel`（`frontend/src/features/research-context/EventStatePanel.tsx`）通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，展示 MACD 动量、Evidence（事件证据）、`state.evidence`（状态证据）、数据日期/质量、当前价格结构、成交密集区关系、最近状态变化时间线；面板首次默认收起（`rightPanelCollapsed=true`），localStorage key `panji:market-right-panel-collapsed:v1` 持久化用户选择；收起时不挂载 `EventStatePanel`、不请求 context；普通用户不显示内部字段名（`sourceField`）、算法参数、`idempotencyKey`、JSON 或商业机密；原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol` 展示。
17. 管理员调试路由独立：原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol`（`AdminRoute` + `AdminAppShell` 下）的 `AdminStockDebugPage` 中展示，复用 `MarketInstrumentPane`/`useStockResearchData`/`StockResearchWorkspace`/`useAdminStockDebug`（含原始 payload 的管理员调试接口）；`/market` 不得承载管理员调试能力，`debug` 不在 `/market` URL 契约中；`/market?debug=1` 管理员访问时重定向到 `/admin/stock-debug/:symbol`，普通用户忽略并清除。
18. 事件状态面板查询入口：`features/research-context/` 只含 `EventStatePanel`/`reasonCodeMessages`（纯函数 + 测试）；`EventStatePanel` 通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，不再调用 `useStrategyEventDetail`/`useStructuralFactors`/`useTemporalFeatures` 等散落 hooks（React Query 按 queryKey 去重，不产生重复请求）；`reasonCodeMessages` 将 `reasonCode`（如 `no_published_full_run`、`snapshot_missing`）映射为用户可读文案；不新增后端算法，接口缺失时显示对应 `reasonCode` 文案，禁止伪造数据；`ScreenerPage` 查看详情进入 `/stock/:symbol?returnTo=<原 ScreenerPage URL>`（K 线详情），`MessagesPage` 有股票时进入 `/stock/:symbol?event_id=...`；`/market` URL 契约为 `scope/selected`（MarketWorkspacePage 管理）+ `sort/dir/keyword/filters/page/page_size`（StrategyDataTable 内置 screenerUrlState 管理），不得混入 `symbol/source/strategy/event_id`/`industry/concept/state`。
19. 图表图层 ≠ 策略因子：`IndicatorToolbar` 是 K 线图层开关唯一交互入口（`ChartLayerVisibility` 7 键：`trend/node/boll/volume/macd/sqzmom/breakout`），`StrategyChart` 不再渲染 `tv-strategy-legend` 只读行（已删除）；图层开关状态由 `StockResearchWorkspace` 持有单一 `layerVisibility` state，localStorage key `panji:chart-layer-visibility:v2`；`DISPLAY_GROUPS`/`DisplayGroupDef` 已删除，禁止恢复；图层 ≠ 策略因子：图层控制的是 K 线渲染（趋势/节点/布林/成交量/MACD/SQZMOM/突破），策略因子是后端 DSA 计算的结构因子（DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与），二者不可混淆。
20. MACD 语义：MACD 是 `feature_snapshot_service` 附加的日线辅助技术指标（标准 12/26/9），注入日线 `primary_factors.macd_state`；不是 bar 因子也不是时序特征；`structural_factor_service` 的 bar 因子只有：DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与。前端 `defaultChartLayerVisibility` 中 `watchlist` 和 `selection` 默认关闭 MACD（`macd: false`）；不删历史字段、不迁移、不回填。
21. 右栏按需加载：`/market` 和 `/stock/:symbol` 首次默认收起右栏（`rightPanelCollapsed=true` / `eventPanelCollapsed=true`），localStorage 持久化用户选择（`panji:market-right-panel-collapsed:v1` / `panji:event-panel:v1`）；收起时不挂载 `EventStatePanel`、不请求 `useStockContext`；展开后才挂载并请求数据；禁止在收起状态下预取数据。
22. 行情列表 boards 已移除：`/market` DSA 列表不再使用 boards 筛选（DSA API 不支持行业/概念筛选）；`MarketToolbar` 简化为仅 scope 分段按钮；`MarketStockTable` 已删除，由 `StrategyDataTable` + `getTrendSelectionColumns` 替代；`/market/boards` API 仍保留供其他用途，但 `/market` 列表不消费 boards。
23. 行情列表 latest_event 已移除：`/market` DSA 列表不显示形态状态/DSA状态/最近事件列；事件只在 `EventStatePanel` 按需展开时通过 `useStockContext` 加载；`market_stocks_service` 仅用于 `/stock/:symbol` 详情页的 returnTo 上下文恢复（`useStockDetailActions` 通过 `useMarketStocks` 恢复来源列表）。
24. DSA 列表字段契约：`/market` 列表数据来自最新 published DSA run（`usePublishedRuns` + `useStrategyRunResults`），复用 `getTrendSelectionColumns` 列定义（stock/change_pct/dsa_dir_bars/vwap_ret_avg/vwap_ret_total/offset_mean/offset_std/offset_percentile/dsa_vwap/dsa_vwap_dev_pct/offset_variance_rate/price/action）；字段值缺失返回 null，禁止用 0/空串伪装；支持服务端排序（`sort_by`+`sort_desc`）、数值筛选（`metric_filters` AND 语义）、分页（`page`+`page_size`）和关键词搜索（`keyword`）；普通重跑不得覆盖已发布结果（`allow_republish=False`）。
25. 列设置与配置 CRUD：`StrategyDataTable` 支持列显示/隐藏、调整顺序（`columnOrder`）、恢复默认、刷新保留（localStorage `table-columns:${tableId}` + `table-column-order:${tableId}`）；股票名称/代码和操作列不可全部隐藏；配置继续复用 `/me/table-view-presets`，保存 `keyword/sort/filters/hiddenColumns/columnOrder/pageSize/industry/concept`（不保存 `page/selected/run_id`）；POST/PATCH/DELETE 真正 commit，跨 session 可读取；旧配置包含已删除字段时忽略未知项，不能白屏。
26. P0 列对齐契约：`StrategyDataTable` 表头 th、表体 td、colgroup col 三者必须从同一 `visibleColumns` 派生（`reorderVisibleColumns` 纯函数，位于 `frontend/src/components/columnOrdering.ts`）；每行 td 数 = 可见 th 数 = `visibleColumns.length`；单元格按 `col.key` 取值，禁止依赖数组下标或对象遍历顺序；action/select 列固定 id，不参与重排；`columnAlignment.test.ts` 覆盖纯函数 + 源码契约。
27. 行情列表行内导航与自选操作（CHANGE-20260713-005）：`/market` 列表 `action` 列改名"自选"，渲染"加入自选/移除自选"按钮（非旧"详情"按钮）；股票名称/代码为可点击 `<a>` 链接，点击进入 `/stock/:symbol?returnTo=<编码后的当前 /market URL>`；链接与自选按钮 `onClick` 必须 `e.stopPropagation()` 防止冒泡到 `<tr onClick>`；股票单元格只显示名称/代码/市场，不再显示行内涨跌幅（独立 `change_pct` 列保留）；页面只请求一次 `useWatchlist`，按 `instrument_id` 建 Set 判断 watched 状态，禁止逐行查询/N+1；`useAddToWatchlist`/`useRemoveFromWatchlist` 成功后 invalidate `['watchlist']` 和 `['watchlist', 'monitor-status']`；`watchlist` scope 移除自选后该行应消失；按 `instrument_id` 维护 `pending` Set 防重复点击；`ScreenerPage` 继续保留 `onAddToWatchlist`/`onDetail` 兼容模式。
28. 行情列表单一搜索 SSOT（CHANGE-20260713-005）：`/market` 顶部 `MarketToolbar` 渲染唯一全文搜索框（占位"搜索股票代码/名称/拼音首字母"，Enter/失焦提交，清空立即提交），`StrategyDataTable` 在 `/market` 必须传 `searchable={false}` 隐藏内置搜索；搜索状态单一真源：`MarketWorkspacePage` 持有 `keyword` state（初始值从 URL `keyword` 读取）→ `StrategyDataTable` 通过 `externalKeyword`/`onKeywordChange` 受控 props 接收 → URL `keyword` 同步；后端 `strategy_result_repository.query_results` 的 `keyword` 必须 ILIKE 同时匹配 `Instrument.symbol`/`Instrument.name`/`Instrument.pinyin_initials`（3 处分支同步）；前端不做全量过滤，不增加新表。
29. 行情列表批次信息权限（CHANGE-20260713-005）：`/market` 数据日期/批次/状态属于调试信息，普通用户 DOM 中**完全不渲染**（不是 CSS 隐藏）；仅 `useAuthStore(s => s.user?.is_admin === true)` 为真时渲染，默认折叠为"批次信息"区块，点击展开后显示 `run_trade_date`/`run_published_at`/`run_status` 等字段；普通用户不显示任何批次元信息。
30. 消息数量 SSOT（CHANGE-20260713-005）：`MessagesPage` 使用 `useUnreadCount`（`GET /messages/unread-count`，queryKey `['messages', 'unread-count']`）作为未读权威数量；"全部"显示后端列表 `messagesQuery.data?.total`（不用 `items.length`）；页头显示"共 X 条 · 未读 Y 条"；分段按钮仅 `all`/`unread` 显示计数，`selection`/`price`/`system`/`process` 不显示误导数字（除非后端实现单条 GROUP BY counts API，禁止新增表/缓存/Worker）；`AccountMenu` 消息项显示未读数 badge（`>99` 显示 `99+`），`unread>0` 时点击进入 `/messages?filter=unread`；标记单条/全部已读后 `useMarkMessageRead`/`useReadAllMessages` 的 `onSuccess` invalidate `['messages']`，自动刷新列表 + unread-count + 菜单角标。
31. 消息跳转目标（CHANGE-20260713-005）：单只股票消息点击进入 `/stock/:symbol?event_id=...&returnTo=/messages`（不再 `/market?symbol=`）；`selection_composite` 类型消息进入 `/market`（不再 `/screener`）；抽屉内单只标的跳转同样进入 `/stock/:symbol?event_id=...&returnTo=/messages`；`returnTo` 必须经 `normalizeInternalReturnTo` 校验。
32. K 线 Pointer Events 拖拽契约（CHANGE-20260713-005）：`StrategyChart` 使用 Pointer Events（`pointerdown`/`pointermove`/`pointerup`/`pointercancel`）替代旧 mouse 事件；`pointerdown` 调用 `setPointerCapture`，`pointerup`/`pointercancel` 调用 `releasePointerCapture`；`dragRef` 保存 `{startClientX, startViewport, pointerId}`，`pointermove` 从 `startViewport` 计算总位移（禁止在 stale viewport 上累计）；`dragMovedRef` 4px 阈值抑制 click（避免拖动误触节点/事件点击）；cursor 为 `grab`/`grabbing`；鼠标移出 canvas 后仍可继续拖动（依赖 setPointerCapture）；保留滚轮锚点缩放、双击复位和移动端双指缩放；`chartDrag.test.ts` 覆盖源码契约。
33. 用户文案契约（CHANGE-20260713-005）：仅改用户可见文案，不改内部 id/DTO/算法；`CHART_LAYER_MANIFEST` 中 `sqzmom` 显示为"挤压动量"（tooltip"波动收窄后的方向与强弱"），`node` 显示为"筹码共识价"（description 注明"基于历史成交量分布的估算代理，非股东真实持仓成本"）；`StrategyChart` 节点价格标签 `POC 峰`→"核心共识价"，`峰`→"共识价"；POC 中心线标签显示"核心共识价"（非裸 `POC`）；tooltip 中 `POC`→"核心共识价"，`PEAK`→"共识价"；缺失提示改为"筹码共识价暂不可用"；内部字段 `n.poc`/`profile.pocPrice`/`row.is_poc`/`is_peak`/`'poc'` layer key 必须保留；不得恢复已删除的 `ConsensusZone`，也不得修改 profile/node/poc 字段名；`chartLabels.test.ts` 覆盖文案契约。
34. 盘迹品牌视觉 V1.0（CHANGE-20260713-006）：视觉真源为 `ref/盘迹品牌视觉资产包_v1.0/`；`frontend/src/styles/variables.scss` 为唯一视觉 token 真源，禁止在组件中硬编码颜色（必须使用 `v.$color-*` 或 `var(--*)`）；品牌主色为莹感绿 `#00F6C2`（`$color-brand`），只承担品牌焦点和关键交互（主按钮、选中 tab、focus 轮廓、Logo 末端节点），不得用于表达涨跌；A股继续红涨绿跌（`$color-up` `#FF4D4F` / `$color-down` `#22C55E`）；大面积背景使用深石墨黑 `#0A0F14`（`$color-bg`），避免荧光绿铺满页面；`BrandLogo` 为四节点折线路径 + 末端高亮共识节点（莹感绿圆环），不变形、不旋转、不增减节点、不替换颜色；Logo 资产位于 `frontend/src/assets/brand/`（`logo_symbol_128.png` / `logo_symbol_256.png` / `logo_horizontal_dark.png`），ref 路径不作为运行时依赖；中文字体 MiSans/HarmonyOS Sans SC/PingFang SC，fallback Noto Sans CJK SC，数字用等宽字体；卡片圆角 10-14px、1px 边框；禁止重阴影和大面积玻璃拟态；视觉改造不得改变 DSA、Node Cluster、盘中监控、Capture 计算口径。
35. DSA 行业/概念筛选契约（CHANGE-20260713-006）：`/market` 列表支持行业/概念筛选，数据源仍为 published DSA run（`usePublishedRuns` + `useStrategyRunResults`），禁止同时请求 `/market/stocks` 拼接结果；共享 `backend/app/repositories/board_filter_helper.py` 的 `build_board_filter_conditions(instrument_id_col, industry, concept)` 构造 EXISTS 子查询（MarketBoardMembership JOIN MarketBoard，type='industry'/'concept'，name 匹配），`strategy_result_repository` 和 `market_stocks_service` 共用；industry+concept 同时提供时为 AND 语义（两个 EXISTS 条件 AND 连接）；`/strategy-runs/{run_id}/results` API 支持 `industry`/`concept` Query 参数；`items`/`filtered_total`/`source_total` 必须应用完全相同的 keyword/industry/concept/metric_filters 条件，SQL 数量固定，禁止 N+1；`TableViewPresetConfig`（后端 Pydantic + 前端 TS）含 `industry`/`concept` 可选字段（max_length=100），不新增表/migration（JSONB 列），旧 preset 缺字段时 default=None 兼容，未知板块值在目录变化后 EXISTS 无匹配返回空；`MarketToolbar` 渲染"搜索、行业、概念"同一行布局，`boards.available=false` 时输入禁用但显示（placeholder"板块数据暂不可用"），不得直接删除输入；`industry`/`concept` 进入 URL，切 scope/搜索/排序/分页时保留，改变板块筛选重置 page=1；`StrategyDataTable` 通过 `externalIndustry`/`onIndustryChange`/`externalConcept`/`onConceptChange` 受控 props 接收（与 `externalKeyword` 同模式），`currentConfig`/`applyPresetConfig` 集成 industry/concept。
36. 管理员入口与 AdminRoute 权限契约（CHANGE-20260713-007）：管理员从用户 `AccountMenu`（`variant='user'` + `is_admin=true`）进入 `/admin`（菜单项"管理后台"）；普通用户（`is_admin=false`）DOM **完全不渲染**管理后台入口（不是 CSS 隐藏）；`AdminRoute` 以 `user.is_admin` 为唯一权限真源（不依赖任何其他角色/字段判断），`accessLoading` 状态防止 auth hydration 未完成时提前判定 false（刷新页面后 access store 重新拉取 `/me/access` 期间显示 loading，避免 access 未就绪时被误判为非 admin 重定向到 `/market`）；`getAccountMenuItemsForVariant(isAdmin, 'admin')` 显示"返回行情"链接到 `/market`，不重复显示"管理后台"（避免管理员在 `AdminAppShell` 内重复入口）；权限真源只来自后端 `users.is_admin` 字段，禁止前端臆造或缓存 admin 状态。
37. K 线右侧留白与交互契约（CHANGE-20260713-008）：`StrategyChart` 引入 `RIGHT_PADDING_RATIO = 0.20`（20% 留白，落在 18%-22% 区间）；`step = effectivePlotW / display.length`，`effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)`；bars 只占据绘图区前 80%，最新 K 线位于约 80% 位置；所有交互坐标映射（十字线/滚轮锚点/Pointer 拖拽/双击复位/节点/事件命中）统一使用 `step`，自动同步；网格线和十字线水平线仍延伸到 `g.plotRight`（保持全宽）；时间轴标签使用 `effectivePlotW`；不修改 Node/Profile/POC 算法、indicator_contract、盘中监控或 Capture 口径。
38. 批准 Logo 与视觉 V1.0 token（CHANGE-20260713-007）：`BrandLogo` 使用批准 PNG 资产（`logo_symbol_128.png` sidebar / `logo_horizontal_dark.png` landing/footer），禁止恢复手绘 SVG；运行资产位于 `frontend/src/assets/brand/`，ref 不作为运行时依赖；`variables.scss` 为唯一 token 真源（品牌 #00F6C2/#39F5CF/#00B28A；背景 #0A0F14/#111A23/#161F29；文字 #F2F6F8/#98A1B3/#657281；边框 #263440；上涨 #FF4D4F，跌幅 #22C55E；info #3882F6，warning #F59E0B）；品牌绿只用于 Logo、主按钮、选中、focus 和关键节点，不能替代涨跌色或所有信息蓝。
39. 数量契约四层语义（CHANGE-20260713-007）：`/strategy-runs/{run_id}/results` 响应包含四层数量：`source_total`（published run 原始总量，不受业务筛选影响，也不受 universe=all/watchlist 范围影响，恒等于 `run.total_instruments`）、`universe_total`（all/watchlist 范围总量，业务筛选前）、`filtered_total`（keyword+industry+concept+metric_filters 后总量）、`items`（filtered_total 当前页）；`len(items) <= filtered_total`；禁止文档描述"source_total 与筛选使用相同条件"或"source_total 受 universe 影响"。
40. 详情页来源上下文契约（CHANGE-20260713-009）：`/market` 点击股票名称进入 `/stock/:symbol?source=<src>&strategy=<strat>&returnTo=<完整当前 /market URL>`，`scope=market` 传 `source=selection&strategy=dsa_selector`，`scope=watchlist` 传 `source=watchlist&strategy=watchlist_monitor`；`returnTo` 解析使用共享纯函数 `decodeMarketListContext`（任意合法 `/market` URL 都识别为 market context，不要求 `keyword/page/sort` 存在，`scope=market` 和 `scope=watchlist` 都解析，仅含 `industry/concept` 的 URL 也有效）；`buildStrategyResultQueryParams` 将 `MarketListContext` 转换为 `StrategyResultQueryParams`（`scope=market`→`universe=all`，`scope=watchlist`→`universe=watchlist`，含 `keyword/industry/concept/sort/metric_filters/page/page_size` 完整转换）；详情页左侧来源列表必须复用 `usePublishedRuns('dsa_selector') + useStrategyRunResults(activeRunId, sourceListParams)` + `adaptStrategyResultToTrendRow` + `getStockDisplay` 链，禁止使用 `useMarketStocks`；`sourceListKind` 由 `marketContext.scope` 决定（`market`→"行情来源"，`watchlist`→"自选来源"）；`sourceBadge` 优先 `sourceListKind=market`→"行情来源"，否则 `source=selection`→"选股结果"，`source=watchlist`→"自选来源"；上一只/下一只基于恢复后的同一页 DSA results；`normalizeInternalReturnTo` 长度限制从 200 提升到 500（`/market` URL 含 `filters` JSON 编码后可能超过 200 字符）；`MarketWorkspacePage` 和 `useStockDetailActions` 共用 `decodeMarketListContext` + `buildStrategyResultQueryParams` 避免筛选口径漂移。
41. 个股详情市值与列表增强（CHANGE-20260713-010）：（1）**市值**：pytdx `get_finance_info` 每日 18:00 同步 `total_share`/`float_share`/`share_as_of` 到 `instruments` 表（migration 063），quote 端点从 DB 读取股本+当前价格计算 `total_market_cap`/`float_market_cap`，禁止用户请求时第三方联网；数据缺失返回 `market_cap_degraded_reason="market_cap_data_unavailable"` 不伪造；`StockQuoteStrip` 展示 8 项指标含总市值/流通市值，`formatMarketCap` 区分万/亿/万亿元，空值显示"--"。（2）**Excel 导出**：`POST /strategy-runs/{run_id}/results/export` 使用标准库 zipfile+XML 生成真实 .xlsx（禁止 openpyxl/xlsxwriter），`MAX_EXPORT_ROWS=10000` 超限返回 422，公式注入防护（=+-@前缀单引号），响应头含 X-Source-Total/X-Universe-Total/X-Filtered-Total/X-Export-Rows，`handleExport` 复用 `convertFiltersToMetricFilters`（与 `buildStrategyResultQueryParams` 同源），stock 列 `payload_key=null`（不导出操作列）。（3）**小 K 线**：`MiniKlineCard`（lightweight-charts v4 createChart+CandlestickSeries）+ `useMiniKlineData`（1d=80/1w=60/1mo=48，`refetchInterval:false`）+ `MarketRightPanel`（MiniKlineCard 顶部+EventStatePanel 底部）；面板收起 0 请求，只请求活动周期不预取三周期，chart 实例仅创建一次（useEffect `[]`），ResizeObserver 响应式+卸载清理 `disconnect()`+`chart.remove()`+ref 清空，timeframe 独立于 symbol。（4）**filterAlias**：`DataTableColumn.filterAlias?:'keyword'`，stock 列与顶部搜索共用唯一 keyword 真源；`KeywordFilterPopover` onApply/onClear 双向同步（`setGlobalQuery`+`onKeywordChange`）；列头激活状态基于 `effectiveKeyword`；`isKeyword` flag 区分不进入 filters state；URL sync `replace:true`+`skipNextUrlSyncRef` 避免循环；`currentConfig.keyword`/`applyPresetConfig` 共用 `effectiveKeyword`；stock/action 不入 `metric_filters`。

***

## 十三、质量门禁

### Ruff

新增或修改的 Python 文件必须 Ruff 零错误。\
全仓 Ruff 历史债务由 `tools/quality_baselines/ruff.json` 管控。\
禁止通过全局 ignore、批量 noqa、扩大 exclude 掩盖新增问题。

### Mypy

新增 backend/app Python 生产文件必须 mypy 零错误。\
全仓 mypy 历史债务由 `tools/quality_baselines/mypy.json` 管控。\
禁止批量 `type: ignore` 或关闭检查掩盖新增问题。

### 文档检查

每次 PR 至少运行：

```
python tools/check_docs_consistency.py
python tools/check_architecture.py
python tools/check_test_allowlist.py
python tools/update_docs.py --check

```

***

## 十四、分支和 PR

每个变更使用独立分支：

```
fix/<topic>
feat/<topic>
docs/<topic>
refactor/<topic>
chore/<topic>
experiment/<topic>

```

禁止直接改 main。

PR 必须说明：

```
1. 当前系统原来如何运行；
2. 本次为什么修改；
3. 修改了哪些代码；
4. 修改了哪些 docs/current；
5. 修改了哪些 docs/maps；
6. 新增哪个 CHANGE；
7. 是否改变 API；
8. 是否改变数据模型；
9. 是否改变 Worker 或第三方集成；
10. 测试结果；
11. 是否仍有 Known Gap；
12. 是否需要生产验证。

```

***

## 十五、完成报告格式

Trae 完成后必须输出：

```
当前分支：
Base Commit：
Head Commit：

一、修改前理解
- 当前产品行为：
- 当前系统地图依据：
- 当前代码入口：
- 当前文档依据：
- 当前冲突：

二、实际修改
- 代码文件：
- docs/current：
- docs/maps：
- docs/changes：
- tools：
- 测试：

三、一致性检查
- current 是否更新：
- maps 是否更新：
- CHANGE 是否新增：
- CHANGELOG 是否更新：
- archive 是否只作历史参考：
- 是否存在未登记冲突：

四、验证
- 执行命令：
- 测试结果：
- CI 状态：

五、剩余问题
- Known Gap：
- OPEN：
- 需要生产验证：

```

***

## 十六、最终规则

任何修改都必须满足：

```
当前设计文档
+ 系统实现地图
+ 真实代码
+ 测试
+ CHANGE
+ PR

```

六者缺一不可。

如果只是代码变了，文档没变，不算完成。\
如果只是文档变了，代码没核对，不算完成。\
如果 maps 过期，新对话会误解项目，也不算完成。

***

## 十七、提交安全与 Trae 工具通道规则

### 1. 禁止默认 git add -A / git add .

禁止使用 `git add -A`、`git add .`、`git add -u` 批量暂存。\
必须用精确文件列表逐个 `git add <file>`。

### 2. 提交前必须验证工作区

提交前必须执行并检查：

```
git status --short -uno
git status --short
git diff --name-only
git diff --stat
```

如果出现未预期文件、日志、csv/parquet、coverage、node_modules、venv、缓存，必须先汇报，不得 add。

### 3. 不得提交的文件

不得提交以下文件（已在 .gitignore / .git/info/exclude 排除）：

```
.vscode/settings.json
.traeignore
node_modules/
.venv/
venv/
__pycache__/
*.py[cod]
.mypy_cache/
.pytest_cache/
.ruff_cache/
.coverage
coverage.xml
htmlcov/
coverage/
dist/
build/
*.log
*.csv
*.parquet
```

### 4. 长命令执行规则

Trae 前台 RunCommand 不适合等待长命令（mypy 冷启动、大批量测试、research backfill）。

长命令必须用后台日志方式：

```
nohup bash -lc '<command>' > /tmp/<name>.log 2>&1 &
echo $! > /tmp/<name>.pid
```

然后用 `ps -p $(cat /tmp/<name>.pid)` 和 `tail /tmp/<name>.log` 轮询，不依赖 check_command_status 等待长连接。

### 5. 禁止删除

未经用户明确授权，禁止删除：

```
数据库卷
运行中容器
postgres/redis 数据目录
node_modules
.venv
.git
源码
生产数据
```

### 6. 磁盘与缓存维护

定期清理安全缓存以降低 Trae watcher/索引压力：

```
__pycache__、*.pyc
.mypy_cache、.pytest_cache、.ruff_cache
.coverage、coverage.xml、htmlcov、coverage
/tmp 下的诊断/日志临时文件
```

不得删除 `node:20-alpine` 镜像、不得 `docker image prune -a`。\
普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`。
