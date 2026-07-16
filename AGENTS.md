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
8. 行情工作区：`/market` 渲染 `MarketWorkspacePage`（**无 K 线**，布局为 `MarketToolbar` + `StrategyDataTable`（复用 `getTrendSelectionColumns` DSA 列定义）+ 可收起 `AtomicFactsPanel`）；`/market` 明确禁止挂载 `StockResearchWorkspace`/`StrategyChart`/任何 K 线组件；`/market` 列表数据来自 `usePublishedRuns` + `useStrategyRunResults`（published DSA run），不再使用 `useMarketStocks`/`MarketStockTable`；`/stock/:symbol` 渲染 `StockDetailPage`（唯一 K 线入口，复用 `useStockResearchData` + `StockResearchWorkspace`）；`useStockResearchData` 只保留 bars/indicators/quote/events 核心查询，详情页专属能力（自选/上下切换/memo/飞书）拆到 `useStockDetailActions`/`useStockDetailFeishu`；`/overview`、`/watchlist`、`/screener` 仅保留兼容重定向。
9. `timeframe` 单一真源：URL → `useStockResearchData`（bars/indicators 请求参数）→ `StockResearchWorkspace`（图表渲染）三者始终使用同一 `DisplayTimeframe`；工具栏切换必须通过 `onTimeframeChange` 回调写回 URL，禁止子组件 `useState` 维护独立 timeframe；图表显示周期不得改变 1d+15m 监控配置或 1m 事件触发口径；`/stock/:symbol` 的 timeframe 也从 URL 解析。
10. 请求门控：`useWatchlistMonitorStatus` 和 `useInstruments` 必须通过 `enabled` 参数按 scope 互斥启用（watchlist scope 只启用 monitor-status，market scope 且搜索词 trim 后 ≥2 字符才启用 instruments）；`useStockResearchData` 不得请求 `MarketWorkspace` 未使用的 watchlist/batchInstruments/stockMemo。
11. URL 状态保留：`/market` URL 契约简化为 `scope/selected`（由 `MarketWorkspacePage` 管理）；`sort/dir/keyword/filters/page/page_size` 由 `StrategyDataTable` 内置 `screenerUrlState` 管理；切换 scope、搜索、筛选、翻页、选行时必须保留其他字段；`selected` 由 `StrategyDataTable` 单击行更新并驱动右栏 `EventStatePanel`；`returnTo` 为来源页 URL（从 `/screener`、`/messages` 进入 `/stock/:symbol` 时携带），返回按钮优先使用，必须经 `normalizeInternalReturnTo` 校验（仅允许 `/screener`、`/market`、`/messages` 前缀，拒绝外部 URL/`javascript:`/双斜杠/非白名单路径）。
12. 行情列表行选择：`StrategyDataTable` 单击数据行（`onRowClick`）更新 URL `selected=<symbol>` 并驱动右栏 `AtomicFactsPanel` 加载该股票 context；点击股票名称/代码链接进入 `/stock/:symbol?returnTo=<编码后的当前 /market URL>`；切换 scope（watchlist 或 market）时保留 sort/keyword/filters/page/page_size，`selected` 可清空。
13. 搜索与筛选门控：`StrategyDataTable` 内置全文搜索（URL `keyword` 参数）和列筛选（URL `filters` 参数，转 `metric_filters` 透传 DSA API）；`scope=market` → `universe=all`，`scope=watchlist` → `universe=watchlist`；行业/概念筛选已移除（DSA API 不支持）；状态筛选已移除（DSA 列表不含形态状态列）。
14. 行情状态文案：`StockResearchWorkspace` 不得在 15m/1h/1w/1mo 显示"日线回退"；非实时非降级时统一显示"行情回退"；partial 文案必须包含当前周期（如"盘中 partial bar（15m）"），禁止所有周期统一显示"日线"。
15. 共享研究核心：`DisplayTimeframe`/`ResearchSource`/`ALLOWED_TIMEFRAMES`/`BARS_COUNT_BY_TIMEFRAME`/`defaultStrategyForSource`/`normalizeDisplayTimeframe`/`normalizeResearchSource` 权威定义在 `frontend/src/features/stock-research/stockResearchTypes.ts`；`marketWorkspaceUrlState.ts` 从该文件导入并重新导出，依赖方向为 market-workspace → stock-research（禁止反向依赖）；`StockResearchWorkspace` 通过 `toolbar`/`rightPanel`/`showRightPanel`/`chartColumnProps` 可选 props 支持详情页结构面板开关和截图模式属性；`/capture/stock/:symbol` 完全独立，不使用 `useStockResearchData`/`StockResearchWorkspace`/`apiClient`。
16. 普通用户状态观察面板：`/market` 右栏 `AtomicFactsPanel`（`frontend/src/features/research-context/AtomicFactsPanel.tsx`）通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，展示 Atomic Fact Contract V1（AFC V1，详见 `docs/current/07-atomic-fact-contract-v1.md`）的 14 个 Core Fact + 8 个默认展示 Auxiliary Fact（T3/T6 默认 `ui_enabled=false`，**10 个 Aux 中仅 8 个可展开，T3/T6/V1 永不出现 DOM**）紧凑形态；面板首次默认收起（`rightPanelCollapsed=true`），localStorage key `panji:market-right-panel-collapsed:v1` 持久化用户选择；收起时不挂载 `AtomicFactsPanel`、不请求 context；普通用户不显示内部字段名（`sourceField`）、算法参数、`idempotencyKey`、JSON 或商业机密；原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol` 展示。**AFC V1 原子值 UI 改造（CHANGE-20260716-004）**：(a) `valueText` 为短原子值（非完整中文长句），如 T1=`上行`、T2=`+0.0123`+`ATR / 根日K`、T5=`1.23×`+`分类未启用`、S7=`1.23 ATR`+categoryLabel `尚未到达/已越过`；统一格式器 `_fmt_atomic_value` 读 presentation `valuePrecision`，禁止散落 `.4f/.6f`；(b) `visualKind` 统一枚举 `metric/value_with_category/relation/position/distance/ratio`，前端按 visualKind 渲染，**禁止解析中文推断类型/状态**（如不得 `valueText.includes('已越过')`）；(c) M5 `sqz_on`/`sqz_off` 任一缺失即缺失+双true质量异常，categoryLabel `正在收紧/正在释放/正常`；(d) `recentChanges` 含中文 `label`（非 publicKey）；(e) **持久化/调试分离**：`compute_atomic_facts()` 仅 core/aux/availability（无 debug）、`compute_atomic_fact_debug()` 管理员即时生成、`build_persisted_afc_payload()` 含四版本字段（`payloadVersion=1`/`researchContractVersion`/`researchFreezeVersion=V4.13`/`presentationVersion`）无 debug、`_is_valid_stored_afc` 严格校验四版本+四组+publicKey+无 debug，旧 worker 旧格式→fallback 兼容；(f) 前端 `FactRow` 按 visualKind 渲染去重（CSS Grid 透明行非卡片 `minmax(0,1fr) auto`）、S3 完整轨道（低位/0.33/0.67/高位+圆点+`0.63 · 中间`）、Auxiliary 按 动量补充/结构补充/成交补充 分组默认收起、Drawer 焦点 trap+关闭恢复焦点+body 滚动锁定。**AFC V1 终审修正（CHANGE-20260716-005）**：(g) M5 `_squeeze_state` 改 `if on is None or off is None: return None`（`or` 非 `and`），单侧缺失不进入 Core、不触发 `m5_inconsistent`；(h) Recent Changes 按每个 Fact 的 presentation `valuePrecision` 量化（`_quantize_fact_value`，禁止统一 `round(...,4)`），事实消失时 `dimension` 来自 `FACT_DIMENSION_BY_ID`（禁止默认 trend），`_combine_text` 组合 valueText + categoryLabel 保留 M3 双文本状态；(i) `PersistedAtomicFactsPayload` Pydantic Schema 严格校验（`extra="forbid"` + `model_validator`）：四版本完全匹配、core 键恰好四维度、每项通过 `PublicAtomicFactItem`、publicKey 属正确维度且无重复/未知、T3/T6/V1 不存在、availability 与实际数组及固定分母 14 一致、不含 debug，不兼容一律 fallback 不得 500；(j) `as_of` 改为截止日期语义（`trade_date <= as_of` + `ORDER BY trade_date DESC, published_at DESC, finished_at DESC LIMIT 1`），周末/无批次日期返回之前最近发布状态；(k) legacy 快照 `snapshot_run_not_linked`/`legacy_snapshot_ambiguous` 进入 `dataQuality.degradedReasons`（无 snapshot 才用 `reasonCode`，不得静默清除原因）；(l) API 响应新增 `meta`（`payloadVersion`/`researchFreezeVersion`/`presentationVersion`），**前端 Header 从 `data.meta.researchFreezeVersion` 读取，禁止硬编码 V4.13**；(m) presentation `secondaryLabel`/`unclassifiedLabel` 为真源（`_secondary_text_for` helper，移除服务中散落 "ATR / 根日K"/"个交易日"/"分类未启用" 常量）；(n) 前端 factRow secondary 右对齐（`grid-template-areas "label value" ". secondary"` + `text-align: right`）、PositionRow 独立布局（第一行 label/caption，第二行轨道横跨整组宽度，`railScale` `space-between` 预留刻度高度）、RecentChanges 显示 `deltaText`（变化类型文案）、Drawer 双向焦点限制（`!drawer.contains(active)` 正向 Tab 和 Shift+Tab 均回环）。
17. 管理员调试路由独立：原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol`（`AdminRoute` + `AdminAppShell` 下）的 `AdminStockDebugPage` 中展示，复用 `MarketInstrumentPane`/`useStockResearchData`/`StockResearchWorkspace`/`useAdminStockDebug`（含原始 payload 的管理员调试接口）；`/market` 不得承载管理员调试能力，`debug` 不在 `/market` URL 契约中；`/market?debug=1` 管理员访问时重定向到 `/admin/stock-debug/:symbol`，普通用户忽略并清除。
18. 状态观察面板查询入口：`features/research-context/` 只含 `AtomicFactsPanel`/`reasonCodeMessages`（纯函数 + 测试）；`AtomicFactsPanel` 通过 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一接口加载，不再调用 `useStrategyEventDetail`/`useStructuralFactors`/`useTemporalFeatures` 等散落 hooks（React Query 按 queryKey 去重，不产生重复请求）；`reasonCodeMessages` 将 `reasonCode`（如 `no_published_full_run`、`snapshot_missing`）映射为用户可读文案；不新增后端算法，接口缺失时显示对应 `reasonCode` 文案，禁止伪造数据；`ScreenerPage` 查看详情进入 `/stock/:symbol?returnTo=<原 ScreenerPage URL>`（K 线详情），`MessagesPage` 有股票时进入 `/stock/:symbol?event_id=...`；`/market` URL 契约为 `scope/selected`（MarketWorkspacePage 管理）+ `sort/dir/keyword/filters/page/page_size`（StrategyDataTable 内置 screenerUrlState 管理），不得混入 `symbol/source/strategy/event_id`/`industry/concept/state`。
19. 图表图层 ≠ 策略因子：`IndicatorToolbar` 是 K 线图层开关唯一交互入口（`ChartLayerVisibility` 7 键：`trend/node/boll/volume/macd/sqzmom/breakout`），`StrategyChart` 不再渲染 `tv-strategy-legend` 只读行（已删除）；图层开关状态由 `StockResearchWorkspace` 持有单一 `layerVisibility` state，localStorage key `panji:chart-layer-visibility:v2`；`DISPLAY_GROUPS`/`DisplayGroupDef` 已删除，禁止恢复；图层 ≠ 策略因子：图层控制的是 K 线渲染（趋势/节点/布林/成交量/MACD/SQZMOM/突破），策略因子是后端 DSA 计算的结构因子（DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与），二者不可混淆。
20. MACD 语义：MACD 是 `feature_snapshot_service` 附加的日线辅助技术指标（标准 12/26/9），注入日线 `primary_factors.macd_state`；不是 bar 因子也不是时序特征；`structural_factor_service` 的 bar 因子只有：DSA 段质量、Swing、成本/节点、BB+SQZMOM、成交参与。前端 `defaultChartLayerVisibility` 中 `watchlist` 和 `selection` 默认关闭 MACD（`macd: false`）；不删历史字段、不迁移、不回填。
21. 右栏按需加载：`/market` 和 `/stock/:symbol` 首次默认收起右栏（`rightPanelCollapsed=true` / `eventPanelCollapsed=true`），localStorage 持久化用户选择（`panji:market-right-panel-collapsed:v1` / `panji:event-panel:v1`）；收起时不挂载 `AtomicFactsPanel`/`AtomicFactsDrawer`、不请求 `useStockContext`；展开后才挂载并请求数据（详情页展开为 `AtomicFactsDrawer` overlay，关闭即卸载、context 请求为 0）；禁止在收起状态下预取数据。
22. 行情列表 boards 已移除：`/market` DSA 列表不再使用 boards 筛选（DSA API 不支持行业/概念筛选）；`MarketToolbar` 简化为仅 scope 分段按钮；`MarketStockTable` 已删除，由 `StrategyDataTable` + `getTrendSelectionColumns` 替代；`/market/boards` API 仍保留供其他用途，但 `/market` 列表不消费 boards。
23. 行情列表 latest_event 已移除：`/market` DSA 列表不显示形态状态/DSA状态/最近事件列；状态观察只在 `AtomicFactsPanel` 按需展开时通过 `useStockContext` 加载；`market_stocks_service` 仅用于 `/stock/:symbol` 详情页的 returnTo 上下文恢复（`useStockDetailActions` 通过 `useMarketStocks` 恢复来源列表）。
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
41. 个股详情市值与列表增强（CHANGE-20260713-010）：（1）**市值**：pytdx `get_finance_info` 每日 18:00 同步 `total_share`/`float_share`/`share_as_of` 到 `instruments` 表（migration 063），quote 端点从 DB 读取股本+当前价格计算 `total_market_cap`/`float_market_cap`，禁止用户请求时第三方联网；数据缺失返回 `market_cap_degraded_reason="market_cap_data_unavailable"` 不伪造；`StockQuoteStrip` 展示 8 项指标含总市值/流通市值，`formatMarketCap` 区分万/亿/万亿元，空值显示"--"。（2）**Excel 导出**：`POST /strategy-runs/{run_id}/results/export` 使用标准库 zipfile+XML 生成真实 .xlsx（禁止 openpyxl/xlsxwriter），`MAX_EXPORT_ROWS=10000` 超限返回 422，公式注入防护（=+-@前缀单引号），响应头含 X-Source-Total/X-Universe-Total/X-Filtered-Total/X-Export-Rows，`handleExport` 复用 `convertFiltersToMetricFilters`（与 `buildStrategyResultQueryParams` 同源），stock 列 `payload_key=null`（不导出操作列）。（3）**小 K 线**：`MiniKlineCard`（lightweight-charts v4 createChart+CandlestickSeries）+ `useMiniKlineData`（1d=80/1w=60/1mo=48，`refetchInterval:false`）+ `MarketRightPanel`（MiniKlineCard 顶部+AtomicFactsPanel 底部）；面板收起 0 请求，只请求活动周期不预取三周期，chart 实例仅创建一次（useEffect `[]`），ResizeObserver 响应式+卸载清理 `disconnect()`+`chart.remove()`+ref 清空，timeframe 独立于 symbol。（4）**filterAlias**：`DataTableColumn.filterAlias?:'keyword'`，stock 列与顶部搜索共用唯一 keyword 真源；`KeywordFilterPopover` onApply/onClear 双向同步（`setGlobalQuery`+`onKeywordChange`）；列头激活状态基于 `effectiveKeyword`；`isKeyword` flag 区分不进入 filters state；URL sync `replace:true`+`skipNextUrlSyncRef` 避免循环；`currentConfig.keyword`/`applyPresetConfig` 共用 `effectiveKeyword`；stock/action 不入 `metric_filters`。
42. 最新行情涨跌幅与 DSA 日期分离 + preset=none + 股票名称筛选 + 详情左栏滚动 + 五周期小 K 线（CHANGE-20260714-001）：（1）**latest_change_pct**：`change_pct` 列不再读 published DSA run 的 `payload.change_pct`，改从 `bars_daily` 表用 window function（`lag(close)` + `row_number() over partition by instrument_id order by trade_date desc`）取每只股票最新两根"已完成"日线计算 `latest_change_pct=(latest_close/prev_close-1)*100`；API 响应新增 `latest_change_pct`/`latest_change_trade_date` 字段（`StrategyResultResponse` schema + 前端 `StrategyResult`/`TrendSelectionRow` 类型）；服务端排序/列筛选/Excel 导出/详情左侧来源列表必须使用同一 `latest` 字段；`CHANGE_PCT_METRIC_KEY="change_pct"` 常量标记特殊处理分支（`_validate_metric_filters` 和 `sort_by` 校验添加例外）；前端涨跌幅列优先且只显示 `latestChangePct`，无两根有效日线显示 `--`，不得静默回退旧 run 值；表头 tooltip 显示"最新完成交易日"，单元格 title 显示具体 trade_date；DSA 其他列仍绑定 published run 日期；禁止新增表/migration/缓存，禁止逐行查询/N+1；`close IS NOT NULL` + `prev_close IS NOT NULL` + `prev_close != 0` 三重过滤。（2）**preset=none**：清除排序与筛选时 URL 写 `preset=none`，page=1，清 filters/sort/keyword/industry/concept；默认 preset effect 遇到 `preset=none` 跳过；用户主动应用 preset 时删除该值；returnTo 保留 `preset=none`，清除→详情→返回/刷新均不重新生效；不清列显示、列顺序、pageSize 或已保存 preset。（3）**股票名称独立筛选**：顶部 keyword 继续查代码/名称/拼音（OR 三字段），名称列独立支持 contains/not_contains/eq；URL/preset/API 使用 `stock_name`/`stock_name_op`；`stock_name_filter_helper.build_stock_name_conditions` 统一转义 `%`/`_`/`\` 并构造 ILIKE/NOT ILIKE/eq；items/filtered_total/Excel/详情来源复用同一条件，禁止进入数值 metric_filters。（4）**详情左栏**：`SourceStockItem` 含 `changePct`；DSA 来源使用 `latestChangePct`，自选 fallback 用 `useWatchlistMonitorStatus` 单次聚合查询，禁止逐行 quote；切换股票前保存 `scrollTop` 到 sessionStorage，加载后恢复，active 行仅完全离开视口时 `scrollIntoView(nearest)`；保留 source/strategy/returnTo/timeframe。（5）**AdminAppShell 返回行情**：桌面侧栏和小屏 topbar 增加始终可见"← 返回行情"（`APP_ROUTES.market`），AccountMenu 入口保留。（6）**五周期小 K 线**：删除"小K线"标题，周期改为 15m/60m/日/周/月（API 映射 15m/1h/1d/1w/1mo），segmented control 风格（高度 26-28px、`#263440` 边框、10px 圆角、active `#00F6C2`），`layout.attributionLogo=false` 禁止 TV 标志，card `width:100%;max-width:100%;min-width:0;box-sizing:border-box;overflow:hidden` 修复外框裁切，15m/1h 读 `trade_time` 转 Unix 秒、日周月读 `trade_date`，仅请求活动周期。
43. SMC 智能资金指标 + MiniKline viewport P0 修复（CHANGE-20260715-001）：（1）**SMC 算法真源**：`backend/app/strategy_assets/algorithms/features/smc_indicator.py` 基于用户提供的 `ref/smc.py` Python 重写版本（非 LuxAlgo Pine 源码翻译）；不引用、不依赖 LuxAlgo Pine 源码；仅依赖 stdlib（logging/dataclasses/typing），禁止引入 numpy/pandas/plotly/pytdx；默认参数与 ref/smc.py `build_parser()` 默认值一致（swings_length=50, equal_length=3, equal_threshold=0.1, internal_filter_confluence=False, internal_ob_size=5, swing_ob_size=5, order_block_filter="Atr", order_block_mitigation="High/Low", show_internal_order_blocks=True, show_swing_order_blocks=False, show_equal_hl=True, show_high_low_swings=True, show_swings=False）。（2）**FVG 完全排除**：Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关；生产计算路径不包含 FVG 函数或状态；输出结构中不存在 FVG 相关键、事件或 box；测试/注释/文档可以正常写"FVG 不计算、不返回、不显示"；禁止用动态拼接、混淆或隐藏 FVG 字符串迎合源码扫描；FVG 验收方式为输出级别断言（检查 result keys/events/order_blocks/equal_highs_lows/params/state 不含 FVG），不是源码字符串扫描。（3）**include_smc 按需计算**：`compute_all_indicators` 新增 `include_smc: bool = False` 参数；`include_smc=False` 时跳过 SMC 计算（0 CPU），响应无 smc layer；`include_smc=True` 时计算 SMC 并注入 smc 图层；SMC 输入使用当前 timeframe 对应 `macd_bars`（与 MACD/SQZMOM 同源）；SMC 是独立图层，不进入 DSA、Node 监控、Capture 或右栏 context。（4）**缓存隔离**：`indicator_cache.ALGORITHM_VERSION` 从 v5 bump 到 v6（旧 v5 缓存自动失效）；`include_smc=True` 时缓存键追加 `:smc` 后缀；SMC 与非 SMC 结果独立缓存，同 symbol/timeframe 切换开关不返回旧缓存；SMC 默认关闭（不带 `:smc` 后缀）。（5）**API 契约**：`/api/v1/instruments/{id}/indicators` 新增 `include_smc` 查询参数（bool，默认 false）；缺省/false 时响应无 smc layer 且不计算；true 时返回 smc layer（含 renderer/direction_colored/颜色/data）。（6）**前端图层 7→8**：`ChartLayerKey` 新增 `'smc'`（trend/node/boll/volume/macd/sqzmom/breakout/smc）；`CHART_LAYER_MANIFEST` 8 条目（smc: name="智能资金", kind="main", 默认 false）；`defaultChartLayerVisibility` watchlist 和 selection 的 smc 默认 false；旧 localStorage 偏好缺少 smc 时迁移为 false；smc 不是 selectionOnly（watchlist 和 selection 都能切换）；`StrategyChart` SMC Canvas 渲染（BOS/CHoCH 事件 + OB 矩形 + EQH/EQL 标记）；SMC 只进入 `/stock` 指标链，`/market` 右栏小 K 线（`MiniKlineCard`）不显示 SMC。（7）**anchor/confirmed 因果契约**：每个事件含 `anchor_index`/`anchor_time` 与 `confirmed_index`/`confirmed_time`；pivot.anchor=ref_i（i-size），pivot.confirmed=i（leg change 确认 bar）；BOS/CHoCH.anchor=pivot.barIndex（被穿越的 pivot bar），BOS/CHoCH.confirmed=i（close 穿越 pivot 的 bar）；OB.anchor=parsed_index（OB bar），OB.confirmed=current_i（触发 OB 创建的 BOS/CHoCH bar）；EQH/EQL.anchor=prev piv.barIndex（前一 pivot），EQH/EQL.confirmed=i-size（新 pivot bar）；Mitigation.confirmed=i（close/high/low 穿越 OB 的 bar）；API 事件时间使用 confirmed；可视化从 anchor 画到 confirmed；未来 bar 不得修改已确认事件（事件一旦写入即不可变）；events 新增 `bias` 字段（BULLISH=1/BEARISH=-1）与 OB 输出一致。（8）**MiniKline viewport P0**：`frontend/src/features/market-workspace/miniKlineViewport.ts` 纯函数 `computeMiniKlineViewport` 替代 `fitContent`；per-timeframe clamp（全部在 [30, 64] 区间内）：15m/60m 50–64，日线 48–58，周线 40–52，月线 30–40；`visibleBars = floor((contentWidth - 56) / 5)` clamp 到 per-timeframe 区间；`from = max(0, dataLength - visibleBars)`；`to = dataLength - 1 + 3`（右侧 3 bar 留白，最新 K 线不紧贴右轴）；切周期不沿用旧 range（每次重新计算）；价格轴宽度固定 56（`MIN_PRICE_SCALE_WIDTH`）；`effectivePlotWidth = floor(contentWidth) - 56`；空数据返回零区间；contentWidth 整数化（亚像素不抖动）；禁止重新使用 `fitContent` 覆盖自定义 logical range。
44. SMC Pine parity 核心 + MiniKline viewport 重写 + SMC renderer 对齐（CHANGE-20260715-002）：（1）**Pine 语义核心**：`backend/app/strategy_assets/algorithms/features/smc_pine_core.py` 为唯一 Pine 语义核心（生产服务和测试共同调用，禁止维护两套近似算法）；`smc_indicator.py` 重构为薄包装（委托 `compute_smc_pine`，`_SMCState = _SMCPineState` 别名，签名不变）；Pine 原语实现：`pine_rma(src,length)` Wilder RMA（SMA 播种 + 递推 `rma[i]=(rma[i-1]*(length-1)+src[i])/length`，前 `length-1` 个为 NaN），`pine_atr(highs,lows,closes,length)=pine_rma(pine_true_range,length)`，`pine_cumulative_mean_range`（`ta.cum(ta.tr)/bar_index`，bar0=NaN 除零），`pine_highest/lowest`（滚动极值不含当前 bar），`pine_crossover/crossunder`；`_SMCPineState` 状态机完全按 Pine lines 766-807 执行顺序（**trailing→swing→internal→equal→BOS/CHoCH→mitigation**，trailing 必须在 getCurrentStructure 之前，CHANGE-20260715-003 修复）；默认参数逐项匹配原始 Pine（Historical/Colored、internal structure=true/size=5/All/confluence=false/tiny、swing structure=true/length=50/All/small、Strong/Weak=true、internal OB=true/5/ATR/High/Low、swing OB=false、EQH/EQL=true/bars=3/threshold=0.1/tiny、其他=false）；events 使用 `internal: bool`（true=internal,false/缺失=swing）替代旧 `kind` 字段；用户 Pine 代码（`ref/smc_user_source.pine`，SHA256 0bd3d2ad，843 行）为原创作品并授权盘迹使用，不再涉及第三方许可证问题。（2）**FVG 完全排除**：不计算、不返回、不缓存、不渲染，不暴露 FVG 开关；FVG 排除不得改变其他逻辑的索引、执行顺序和右侧延伸；FVG 验收为输出级别断言（result keys/events/order_blocks/equal_highs_lows/params/state 不含 FVG），不是源码字符串扫描。（3）**warmup 契约**：1d timeframe 使用 `full_daily_bars`（DB 全量日线，≥500 warmup，在 `daily_bars.tail(daily_count)` 截断前保存）；其他周期复用 `macd_bars`（15m≈12000、1h≈3000、1w≈714、1mo≈166，均为可获得最大历史）；不调用 `_truncate_lists` 截断 SMC 输出（time 数组需完整长度对齐 anchor/confirmed 索引）；前端 `smcToDisplay` 通过时间匹配自动过滤展示区外事件；不得只用展示区可见 bars 初始化状态。（4）**缓存隔离**：`indicator_cache.ALGORITHM_VERSION` v6→v7（旧 v6 SMA 缓存强制失效）；`:smc` 后缀隔离不变；禁止 Redis FLUSHDB/FLUSHALL，只允许精确 DEL 测试键。（5）**StrategyChart SMC renderer 对齐 Pine**：`SmcEvent.kind?` → `internal?: boolean`；`SmcOrderBlock` 新增 `internal?: boolean`；BOS/CHoCH 线型按 scope 区分（internal=虚线 `[4,3]` + tiny 8px，swing=实线 + small 11px），不再按事件类型区分；标签位置为中点 `(x1+x2)/2` + `'center'` 对齐；trailing 文案"强高/弱高/强低/弱低"（强高 if `swingBias===-1` else 弱高；强低 if `swingBias===1` else 弱低）；OB 半透明 box（active alpha 0.12，mitigated alpha 0.05）；颜色多头红 `#FF4D4F`、空头绿 `#22C55E`（A 股红涨绿跌）；Historical 模式绘制全部事件（不因标签碰撞删除，只允许调整标签偏移）；internal OB 默认显示最近 5 个有效区域，从创建 bar 延伸到 mitigation 或当前最右端；拖拽/缩放/复位/周期切换后所有线/标签/OB 与 K 线共用相同 viewport 映射。（6）**MiniKline viewport 彻底重写**：目标根数按周期固定（15m=48、60m=44、日=40、周=36、月=30）；`barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)`（窄宽度时减少根数）；左侧 1-2 根留白 `from=max(-2, n-visible-1)`；右侧 3 根留白 `to=n-1+3`；禁止调用 `fitContent`/`resetTimeScale`/`scrollToRealTime` 覆盖 range；`candlestick series` 设置 `autoscaleInfoProvider` 扩展价格范围（上方 12%，下方 15%）；`rightPriceScale` `autoScale=true` + `scaleMargins {top:0.08, bottom:0.08}` + `minimumWidth=56`；`setData` 后在 `requestAnimationFrame` 应用 range；symbol/timeframe/width 变化重新应用，切周期不复用旧 logical range；图表容器高度固定 190px，card 不得存在多余 min-height 或底部空白；15 项纯函数测试 + 组件级 mock 测试（断言 `setData` 后调用 `setVisibleLogicalRange`、`autoscaleInfoProvider` 生效、resize 重算、无 `fitContent`）。（7）**Pine golden fixture**：状态 PENDING（等待 TradingView 导出）；`backend/tests/fixtures/smc_pine/README.md` 提供 TV 导出步骤、隐藏 plot 代码、CSV 格式规范；fixture 包含美诺华 603538 日线 1000 根 + 一个 15m 样本；golden 测试比较事件有序序列 `(type,scope,bias,anchor,confirmed,level)` 和 OB 序列 `(bias,anchor,top,bottom,mitigated)` 完全相等（浮点容差 1e-8）；无 fixture 时 `TestPineGoldenFixture` skip；**没有 Pine golden fixture 不得宣称"完全对齐"**。（8）**架构边界**：SMC 仅属于 `/stock` 指标链，默认关闭且 `include_smc=false` 时 0 计算；`/market` 右栏不请求 SMC；true/false 缓存键隔离；DSA/Node/监控/Capture/published run 不修改；无新表/migration/worker/历史回填；不使用 `docker cp` 验收，运行代码必须来自最终镜像 SHA；删除"基于 ref/smc.py 且非 Pine 翻译即可视为完成"的旧结论，改为 Pine 语义兼容目标 + golden fixture + exact 默认参数 + warmup/复权契约 + FVG 排除 + SMC renderer 契约 + MiniKline 真实 viewport/autoscale 契约。
45. SMC trailing 顺序修复 + 行情列表 sticky 修复 + 工具栏对齐 + MiniKlineCard 契约测试（CHANGE-20260715-003）：（1）**SMC trailing 执行顺序修复**：`smc_pine_core.py` 的 `_SMCPineState.run()` 中 `update_trailing_extremes` 移到循环体最前面（第1步），在任何 `get_current_structure` 之前；Pine 条件 `if showHighLowSwingsInput or showPremiumDiscountZonesInput`（`show_high_low_swings` 默认 true）；trailing 必须在最前面：Pine 中 `updateTrailingExtremes` 用当前 bar 的 high/low 更新 `trailing.top/bottom`，然后 `getCurrentStructure` 检测到新 swing pivot 时会覆盖 `trailing.top/bottom` 为新 pivot level；若顺序颠倒，trailing 会被当前 bar 的 high/low 二次覆盖，与 Pine 不一致。（2）**Pine 真源文件命名**：`ref/smc_ref.txt` → `ref/smc_user_source.pine`（SHA256 0bd3d2ad，843 行，内容相同）；`smc_pine_core.py` docstring + AGENTS clause 44 更新引用路径；`ref/smc_ref.txt` 保留作为历史路径别名。（3）**Bug 2 sticky 列固定宽度**：`global.scss` 的 `.interactive-table` 定义 CSS 变量 `--stock-col-width: 150px` 和 `--select-col-width: 40px`（header/body 共用）；`.sticky-col` 设置 `width/min-width/max-width: var(--stock-col-width)` 固定宽度；`td.sticky-col` 内部 div/`.symbol`/`.symbol-sub`/`.stock-name-btn` 添加 `overflow: hidden; text-overflow: ellipsis; white-space: nowrap`；`th.sticky-col .th-shell` 添加 `max-width: 100%; overflow: hidden`；背景不透明；z-index 高于普通列。（4）**Bug 3 工具栏 sticky 对齐**：`.table-meta-bar`（配置/列设置/清除/导出）和 `.table-pager`（分页器）添加 `position: sticky; left: 0; width: 100%; z-index: 6`；横向滚动时保持可见，右边界与表格可视区一致。（5）**MiniKlineCard 契约测试**：新增 `miniKlineCardContract.test.ts`（15 项源码契约测试，使用 `readFileSync` + 正则模式验证）：不调用 `fitContent`/`resetTimeScale`/`scrollToRealTime`、调用 `setVisibleLogicalRange`、使用 `computeMiniKlineViewport` 纯函数、`autoscaleInfoProvider`、`ResizeObserver` + `disconnect()`、`requestAnimationFrame`、五周期按钮、`attributionLogo: false`、图表高度 190px、`minimumWidth=56`、`autoScale: true` + `scaleMargins {0.08, 0.08}`、`shiftVisibleRangeOnNewBar: false`、`chart.remove()` 卸载清理、A 股配色、容器宽度 `Math.floor` 整数化。（6）**Parity 文档**：新增 `docs/analysis/smc-user-pine-parity.md`（674 行，14 章节，逐项 Pine→Python 对照表）。
46. Bug 1 修复（详情左栏 loading 占位）+ Pine 真源文件入 Git 跟踪（CHANGE-20260715-004）：（1）**Bug 1 修复**：`useStockDetailActions` 新增 `sourceListLoading: boolean` 字段（`hasMarketContext=true` 时为 `publishedRunsQuery.isLoading || !activeRunId || sourceResultsQuery.isLoading`；`hasMarketContext=false` 时为 `monitorStatusQuery.isLoading`）；`StockDetailPage` 新增 loading 占位渲染分支（`sourceListLoading=true` → 渲染 `<aside data-testid="detail-source-list-loading" class="tv-source-list tv-source-list-loading">` 含 header 和 `<div class="tv-source-list-placeholder">加载中…</div>`；`!sourceListLoading && sourceStocks.length > 0` → 渲染正常列表）；`global.scss` 新增 `.tv-source-list-placeholder { padding: 16px 10px; font-size: 12px; color: #778297; text-align: center; }`；用户从 `/market` 进入 `/stock/:symbol` 后左栏不再空白一段时间才出现列表。（2）**Pine 真源文件入 Git 跟踪**：`ref/smc_user_source.pine`（用户原创 Pine 源码，SHA256 0bd3d2ad，843 行，50084 bytes）使用 `git add -f` 强制纳入 Git 跟踪；`.gitignore` 仍排除 `ref/` 目录下其他文件，仅此单文件例外；未来 `ref/` 目录新增其他文件仍被忽略。（3）**契约测试**：新增 `detailSourceLoadingContract.test.ts` 9 项源码契约测试（sourceListLoading 字段存在、loading 占位渲染、列表渲染条件排除 loading、header 显示来源类型、CSS 类存在、`handleNavigateToStock` 显式传 source/strategy、URL 完整性含 source+strategy+returnTo、`useStockDetailActions` 不使用旧 `useMarketStocks` 函数调用、上一只/下一只保留 returnTo）。（4）**遗留**：Pine golden fixture 仍 PENDING（`backend/tests/fixtures/smc_pine/` 只有 README.md，需用户从 TradingView 导出 CSV）；Bug 1 修复需生产 E2E 验证（DSA 数据加载中不空白、上一只/下一只保留 source/strategy/returnTo）；8 项 baseline contract 失败需独立修复（4 项 filterAlias=keyword + 4 项 Excel 导出，与本变更无关）。
47. 详情左栏来源状态四态拆分 + 表格 sticky 列和工具栏对齐根治（CHANGE-20260715-005）：（1）**来源状态四态拆分**：`useStockDetailActions` 新增 `sourceListError`/`sourceListEmpty`/`sourceContextInvalid` 三个布尔字段，与 CHANGE-004 的 `sourceListLoading` 共同构成 loading/error/empty/invalid 四态；`sourceListError`=`publishedRunsQuery.isError || sourceResultsQuery.isError`；`sourceListEmpty`=`!sourceListLoading && !sourceListError && sourceStocks.length === 0`；`sourceContextInvalid`=`source === 'selection' && (!decodedMarketContext || !decodedMarketContext.scope)`（returnTo 解析失败或非 `/market` 前缀）。（2）**source 参数优先级**：显式 `source` 参数 > `returnTo` 推断；`source === 'selection'` → `sourceListKind='market'`（即使 returnTo 无效也不回退 watchlist，仅设置 `sourceContextInvalid=true`）。（3）**normalizeInternalReturnTo 上限提升**：长度限制从 500 提升到 4096（复杂筛选 URL 含多 filters + industry + concept + keyword + sort 编码后可能超过 500）。（4）**表格结构 `table-wrap` → `table-shell`**：`table-shell > meta-bar + search-bar + table-scroll > table + pager`；只有 `table-scroll` 设置 `overflow-x: auto`；meta-bar/search-bar/pager 移出横向滚动容器，右边界自然等于 table-scroll 右边界；删除 `position:sticky;left:0;width:100%` 补丁（不再需要）；`AdminAfterClosePipelinePage` 同步迁移到 `table-shell` + `table-scroll` 结构。（5）**sticky 列统一判断**：`isStickyColumn(col)` 函数只允许 `col.key === 'stock'` 为 sticky 列；header 和 body 共用同一判断；删除死 CSS `.sticky-col-change-pct`（涨跌幅列保持普通列）。（6）**viewport-sticky 模式**：`.table-shell.viewport-sticky .table-scroll { overflow: visible; }`（viewport sticky 模式下 table-scroll 不滚动，由外层容器滚动）。
48. MiniKline 闭包根治 + SMC Pine 对齐 RMA NA 语义 + 首个 pivot off-by-one + EQH/EQL 三时间点（CHANGE-20260715-006）：（1）**MiniKline 闭包根治**：`MiniKlineCard.tsx` 新增 `barsLengthRef`/`timeframeRef` 持有最新值（每次 render 同步，在 effects 之前，确保 effect 内读到最新值）；`applyViewportRange` 改为 `useCallback([], )` 稳定函数从 refs 读取最新值（不再直接闭包捕获 `bars.length`/`timeframe`）；新增 `scheduleApplyRange` 稳定函数（`useCallback([applyViewportRange], )`），取消 pending rAF 后调度新 rAF；`ResizeObserver` 回调调用 `scheduleApplyRange`（不直接闭包捕获 bars/timeframe，避免 mount 时回调使用首次 render 的 stale 值）；卸载清理函数取消 pending rAF（`cancelAnimationFrame`）。（2）**pine_rma NA 语义修复**：`smc_pine_core.py` 的 `pine_rma(src, length)` 严格复现 Pine v5 `ta.rma`：`bar_index < length-1` 返回 `na`（非逐步 SMA 的 min_periods 行为）；`bar_index == length-1` 写入 `SMA(src, length)` 种子；`bar_index >= length` 使用 Wilder 递推 `rma[i] = (rma[i-1]*(length-1) + src[i]) / length`；旧实现错误地在 `bar_index < length-1` 时返回逐步 SMA，导致 ATR(200) 在前 199 根产生非 na 值。（3）**首个 pivot off-by-one 修复**：`start_of_new_leg`/`start_of_bearish_leg`/`start_of_bullish_leg` 从 `i > size` 改为 `i >= size`；`get_current_structure` 从 `if i <= size: return` 改为 `if i < size: return`；首个 leg/pivot 在 `i == size` 检测（Pine `ta.change(leg)` 在 `bar_index == size` 时可首次非零，旧代码延迟到 `i == size+1`）。（4）**EQH/EQL DTO 三时间点**：EQL 和 EQH 两处新增 `detection_index`/`detection_time`（leg change 确认 bar, `i`），与 anchor（前一 pivot bar, `piv.bar_index`）/confirmed（新 pivot bar, `ref_i=i-size`）分离；三时间点语义：anchor=前一 pivot bar 位置、confirmed=新 pivot bar 位置、detection=leg change 确认 bar 位置（`i`）。（5）**核对通过**：ATR200=`pine_rma(tr, 200)`、highest/lowest 窗口 `[ref_i+1, ref_i+length+1]`（不含当前 bar）、crossover/crossunder NaN→False（Python NaN 比较返回 False，匹配 Pine na→falsy）、OB slice `[piv.bar_index, current_i)` end-exclusive（Python 切片天然 end-exclusive）、trailing 顺序 `update_trailing_extremes → getCurrentStructure(50) → getCurrentStructure(5) → getCurrentStructure(3) → displayStructure → deleteOrderBlocks`。（6）**Golden fixture**：仍为 `PINE_OUTPUT_GOLDEN_PENDING`（`backend/tests/fixtures/smc_pine/` 只有 README.md），无 fixture 时 `TestPineGoldenFixture` 自动 skip，不得宣称"完全对齐"。（7）**SMC 隔离边界不变**：SMC 仅进入 `/stock` 指标链，默认关闭（`include_smc=false` 时 0 计算）；`/market` 右栏小 K 线不请求 SMC；true/false 缓存键隔离（`:smc` 后缀）；不新增表/migration/worker/依赖。

