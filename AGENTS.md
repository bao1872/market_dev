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
8. 行情工作区：`/market` 渲染 `MarketWorkspacePage`（**无 K 线**，布局为 `MarketToolbar` + 服务端分页 `MarketStockTable` + 可收起 `EventStatePanel`）；`/market` 明确禁止挂载 `StockResearchWorkspace`/`StrategyChart`/任何 K 线组件；`/stock/:symbol` 渲染 `StockDetailPage`（唯一 K 线入口，复用 `useStockResearchData` + `StockResearchWorkspace`）；`useStockResearchData` 只保留 bars/indicators/quote/events 核心查询，详情页专属能力（自选/上下切换/memo/飞书）拆到 `useStockDetailActions`/`useStockDetailFeishu`；`/overview`、`/watchlist`、`/screener` 仅保留兼容重定向。
9. `timeframe` 单一真源：URL → `useStockResearchData`（bars/indicators 请求参数）→ `StockResearchWorkspace`（图表渲染）三者始终使用同一 `DisplayTimeframe`；工具栏切换必须通过 `onTimeframeChange` 回调写回 URL，禁止子组件 `useState` 维护独立 timeframe；图表显示周期不得改变 1d+15m 监控配置或 1m 事件触发口径；`/stock/:symbol` 的 timeframe 也从 URL 解析。
10. 请求门控：`useWatchlistMonitorStatus` 和 `useInstruments` 必须通过 `enabled` 参数按 scope 互斥启用（watchlist scope 只启用 monitor-status，market scope 且搜索词 trim 后 ≥2 字符才启用 instruments）；`useStockResearchData` 不得请求 `MarketWorkspace` 未使用的 watchlist/batchInstruments/stockMemo。
11. URL 状态保留：`/market` URL 的 scope/query/page/page_size/sort/selected/industry/concept/state 进入 URL（可分享、刷新恢复）；切换 scope、搜索、筛选、翻页、选行时必须保留其他字段；`selected` 由 `MarketStockTable` 单击行更新并驱动右栏 `EventStatePanel`；`returnTo` 为来源页 URL（从 `/screener`、`/messages` 进入 `/stock/:symbol` 时携带），返回按钮优先使用，必须经 `normalizeInternalReturnTo` 校验（仅允许 `/screener`、`/market`、`/messages` 前缀，拒绝外部 URL/`javascript:`/双斜杠/非白名单路径）。
12. 行情列表行选择：`MarketStockTable` 单击数据行更新 URL `selected=<symbol>` 并驱动右栏 `EventStatePanel` 加载该股票 context；点击股票名称/代码链接进入 `/stock/:symbol?returnTo=<编码后的当前 /market URL>`；切换 scope（watchlist 或 market）时保留 query/page/sort/industry/concept/state，`selected` 可清空。
13. 搜索与筛选门控：`MarketToolbar` 搜索词通过 URL `query` 参数传递，`useMarketStocks` 通过 `enabled` 门控（scope=market 且 query trim 后 ≥2 字符才启用 instruments 搜索，scope=watchlist 不搜索）；行业/概念筛选依赖 `boardsAvailable`（`false` 时禁用输入并显示"板块未开放"）；状态筛选通过 URL `state` 参数传递。
14. 行情状态文案：`StockResearchWorkspace` 不得在 15m/1h/1w/1mo 显示"日线回退"；非实时非降级时统一显示"行情回退"；partial 文案必须包含当前周期（如"盘中 partial bar（15m）"），禁止所有周期统一显示"日线"。
15. 共享研究核心：`DisplayTimeframe`/`ResearchSource`/`ALLOWED_TIMEFRAMES`/`BARS_COUNT_BY_TIMEFRAME`/`defaultStrategyForSource`/`normalizeDisplayTimeframe`/`normalizeResearchSource` 权威定义在 `frontend/src/features/stock-research/stockResearchTypes.ts`；`marketWorkspaceUrlState.ts` 从该文件导入并重新导出，依赖方向为 market-workspace → stock-research（禁止反向依赖）；`StockResearchWorkspace` 通过 `toolbar`/`rightPanel`/`showRightPanel`/`chartColumnProps` 可选 props 支持详情页结构面板开关和截图模式属性；`/capture/stock/:symbol` 完全独立，不使用 `useStockResearchData`/`StockResearchWorkspace`/`apiClient`。
16. 普通用户事件状态面板：`/market` 右栏 `EventStatePanel`（`frontend/src/features/research-context/EventStatePanel.tsx`）通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，展示 MACD 动量、Evidence（事件证据）、`state.evidence`（状态证据）、数据日期/质量、当前价格结构、成交密集区关系、最近状态变化时间线；面板首次默认收起（`rightPanelCollapsed=true`），localStorage key `panji:market-right-panel-collapsed:v1` 持久化用户选择；收起时不挂载 `EventStatePanel`、不请求 context；普通用户不显示内部字段名（`sourceField`）、算法参数、`idempotencyKey`、JSON 或商业机密；原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol` 展示。
17. 管理员调试路由独立：原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol`（`AdminRoute` + `AdminAppShell` 下）的 `AdminStockDebugPage` 中展示，复用 `MarketInstrumentPane`/`useStockResearchData`/`StockResearchWorkspace`/`useAdminStockDebug`（含原始 payload 的管理员调试接口）；`/market` 不得承载管理员调试能力，`debug` 不在 `/market` URL 契约中；`/market?debug=1` 管理员访问时重定向到 `/admin/stock-debug/:symbol`，普通用户忽略并清除。
18. 事件状态面板查询入口：`features/research-context/` 只含 `EventStatePanel`/`reasonCodeMessages`（纯函数 + 测试）；`EventStatePanel` 通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，不再调用 `useStrategyEventDetail`/`useStructuralFactors`/`useTemporalFeatures` 等散落 hooks（React Query 按 queryKey 去重，不产生重复请求）；`reasonCodeMessages` 将 `reasonCode`（如 `no_published_full_run`、`snapshot_missing`）映射为用户可读文案；不新增后端算法，接口缺失时显示对应 `reasonCode` 文案，禁止伪造数据；`ScreenerPage` 查看详情进入 `/stock/:symbol?returnTo=<原 ScreenerPage URL>`（K 线详情），`MessagesPage` 有股票时进入 `/stock/:symbol?event_id=...`；`/market` URL 契约为 `scope/query/page/page_size/sort/selected/industry/concept/state`，不得混入 `symbol/source/strategy/event_id`。
19. 图表图层 ≠ 策略因子：`IndicatorToolbar` 是 K 线图层开关唯一交互入口（`ChartLayerVisibility` 7 键：`trend/node/boll/volume/macd/sqzmom/breakout`），`StrategyChart` 不再渲染 `tv-strategy-legend` 只读行（已删除）；图层开关状态由 `StockResearchWorkspace` 持有单一 `layerVisibility` state，localStorage key `panji:chart-layer-visibility:v2`；`DISPLAY_GROUPS`/`DisplayGroupDef` 已删除，禁止恢复；图层 ≠ 策略因子：图层控制的是 K 线渲染（趋势/节点/布林/成交量/MACD/SQZMOM/突破），策略因子是后端 DSA 计算的结构因子（DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与），二者不可混淆。
20. MACD 语义：MACD 是 `feature_snapshot_service` 附加的日线辅助技术指标（标准 12/26/9），注入日线 `primary_factors.macd_state`；不是 bar 因子也不是时序特征；`structural_factor_service` 的 bar 因子只有：DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与。前端 `defaultChartLayerVisibility` 中 `watchlist` 和 `selection` 默认关闭 MACD（`macd: false`）；不删历史字段、不迁移、不回填。
21. 右栏按需加载：`/market` 和 `/stock/:symbol` 首次默认收起右栏（`rightPanelCollapsed=true` / `eventPanelCollapsed=true`），localStorage 持久化用户选择（`panji:market-right-panel-collapsed:v1` / `panji:event-panel:v1`）；收起时不挂载 `EventStatePanel`、不请求 `useStockContext`；展开后才挂载并请求数据；禁止在收起状态下预取数据。
22. 行情列表 boards 单一真源：`MarketWorkspacePage` 唯一调用 `useMarketBoards`，将 `boardsAvailable`/`industryOptions`/`conceptOptions` 以 props 传给 `MarketToolbar` 和 `MarketStockTable`；两个子组件不得再次请求 boards；`boardsAvailable=false` 时 `MarketToolbar` 禁用行业/概念输入并显示"板块未开放"，`MarketStockTable` 隐藏行业/概念列。
23. 行情列表 latest_event 兼容保留：`market_stocks_service` 已删除 `stock_state_event` 批量查询（固定 SQL 数从 9 条减为 8 条），`MarketStockRow.latest_event_title`/`latest_event_time` 兼容保留为 `null`；事件只在 `EventStatePanel` 按需展开时通过 `useStockContext` 加载；`MarketStockTable` 不显示"最近事件"列。

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