49. SMC 核心 crossover/crossunder Pine 语义修正 + 缓存 v8→v9 + swing_bias 直接返回（CHANGE-20260716-001）：（1）**crossover/crossunder level_curr/level_prev 快照修正**：`display_structure` 旧实现错误地将 `piv.current_level` 同时作为 curr 和 prev（`pine_crossover(close_curr, close_prev, current_level, current_level)`），丢失 pivot level 自身逐 Bar 变化信息；修正为每 Bar 快照六个 pivot level（swing/internal 的 high/low 及 equal 状态，按 swing/internal 独立，不能用当前新 pivot 覆盖上一 Bar series 语义），`displayStructure` 接收 `level_curr`（当前 Bar pivot level）和 `level_prev`（上一 Bar pivot level）——crossover=`close_curr > level_curr && close_prev <= level_prev`，crossunder=`close_curr < level_curr && close_prev >= level_prev`，上一值为 NaN 时必须 false；保留三套独立 leg 调用状态；用 golden 事件有序序列验证，不凭内部测试宣称一致。（2）**EQH/EQL DTO 统一**：`anchor_*`=前一 pivot；`second_pivot_*`=新 pivot 所在 `i-size`（**视觉线端点**）；`confirmed_*`=当前检测 Bar `i`（**因果/回放使用**）；`ref_i` 不得命名为 `confirmed`；视觉线画到 second_pivot，因果/回放使用 confirmed。（3）**swing_bias 直接返回**：`swing_bias` 直接返回 `state.swing_trend.bias`（值域 {1, -1, 0}）；前端从 DTO `swing_bias` 字段读取，禁止从可见事件猜测（旧实现前端从可见事件推断 Strong High/Strong Low/Weak，存在推断错误风险）。（4）**缓存版本 v8→v9**：`indicator_cache.ALGORITHM_VERSION` 从 "v8" bump 到 "v9"（旧 v8 缓存自动失效，crossover/crossunder 修正后 BOS/CHoCH 触发时点可能变化）；SMC/non-SMC 键继续隔离（`include_smc=true` 追加 `:smc` 后缀）。（5）**风险**：crossover/crossunder 修正后 BOS/CHoCH 触发时点可能提前或延后一根 Bar（取决于 pivot level 变化方向），需 golden fixture 验证；未提供 fixture 前不得宣称完全对齐。

50. SMC 前端 viewport 区间求交渲染 + anchor_index 统一 + slice(0,5) + 标签不加 ·I + 纵轴候选 + Canvas mock 行为测试（CHANGE-20260716-001）：（1）**anchor_index 统一**：后端 OB 输出字段是 `anchor_index`，前端接口和渲染旧读取 `bar_index` 导致 OB 全部跳过；统一为 `anchor_index`（`bar_index` 旧字段已废弃）。（2）**viewport 区间求交**：BOS/CHoCH/EQH/EQL/OB 不得要求 anchor 与 confirmed 都在 `displayTimes` 中，只要区间与 viewport 相交就绘制——anchor 在左侧时 `x1=plotLeft` 并标记 `clipped_left`，confirmed/mitigation 在右侧时 clamp 到 `plotRight`，仅完全不相交时跳过；Historical 模式保留全部相交事件。（3）**OB 选择**：只显示数组头部最近 5 个 `internal && !mitigated` OB；活动 OB 从 `anchor_index` 延伸到 mitigation 或右端。（4）**标签不加 `·I` 后缀**：与 TV 文字一致；internal/swing 区别仅靠线型（internal 虚线 `[4,3]` + tiny 8px，swing 实线 + small 11px）。（5）**纵轴候选完整**：加入可见 event.level、OB high/low、EQH/EQL level、trailing top/bottom，避免事件存在但被 Canvas 裁掉。（6）**纯函数拆分 + Canvas mock 行为测试**：映射、区间求交、OB 选择、价格候选拆分为纯函数 `frontend/src/components/smcRendering.ts`，配合 Canvas mock 行为测试（`smcRendering.test.ts`），禁止只用源码正则。（7）**EQH/EQL 视觉线端点**：使用 `second_pivot_index`（CHANGE-20260716-001：anchor=前一 pivot，second_pivot=新 pivot 所在 `i-size`，confirmed=当前检测 Bar `i`）。

51. MiniKline CSS 重做 + barSpacing 应用 + 真实左留白 + 五等分 tabs（CHANGE-20260716-001）：（1）**单一方案**：按周期取目标根数 15m=48/60m=44/日=40/周=36/月=30，将 `bars.slice(-target)` 传给 series（不再用旧 `BARS_COUNT={15m:120,60m:120,1d:80,1w:60,1mo:48}` 值）。（2）**真实左 2/右 3 留白**：`setData` 后设置 logical range `{from:-2, to:visibleData.length-1+3}`，形成真实左 2/右 3 空位（不再用 `from=max(-2, n-visible-1)` 依赖 visibleBars 的假留白）；禁止 `fitContent`/`resetTimeScale`/`scrollToRealTime` 和第二套 `rightOffset`。（3）**删除死 barSpacing 计算**：旧 `barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)` 只计算未通过 `timeScale.applyOptions` 应用，是死参数；如需应用必须明确通过 `applyOptions`，不得保留死参数。（4）**autoscale**：只基于当前 visibleData 的 high/low，上方 12%/下方 15%。（5）**CSS 明确**：tabs 改为五等分全宽 grid；chart CSS 明确 `height:190px`；card 无多余空白；价格轴 56px。（6）**切 symbol/周期**：先清旧 data，取消旧 rAF，resize 后使用最新数据。（7）**真实 mock 测试**：断言 setData 根数、range、五周期切换、ResizeObserver cleanup、chart.remove 和 0 旧数据残留。

52. indicator_service required_inputs 优化 — 避免无条件读取 750 天 15m/1m（CHANGE-20260716-001）：（1）**`_REQUIRED_INPUTS` 映射**：为每个注册策略定义所需 bar 类型集合——`dsa_selector`={daily}、`volume_node_monitor`={daily,15min,minute}、`bb_monitor`={daily}、`watchlist_monitor`={daily}；`_determine_required_bars()` 合并所有注册策略需求返回 `frozenset[str]`。（2）**按数据类型独立回看天数**：15min=400 天（limit=NODE_CLUSTER_LOW_BARS=4000，VP 需要 4000 根，4000/16=250 交易日≈350 日历日）；minute=5 天（limit=NODE_CLUSTER_MINUTE_BARS=2，VP crossover 仅需 2 根）；60min=750 天（1h 指标需要完整历史，独立常量 `_60MIN_LOOKBACK_DAYS`）。（3）**条件查询**：`needs_15min = "15min" in required_bars or timeframe == "15m"`（timeframe==15m 时 `macd_bars=bars_15min`，必须加载）；`needs_minute = "minute" in required_bars`；不需要时 `bars_15min`/`bars_minute` 为空 `pd.DataFrame()`。（4）**`_query_minute_bars` 新增 `limit` 参数**：DESC + LIMIT + 反转为升序，与 `_query_15min_bars` 一致，避免加载全量再截取。（5）**算法行为不变**：VP 内部 `_dedupe_sort_tail` 只取最后 N 根，limit 后结果与全量查询再截取一致；先测 SQL 数量/耗时/响应字节，结果不得变差。（6）**新增策略需同步更新 `_REQUIRED_INPUTS`**：默认 fallback `frozenset({"daily"})`，但需在策略注册时同步更新映射，否则会得到空 DataFrame。

53. TV parity baseline + SMC source diagnostics（CHANGE-20260716-001）：（1）**Pine 隐藏 export 字段（CHANGE-20260716-001）**：**真源 `ref/smc_user_source.pine`（SHA256 0bd3d2ad，843 行）不可变**，导出功能在派生文件 `ref/smc_user_export.pine` 末尾新增 18 个 `plot(..., display=display.none)` 隐藏 export 字段——`_exp_open`/`_exp_high`/`_exp_low`/`_exp_close`（OHLC）、`_exp_int_bull_bos`/`_exp_int_bear_bos`/`_exp_int_bull_choch`/`_exp_int_bear_choch`（internal BOS/CHoCH）、`_exp_swing_bull_bos`/`_exp_swing_bear_bos`/`_exp_swing_bull_choch`/`_exp_swing_bear_choch`（swing BOS/CHoCH）、`_exp_int_bull_ob`/`_exp_int_bear_ob`（internal OB）、`_exp_eqh`/`_exp_eql`（EQH/EQL）、`_exp_swing_bias`/`_exp_int_bias`（bias）；`display=display.none` 不影响图表和算法，但数据可通过 TV "Export indicator data" 导出为 CSV。（2）**CSV fixture 路径**：`backend/tests/fixtures/smc_pine/smc_tv_<symbol>_<tf>.csv`；项目 Python 测试直接读取该 CSV，**禁止从 DB 重新取另一套 Bar**；产品继续默认前复权，TV parity fixture 使用与 TV 完全相同的复权方式、数据源和 completed-bar 边界。（3）**parity 测试** `backend/tests/test_smc_tv_parity.py`（`PINE_PARITY_PENDING`）：`test_tv_csv_bar_parity`（断言 time/OHLC/bar 数量逐项相等，浮点容差 1e-8，不相等写 `INPUT_BAR_MISMATCH`，**不得调整算法迎合截图**）、`test_tv_csv_event_parity`（比较事件有序序列，bar_index ±1 容差）、`test_tv_csv_swing_bias_parity`（比较最后一根 bar 的 swing_bias）；无 CSV fixture 时 skip，不得宣称"完全对齐"。（4）**SMC source 诊断字段（CHANGE-20260716-001）**：API 新增诊断字段（仅 `include_smc=True` 时返回）——`smc_source_bar_hash`（基于 SMC 实际完整输入 `smc_bars` 计算 hash；1d 用 `full_daily_bars`，其他用 `macd_bars`；**不得复用截断后的 `macd_bars` hash**）、`smc_source_first_time`、`smc_source_last_time`、`smc_source_bars`、`smc_adj`；`include_smc=False` 时所有 `smc_source_*` 字段为 None/0。

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
