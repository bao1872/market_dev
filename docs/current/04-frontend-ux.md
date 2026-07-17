# 04 前端、路由与 UX

## 1. 前端职责

前端使用 React、TypeScript、React Router。前端负责页面、交互、DTO 到 ViewModel、图表和页面状态。

前端不得重新实现：

```text
后端权限
套餐额度
DSA 算法
Node Cluster 算法
发布门禁
监控资格
```

## 2. 路由与守卫

| 路由 | 守卫 | 壳层 | 页面 |
|---|---|---|---|
| `/` | Public | — | 门户 |
| `/login` | Public | — | 登录/注册 |
| `/subscription-expired` | Authenticated | — | 续期 |
| `/membership-expired` | Redirect | — | 兼容跳转 |
| `/capture/stock/:symbol` | Capture Token | **无壳层** | 截图专用页面 |
| `/market` | Subscriber/Admin | UserAppShell | 行情工作区（工具栏 + `StrategyDataTable`（DSA 列表）+ 可收起 `AtomicFactsPanel`（小 K 线下状态观察），**无 K 线**，`MarketWorkspacePage`） |
| `/screener` | Redirect | — | 兼容重定向 → `/market` |
| `/stock/:symbol` | Subscriber/Admin | UserAppShell | **唯一** K 线详情入口（`StockDetailPage`） |
| `/messages` | Authenticated | UserAppShell | 历史消息 |
| `/settings` | Authenticated | UserAppShell | 账户和通知渠道 |
| `/admin/*` | Admin | AdminAppShell | 管理页面（含 `/admin/overview`、`/admin/users`、`/admin/jobs`、`/admin/beta-applications`、`/admin/after-close`、`/admin/stock-debug/:symbol`）；`/admin/strategies` 已废弃为 redirect-only → `/admin/after-close`（无页面加载） |
| `/overview` | Redirect | — | 兼容重定向 → `/market` |
| `/watchlist` | Redirect | — | 兼容重定向 → `/market?scope=watchlist` |

### 2.1 壳层拆分（阶段二确立）

- `UserAppShell`（`frontend/src/layouts/UserAppShell.tsx`）：普通用户布局壳，顶部品牌 + 一级导航（行情 `/market`、趋势选股 `/screener`）+ 右上角账户菜单；**无左侧栏**；**无角色预览按钮**（role preview 已移除）。
- `AdminAppShell`（`frontend/src/layouts/AdminAppShell.tsx`）：管理员独立布局壳，侧栏管理导航 + 顶栏账户菜单；仅承载 `/admin/*`；**响应式布局（CHANGE-20260714-001）**：桌面端（≥1024px）显示左侧管理导航侧栏 + 顶栏账户菜单；小屏（<1024px）隐藏侧栏，顶栏左侧显示"← 返回行情"按钮链接到 `/market`（与 `getAccountMenuItemsForVariant(_, 'admin')` "返回行情"入口一致，CHANGE-20260713-007），管理导航收起为顶部菜单或抽屉。
- `AccountMenu`（`frontend/src/components/AccountMenu.tsx`）：右上角下拉菜单，包含消息、设置、管理后台（仅 `is_admin`）、退出；支持点击外部关闭、Escape 关闭、基本 ARIA。**管理员入口契约（CHANGE-20260713-007）**：管理员从用户 `AccountMenu`（`variant='user'` + `is_admin=true`）进入 `/admin`（菜单项"管理后台"）；普通用户（`is_admin=false`）DOM **完全不渲染**管理后台入口（不是 CSS 隐藏）；`getAccountMenuItemsForVariant(isAdmin, 'admin')` 显示"返回行情"链接到 `/market`，不重复显示"管理后台"（避免管理员在 `AdminAppShell` 内重复入口）。
- `ProtectedLayout` 只负责认证和 access profile，不再固定渲染同一壳层；`UserAppShell`/`AdminAppShell` 由各自路由组在 `ProtectedLayout` 之下作为 layout element 挂载。**AdminRoute 权限真源（CHANGE-20260713-007）**：`AdminRoute` 以 `user.is_admin` 为唯一权限真源（不依赖任何其他角色/字段判断），`accessLoading` 状态防止 auth hydration 未完成时提前判定 false（刷新页面后 access store 重新拉取 `/me/access` 期间显示 loading，避免 access 未就绪时被误判为非 admin 重定向到 `/market`）；权限真源只来自后端 `users.is_admin` 字段，禁止前端臆造或缓存 admin 状态。
- `/capture/stock/:symbol` 位于两套壳层之外，只使用 `captureClient`。
- 导航/路由常量集中于 `frontend/src/navigation/appNavigation.ts`，避免路径散落。

### 2.2 兼容重定向

旧地址保留重定向，避免书签/旧链接 404：
- `/overview` → `/market`
- `/watchlist` → `/market?scope=watchlist`
- `/screener` → `/market`

### 2.3 尚未完成（下一阶段）

- 复盘模式本轮不开发。

### 2.4 统一行情工作区

- `/market` 渲染 `MarketWorkspacePage`（`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`），**无 K 线**，布局为工具栏 + DSA 列表表格 + 可收起事件状态面板：
  - 工具栏（`MarketToolbar`）：同一行布局 scope 切换（watchlist/market）分段按钮 → 搜索框 → 行业筛选 → 概念筛选（CHANGE-20260713-005/006，CHANGE-20260716-007 行为收紧 + PR #77 收口 + PR #77 收口第二轮 P0 修复）；搜索框占位"搜索股票代码/名称/拼音首字母"，Enter/失焦提交，清空立即提交；`StrategyDataTable` 在 `/market` 传 `searchable={false}` 隐藏内置搜索 UI；列筛选仍由 `StrategyDataTable` 内置 UI 承载（URL `filters` 参数）；**行业/概念筛选输入行为（CHANGE-20260716-007 + PR #77 收口 + 收口第二轮 P0 修复）**：删除原生 `<datalist>`，改用 `BoardFilterCombobox.tsx`（行业+概念共用，盘迹 SCSS 变量，不新增依赖）；行业模式（`mode="industry"`）placeholder「搜索行业关键词」，允许任意关键词输入（不再用 `industryNameSet` 拒绝/重置），本地过滤完整路径最多 12 条建议，展示「一级 / 二级 / 三级」并高亮命中，Enter 提交关键词/点击建议提交完整路径/清空立即提交；概念模式（`mode="concept"`）本地搜索目录只提交精确概念，不逐字符请求后端；ArrowUp/Down/Enter/Escape + 点击外部关闭 + 清除按钮 + aria-combobox/listbox/option + 150ms blur 延迟解决点击问题；**PR #77 收口第二轮 P0 修复**：(a) `openPanel()` 与 `handleInputChange` 改为 `activeIndex=-1`（不再默认选中首项，Enter 不自动选第一条）；(b) Enter 无激活建议时行业提交当前关键词（非首条完整路径）、概念仅当存在精确匹配时提交；(c) `normalizeInput` 增加 `.normalize('NFKC')` 与后端 `_normalize_keyword` 对齐；(d) `useId()` 生成唯一 listbox/option ID 防多实例冲突；(e) 新增 `suggestionRank()`（exact=0/prefix=1/contains=2 + `localeCompare('zh-Hans-CN')` 稳定排序）；(f) 清除按钮移除 `tabIndex={-1}` 恢复键盘可达；(g) 新增「无匹配行业」/「未找到该概念」无结果反馈（`hasInputNoMatch`）；(h) SCSS 行业面板宽 360-480px（原 220px）、概念面板最大 240px、`.comboboxPanel` 改 `min-width: 100%`（原 `left:0;right:0`）；(i) 建议项 `<li>` 增加 `title` 显示完整行业路径；行业宽 200~240px（输入框），概念宽 160~200px（输入框），深色 panel/1px 边框/8px 圆角/轻阴影/荧感绿 focus/青绿色 hover/最大高度 320px/z-index 高于表格；**后端 industry 改关键词 ilike 匹配**（`MarketBoard.name.ilike('%keyword%', escape='\\')`，匹配完整路径任意一级，NFKC + trim + 转义 `\`/`%`/`_`），concept 保持精确匹配（收口第二轮：concept 也应用 `_normalize_keyword()` NFKC + trim 后再 `==` 比较）；清空输入立即提交并重置分页到第 1 页；`boards.available=false` 时输入禁用但显示（placeholder "板块数据暂不可用"），不直接删除输入；`boards.stale=true` 时输入仍可用，工具栏展示"沿用上次板块数据"提示（数据为上一成功版本，筛选仍生效）；行业值 `-` 在前端可渲染为 `/`（API 值不变，仅显示层映射）；industry + concept 同时提供时为 AND 语义（交集）；market stocks/StrategyRunResults/行情/自选/Excel 复用同一 `board_filter_helper`。
  - 搜索单一真源（CHANGE-20260713-005）：`MarketWorkspacePage` 持有 `keyword` state（初始值从 URL `keyword` 读取）→ `StrategyDataTable` 通过 `externalKeyword`/`onKeywordChange` 受控 props 接收 → URL `keyword` 同步；后端 `strategy_result_repository.query_results` 的 `keyword` 必须 ILIKE 同时匹配 `Instrument.symbol`/`Instrument.name`/`Instrument.pinyin_initials`（3 处分支同步）。
  - `StrategyDataTable`（`frontend/src/components/StrategyDataTable.tsx`）：复用趋势选股的 DSA 列表能力，数据来自 `usePublishedRuns` + `useStrategyRunResults`（最新 published DSA run）；列定义复用 `getTrendSelectionColumns`（`frontend/src/features/trend-selection/columns.tsx`），包含 stock/change_pct/dsa_dir_bars/vwap_ret_avg/vwap_ret_total/offset_mean/offset_std/offset_percentile/dsa_vwap/dsa_vwap_dev_pct/offset_variance_rate/price/action；`scope=market` → `universe=all`，`scope=watchlist` → `universe=watchlist`；**默认不显示**形态状态、DSA状态、最近事件列（事件只在 `EventStatePanel` 按需展开时加载）；行点击（`onRowClick`）更新 URL `selected` 并驱动右栏 `AtomicFactsPanel` 加载该股票 context。
  - 行内导航与自选操作（CHANGE-20260713-005）：股票名称/代码为可点击 `<a>` 链接，点击进入 `/stock/:symbol?returnTo=<编码后的当前 /market URL>`；链接 `onClick` 必须 `e.stopPropagation()` + `e.preventDefault()` 防止冒泡到 `<tr onClick>` 和默认跳转；股票单元格只显示名称/代码/市场，不再显示行内涨跌幅（独立 `change_pct` 列保留）；`action` 列改名"自选"，渲染"加入自选/移除自选"按钮（替代旧"详情"按钮），`onClick` 必须 `e.stopPropagation()`；页面只请求一次 `useWatchlist`，按 `instrument_id` 建 Set 判断 watched 状态（禁止 N+1）；`useAddToWatchlist`/`useRemoveFromWatchlist` 成功后 invalidate `['watchlist']` 和 `['watchlist', 'monitor-status']`；`watchlist` scope 移除自选后该行应消失；按 `instrument_id` 维护 `pending` Set 防重复点击。
  - 批次信息权限（CHANGE-20260713-005）：数据日期/批次/状态属于调试信息，普通用户 DOM 中**完全不渲染**（不是 CSS 隐藏）；仅 `useAuthStore(s => s.user?.is_admin === true)` 为真时渲染，默认折叠为"批次信息"区块，点击展开后显示 `run_trade_date`/`run_published_at`/`run_status` 等字段。
  - 列设置与配置：`StrategyDataTable` 支持列显示/隐藏、调整顺序（`columnOrder`）、恢复默认、刷新保留（localStorage `table-columns:${tableId}` + `table-column-order:${tableId}`）；配置复用 `/me/table-view-presets`（`TablePresetMenu`），保存 `keyword/sort/filters/hiddenColumns/columnOrder/pageSize/industry/concept`（CHANGE-20260713-006 新增 industry/concept）；股票名称/代码和操作列不可全部隐藏；旧配置包含已删除字段时忽略未知项。
  - P0 列对齐契约：表头 th、表体 td、colgroup col 三者从同一 `visibleColumns` 派生（`reorderVisibleColumns` 纯函数，`frontend/src/components/columnOrdering.ts`）；每行 td 数 = 可见 th 数；单元格按 `col.key` 取值，禁止依赖数组下标；`columnAlignment.test.ts` 覆盖纯函数 + 源码契约。
  - **sticky 列固定宽度契约（CHANGE-20260715-003 → CHANGE-20260715-005）**：`.interactive-table` 定义 CSS 变量 `--stock-col-width: 150px` 和 `--select-col-width: 40px`（header/body 共用）；`.sticky-col` 设置 `width/min-width/max-width: var(--stock-col-width)` 固定宽度，防止长名称撑开列宽导致横向滚动重叠；`td.sticky-col` 内部 div/`.symbol`/`.symbol-sub`/`.stock-name-btn` 添加 `overflow: hidden; text-overflow: ellipsis; white-space: nowrap`；`th.sticky-col .th-shell` 添加 `max-width: 100%; overflow: hidden`；背景不透明（`#151a23`/`#11161e`）；z-index 高于普通列（sticky-col=3, thead th=4, 角落=5）；**CHANGE-20260715-005: `isStickyColumn(col)` 统一判断函数只允许 `col.key === 'stock'` 为 sticky 列**（header 和 body 共用同一判断，涨跌幅列保持普通列，删除死 CSS `.sticky-col-change-pct`）。
  - **表格结构与工具栏对齐契约（CHANGE-20260715-003 → CHANGE-20260715-005）**：**CHANGE-20260715-005: `table-wrap` → `table-shell > meta-bar + search-bar + table-scroll > table + pager`**；只有 `table-scroll` 设置 `overflow-x: auto`；meta-bar（配置/列设置/清除/导出）/search-bar/pager 移出横向滚动容器，右边界自然等于 table-scroll 右边界；删除 `position:sticky;left:0;width:100%` 补丁（不再需要）；`AdminAfterClosePipelinePage` 同步迁移到 `table-shell` + `table-scroll` 结构；viewport-sticky 模式下 `.table-shell.viewport-sticky .table-scroll { overflow: visible; }`（由外层容器滚动）。
  - `AtomicFactsPanel`（`frontend/src/features/research-context/AtomicFactsPanel.tsx`）：可收起面板，替换已删除的 `EventStatePanel`；P0-4: 首次默认收起，localStorage key `panji:market-right-panel-collapsed:v1` 持久化用户选择；收起时不挂载、不请求数据。面板使用 `useStockContext` 单一接口（`GET /api/v1/stocks/{symbol}/context`），复用于 `/market`（compact）与 `/stock/:symbol`（expanded，由 `AtomicFactsDrawer` 承载）；展示 **双合同分离**后的产品文案（冻结研究合同 `atomic_fact_contract_v1.json` 决定事实/顺序/公式，产品展示合同 `atomic_fact_presentation_v1.json` 决定 publicKey/publicLabel/visualKind/组色）。Compact（`/market` 右栏）：Header 两行，第一行「个股状态观察」+「日线 · {meta.researchFreezeVersion}」+ `N/14`（**从 API `meta.researchFreezeVersion` 读取，禁止硬编码 V4.13**），第二行观察日期；四张组卡（趋势运行 info / 动量配合 brand / 结构位置 purple / 成交参与 warning）；**事实行非卡片**（CSS Grid 透明行 `minmax(0,1fr) auto` + `grid-template-areas "label value" / ". secondary"`，secondary 位于第二行右列且 `text-align: right`，仅底部分隔线）；`FactRow` 按 `visualKind` 渲染去重；**S3/S6 位置轨道使用独立布局**（`.positionRow`）：第一行 label 左 / `0.63 · 中间` caption 右，第二行轨道横跨整组宽度（`grid-template-areas "label caption" / "track track"`），轨道下方四刻度 低位 / 0.33 / 0.67 / 高位（`railScale` `space-between` 均匀分布），预留刻度高度（`min-height`）禁止刻度与 caption 重叠；T5/V3 比值+「分类未启用」、S7/S8「尚未到达/已越过」；面板内滚动不压小 K 线。普通用户 DOM 不得出现 factId/路径/内部英文术语（DSA/SQZMOM/Segment/Active/Developing/bar/raw）；原始 factor/feature/JSON 仅在 `/admin/stock-debug` 和 `/admin/stock-debug/:symbol` 展示。
  - **右栏容器与小 K 线（CHANGE-20260713-010，CHANGE-20260714-001 扩展为五周期）**：`MarketRightPanel`（`frontend/src/features/market-workspace/MarketRightPanel.tsx`）组合 `MiniKlineCard`（顶部）+ `AtomicFactsPanel`（底部，compact 形态），保持 `AtomicFactsPanel` 单一职责不变；`MiniKlineCard`（`frontend/src/features/market-workspace/MiniKlineCard.tsx`）使用 `lightweight-charts` v4 `createChart` + `addCandlestickSeries` 渲染简化 K 线（无指标/Node/事件/工具栏），**五周期 segmented control 风格**切换 `15m`/`60m`/`1d`/`1w`/`1mo`（标签"15分/60分/日/周/月"），默认 `1d`；`attributionLogo=false` 关闭 lightweight-charts 水印；`useMiniKlineData`（`frontend/src/features/market-workspace/useMiniKlineData.ts`）定义 `BARS_COUNT` = `{15m: 48, 60m: 44, 1d: 40, 1w: 36, 1mo: 30}`（**CHANGE-20260716-001**：从旧值 `{15m:120,60m:120,1d:80,1w:60,1mo:48}` 收敛为 TV 对齐目标根数），`refetchInterval: false` 禁用轮询；面板收起时由父组件 `MarketWorkspacePage` 不挂载 `MarketRightPanel`，`bars`/`context` 请求均为 0；展开后只请求活动周期（不预取五周期），切回已加载周期使用 React Query 缓存；chart 实例仅创建一次（`useEffect []`），`ResizeObserver` 响应式 + 卸载清理 `disconnect()` + `chart.remove()` + ref 清空，避免重复 canvas；`symbol=null` 时 `MiniKlineCard` 内部显示提示，`EventStatePanel` 不渲染。
  - **MiniKline viewport 彻底重写（CHANGE-20260715-001 → CHANGE-20260715-002 → CHANGE-20260715-006 闭包根治 → CHANGE-20260716-001 真实方案）**：`MiniKlineCard` 使用依赖注入图表控制器 `miniKlineController`（`frontend/src/features/market-workspace/miniKlineController.ts`）+ 纯函数 `computeMiniKlineViewport`（`frontend/src/features/market-workspace/miniKlineViewport.ts`）；**CHANGE-20260716-001 真实方案（取代旧 barSpacing 计算未应用问题）**：单一方案按周期取目标根数 15m=48/60m=44/日=40/周=36/月=30，将 `bars.slice(-target)` 传给 series（不再用 `BARS_COUNT={15m:120,60m:120,1d:80,1w:60,1mo:48}` 旧值）；`setData` 后设置 logical range `{from:-2, to:visibleData.length-1+3}`，形成真实左 2/右 3 空位（不再用 `from=max(-2, n-visible-1)` 依赖 visibleBars 的假留白）；**删除死 barSpacing 计算**（旧 `barSpacing = clamp(contentWidth/visibleBars, 5.5, 8)` 只计算未通过 `timeScale.applyOptions` 应用，是死参数；如需应用必须明确通过 applyOptions，不得保留死参数）；禁止调用 `fitContent`/`resetTimeScale`/`scrollToRealTime` 覆盖 range 和第二套 `rightOffset`；`autoscaleInfoProvider` 只基于当前 visibleData 的 high/low 扩展价格范围（上方 12%，下方 15%）；`rightPriceScale` `autoScale=true` + `scaleMargins {top:0.08, bottom:0.08}` + `minimumWidth=56`；`setData` 后在 `requestAnimationFrame` 应用 range；symbol/timeframe/width 变化重新应用，切周期不复用旧 logical range；图表容器 CSS 明确 `height:190px`，card 无多余 min-height 或底部空白；**tabs 改为五等分全宽 grid**（15m/60m/日/周/月）；切 symbol/周期先清旧 data、取消旧 rAF、resize 后使用最新数据；真实 mock 测试断言 setData 根数、range、五周期切换、ResizeObserver cleanup、chart.remove 和 0 旧数据残留。**CHANGE-20260715-006 闭包根治**：新增 `barsLengthRef`/`timeframeRef`/`rafIdRef` 持有最新值（每次 render 同步，在 effects 之前）；`applyViewportRange` 改为 `useCallback([], )` 稳定函数从 refs 读取（不再直接闭包捕获 `bars.length`/`timeframe`）；新增 `scheduleApplyRange` 稳定函数取消 pending rAF 后调度新 rAF；`ResizeObserver` 回调调用 `scheduleApplyRange`（不直接闭包捕获 bars/timeframe，避免 mount 时回调使用首次 render 的 stale 值）；卸载清理取消 pending rAF；5 项闭包契约测试（16-20）。
  - **股票名称筛选 alias（CHANGE-20260713-010）**：`DataTableColumn.filterAlias?: 'keyword'` 字段允许列头筛选与顶部搜索共用唯一 keyword 真源；`stock` 列设置 `filterAlias: 'keyword'` + `filterable: true`（与 `MarketToolbar` 顶部搜索、URL `keyword`、preset 共用唯一真源）；`StrategyDataTable` `KeywordFilterPopover` 组件 `onApply`/`onClear` 双向同步（`setGlobalQuery` + `onKeywordChange`）；列头激活状态基于 `effectiveKeyword`；`isKeyword` flag 区分不进入 `filters` state；URL sync `replace: true` + `skipNextUrlSyncRef` 避免 URL 循环；`currentConfig.keyword` / `applyPresetConfig` 共用 `effectiveKeyword`；`stock`/`action` 不入 `metric_filters`。
  - **股票名称独立筛选 stock_name/stock_name_op（CHANGE-20260714-001）**：除 `keyword` 全文搜索外，新增 `stock_name`（Query 参数）+ `stock_name_op`（操作符，`contains`/`not_contains`/`eq`）独立股票名称筛选；`stock_name` 只匹配 `Instrument.name`（中文名称），不匹配 symbol/pinyin_initials（与 `keyword` 语义分离）；后端 `strategy_result_repository.query_results` 接收 `stock_name`/`stock_name_op` 参数，构造 `Instrument.name` ILIKE 或 `=` 条件（`contains` → ILIKE `%<value>%`，`not_contains` → NOT ILIKE `%<value>%`，`eq` → `=`）；`stock_name`/`stock_name_op` 进入 URL 状态由 `screenerUrlState` 管理，与 `keyword/sort/filters/page/page_size` 同级；`items`/`filtered_total` 两处同步应用该筛选条件；`stock_name` 不进入 `metric_filters`（与 `keyword` 同为独立 Query 参数）；详见 `docs/current/02-data-api-contracts.md` 第 17 节。
- `/stock/:symbol` 是唯一个股详情和 K线入口（PRD V1.1），渲染 `StockDetailPage`（`frontend/src/pages/StockDetailPage.tsx`）：
  - 使用共享 `useStockResearchData` + `StockResearchWorkspace` 渲染图表区，不再独立调用 useBars/useIndicators/useRealtimeQuote/useInstrumentEvents。
  - 详情页专属能力拆到 `useStockDetailActions`（自选/上下切换/memo + returnTo 上下文恢复左栏列表）和 `useStockDetailFeishu`（飞书截图/轮询/超时）。
  - 保留 header、价格条、返回、上下只、watchlist、memo、飞书、全屏、事件状态面板开关。
  - **报价条 `StockQuoteStrip`（CHANGE-20260713-010）**：从 `StockDetailPage` 内联报价条抽取为独立组件（`frontend/src/features/stock-research/StockQuoteStrip.tsx`），展示 8 项指标——现价/涨跌/开盘/最高/最低/成交额/总市值/流通市值；总市值与流通市值放在成交额右侧；`formatMarketCap(v)` 区分 `< 1亿` 万元 / `>= 1亿` 亿元 / `>= 1万亿` 万亿元，空值显示 `--`；tooltip 显示数据日期（`market_cap_as_of`）；`QuoteMetric` 子组件统一渲染单格（`label`/`value`/`valueClassName`/`title`）；`PriceSummary` 接口含 `totalMarketCap`/`floatMarketCap`/`marketCapAsOf` 字段；市值数据不可用时为 `null`，前端不伪造数据。
  - **状态观察面板折叠状态（P0-4）**：`eventPanelCollapsed` 首次默认收起（`true`），localStorage key `panji:event-panel:v1` 持久化用户选择；`/stock/:symbol` 详情页「显示状态观察」按钮打开右侧 **overlay `AtomicFactsDrawer`**（`frontend/src/features/research-context/AtomicFactsDrawer.tsx`，宽 `min(1080px, calc(100vw - 48px))`，固定 overlay **不压缩主 K 线**；Escape/遮罩/关闭按钮可关，`role="dialog"` `aria-modal`；四组 Core 在抽屉内响应式横排：宽屏 4 列 / 普通 2 列 / 小屏 1 列；下方全宽近期变化；**10 个 Aux 中仅 8 个可展开**（T3/T6/V1 永不出现 DOM），「更多观察」默认收起，展开渲染 8 项可用 Auxiliary；**近期变化按每个 Fact 的 presentation `valuePrecision` 量化**，显示中文 label、from→to、`deltaText` 和日期，**禁止显示 publicKey**）；**焦点陷阱双向生效（CHANGE-20260716-005）**：打开后 focus 进入 drawer，Tab 和 Shift+Tab 均监听 `!drawer.contains(document.activeElement)`，正向 Tab 越过最后一个可聚焦元素时回环到第一个，Shift+Tab 越过第一个时回环到最后一个；保留焦点恢复（关闭后回到触发按钮）、Escape 关闭和 body 滚动锁；抽屉关闭时 `AtomicFactsDrawer` 不挂载、`useStockContext` 请求为 0；Drawer 内渲染 `AtomicFactsPanel variant="expanded"`（四组事实 + 近期变化 + Auxiliary 默认收起）。
  - `StockResearchWorkspace` 通过 `toolbar`/`rightPanel`/`showRightPanel`/`chartColumnProps` 可选 props 支持详情页的事件状态面板开关和截图模式属性。
  - **禁止重定向到 `/market`**（PRD V1.1 硬性规定）。
- `/admin/stock-debug` 和 `/admin/stock-debug/:symbol` 渲染 `AdminStockDebugPage`（`frontend/src/pages/AdminStockDebugPage.tsx`），位于 `AdminRoute` + `AdminAppShell` 下，普通用户不可访问（403）：
  - 复用 `MarketInstrumentPane`（股票搜索）、`useStockResearchData`（bars/indicators/quote/events）、`StockResearchWorkspace`（K线研究区）、`useAdminStockDebug`（含原始 payload 的管理员调试接口）。
  - 管理员调试能力独立于 `/market`，`/market` 不承载任何原始因子或 JSON；`debug` 不在 `/market` URL 契约中，`/market?debug=1` 管理员访问时重定向到 `/admin/stock-debug/:symbol`。
- 共享类型（`DisplayTimeframe`/`ResearchSource`/`ALLOWED_TIMEFRAMES`/`BARS_COUNT_BY_TIMEFRAME`/`defaultStrategyForSource`/`normalizeDisplayTimeframe`/`normalizeResearchSource`）权威定义在 `frontend/src/features/stock-research/stockResearchTypes.ts`；`marketWorkspaceUrlState.ts` 从该文件导入并重新导出，依赖方向为 market-workspace → stock-research（禁止反向依赖）。
- `useStockResearchData` 只保留图表核心查询：instrument/bars/indicators/quote/events + priceSummary/quoteStatus/barsStatus/isRenderReady；自选操作、上下切换、memo、飞书由 `useStockDetailActions`/`useStockDetailFeishu` 负责（禁止加入核心 hook）。
- URL 状态：`/market` URL 契约为 `scope/selected/industry/concept`（由 `MarketWorkspacePage` via `marketWorkspaceUrlState` 管理）；`sort/dir/keyword/filters/page/page_size` 由 `StrategyDataTable` 内置 `screenerUrlState` 管理；industry/concept 切 scope/搜索/排序/分页时保留，改变板块筛选重置 page=1；`StrategyDataTable` 通过 `externalIndustry`/`onIndustryChange`/`externalConcept`/`onConceptChange` 受控 props 接收（与 `externalKeyword` 同模式）；preset 配置保存并恢复 industry/concept（`currentConfig`/`applyPresetConfig` 集成）；`/stock/:symbol` 的 `timeframe/source/strategy/event_id/returnTo` 进 URL，右栏折叠和 viewport 留本地。切换股票不整页刷新。非法 timeframe 回退 1d。`/stock/:symbol` 的 timeframe 也从 URL 解析（单一真源），工具栏切换写回 URL。`returnTo` 为来源页 URL，必须经 `normalizeInternalReturnTo`（`frontend/src/features/market-workspace/marketWorkspaceUrlState.ts`）校验——仅允许 `/screener`、`/market`、`/messages` 前缀（含 query/hash），拒绝外部 URL（http/https）、`javascript:`、双斜杠、非白名单前缀（如 `/admin`、`/login`、`/capture/stock`）、超长字符串（>500 字符）；左栏选股或切 scope 时清除 `returnTo`。
- `timeframe` 受控单一真源：URL → `useStockResearchData`（bars/indicators 请求参数）→ `StockResearchWorkspace`（图表渲染）三者始终使用同一 `DisplayTimeframe`（'15m'|'1h'|'1d'|'1w'|'1mo'）；工具栏切换通过 `onTimeframeChange` 回调写回 URL，禁止子组件 `useState` 维护独立 timeframe。
- URL 状态保留：切换周期/切换 scope/选择新股票时必须保留其他字段；选择新股票时清除旧 `event_id` 和 `returnTo`。
- 左栏选择上下文重置：从 `MarketInstrumentPane` 选择任意股票时必须写 `source='watchlist'`、`strategy='watchlist_monitor'`、`eventId=null`（退出 selection 上下文）；用户切换 scope（watchlist 或 market）时也必须退出 selection 上下文并清除旧 `event_id`；timeframe 在上述操作中继续保留。状态转换必须通过纯函数 `selectInstrumentFromMarketPane(state, newSymbol)` 和 `changeMarketScope(state, newScope)` 处理，禁止在多个 callback 中重复拼对象。
- **详情页来源上下文契约（CHANGE-20260713-009）**：`/market` 导航至 `/stock/:symbol` 时按 scope 传递 `source`/`strategy`（`scope=market` → `source=selection&strategy=dsa_selector`；`scope=watchlist` → `source=watchlist&strategy=watchlist_monitor`）+ 完整 `returnTo` URL；详情页左栏复用 DSA published results 链（`usePublishedRuns` + `useStrategyRunResults` + `adaptStrategyResultToTrendRow` + `getStockDisplay`，不再使用 `useMarketStocks`）；`decodeMarketListContext`/`buildStrategyResultQueryParams` 共享纯函数（`marketWorkspaceUrlState.ts`）由 `MarketWorkspacePage` 和 `useStockDetailActions` 共用避免 filter drift；`sourceListKind` 由 `marketContext.scope` 派生（`market`→"行情来源"，`watchlist`→"自选来源"）；`normalizeInternalReturnTo` 长度限制 500。
- **详情页左栏 scrollTop 恢复与 SourceStockItem changePct（CHANGE-20260714-001）**：详情页左栏来源列表（`SourceStockItem` 列表）使用 `sessionStorage` 记录用户滚动位置（key 格式 `panji:detail-source-scroll:${scope}`），用户在左栏点击某只股票进入详情后，返回或切换上下只时从 `sessionStorage` 恢复 `scrollTop`，避免列表跳回顶部；`sessionStorage` 在 tab 关闭时自动清除，不跨 session 持久化；`SourceStockItem` 组件渲染每只股票时包含 `changePct`（来自 `row.latestChangePct`，与 `/market` 列表口径一致），显示当日涨跌幅（红涨绿跌，无两根有效日线显示 `--`），tooltip 显示 `latest_change_trade_date`；左栏列表数据复用 DSA published results 链（`adaptStrategyResultToTrendRow` 已包含 `latestChangePct`/`latestChangeTradeDate` 字段映射）。
- **详情页左栏 loading 占位契约（CHANGE-20260715-004）**：`useStockDetailActions` 新增 `sourceListLoading: boolean` 字段（`hasMarketContext=true` 时为 `publishedRunsQuery.isLoading || !activeRunId || sourceResultsQuery.isLoading`；`hasMarketContext=false` 时为 `monitorStatusQuery.isLoading`）；`StockDetailPage` 左栏渲染分两个分支——`sourceListLoading=true` 时渲染 `<aside data-testid="detail-source-list-loading" class="tv-source-list tv-source-list-loading">`，内含 header（`detailActions.sourceListKind === 'market'` ? "行情来源" : "自选来源"）和 `<div class="tv-source-list-placeholder">加载中…</div>` 占位文案；`!sourceListLoading && sourceStocks.length > 0` 时渲染正常列表（`<aside data-testid="detail-source-list" class="tv-source-list">`）；CSS `.tv-source-list-placeholder { padding: 16px 10px; font-size: 12px; color: #778297; text-align: center; }`；用户从 `/market` 进入 `/stock/:symbol` 后左栏不再空白一段时间才出现列表，而是先显示 loading 占位再切换到实际列表。
- **详情页导航 originScope 单一真源（CHANGE-20260716-006）**：`originScope=market|watchlist` 是详情页左栏来源列表的**第一来源真源**，不被 `returnTo.scope` 覆盖；`returnTo` 仅承担返回导航和筛选恢复，不再决定来源类型。统一 URL builder 集中于 `frontend/src/features/stock-research/stockDetailNavigation.ts`，提供 `buildStockDetailUrl` / `resolveStockDetailOrigin` / `sourceForOriginScope` / `strategyForOriginScope`；**三入口统一**：`MarketWorkspacePage` 点击股票、`useStockDetailActions` 上一只/下一只、`StockDetailPage` 左栏来源列表点击，全部调用同一 `buildStockDetailUrl`，禁止手工字符串拼接；旧 `detailNavigation.ts:buildStockDetailUrl` 已删除（**禁止第二套 URL 拼接**）。`originScope` 优先级：显式 `originScope` 参数 > 旧 URL 兼容解析 `returnTo.scope` > 默认 watchlist。**冲突检测**：`originScope` 存在且 `returnTo.scope` 不同 → `contextMismatch=true`，禁止静默回退自选。`MarketWorkspacePage` 生成 returnTo 时使用当前 `searchParams` 副本并强制写入 `scope` 和 `selected`，禁止直接复制可能滞后的 `location.search`。
- **ConfirmedPositionRow 产品观察组件（CHANGE-20260716-006）**：`frontend/src/features/research-context/ConfirmedPositionRow.tsx` 渲染"价格在最近确认区间的位置"产品观察项（`publicKey=confirmed_swing_position`，`visualKind=confirmed_position`，`scope=product`）；位于结构组「价格与已确认区间关系」之后（保留现有 S3「价格在当前主要波段的位置」）；0.33/0.67 边界与 S3 一致；<0 显示"低于确认区间"，>1 显示"高于确认区间"，0–1 范围内显示 0–1 轨道（轨道样式与 PositionRow 一致）；**不计入 Core 14**（与 S3 active swing 位置独立）；数据来源为 Context API 顶层 `productObservations.structure` 字段（非 core/auxiliary）。
- **详情页右边界 grid 布局（CHANGE-20260716-006）**：`.tv-detail-layout` 从 `display: flex` 改为 `display: grid; grid-template-columns: 200px minmax(0,1fr)`（左栏 200px 固定 + 右栏 `minmax(0,1fr)` 防止子元素撑宽导致右边界错位）；`.tv-workspace`/`.strategy-chart-wrap`/`.tv-chart-column`/`.tv-canvas-wrap` 统一 `width:100%; min-width:0; max-width:100%; box-sizing:border-box`；`ResizeObserver` 改为 `requestAnimationFrame` 立即 draw + 120ms trailing draw（保留 window resize 补偿），修复快速切周期/全屏时中间宽度绘制导致右边界错位问题。
- 图表显示周期不改变 1d+15m 监控配置或 1m 事件触发口径。
- 错误状态：instrument/bars/indicators 加载失败时显示明确错误状态（含重试按钮），不伪装为空图。
- 周期文案：根据 timeframe 显示真实周期（1d=完整日线、15m=完整15分钟K线、1h=完整1小时K线、1w=完整周线、1mo=完整月线）；非实时非降级时统一显示"行情回退"（禁止所有非 1d 周期显示"日线回退"）；partial 文案必须包含当前周期（如"K线含未完成 bar（15m）"），禁止所有周期统一显示"日线"。
- `/capture/stock/:symbol` 完全独立，不使用 `useStockResearchData`/`StockResearchWorkspace`/`apiClient`，只使用 `captureClient`。
- `AccountMenu` 复用 `appNavigation.getAccountMenuItemsForVariant(isAdmin, variant)` 单一真源构建菜单项；消息项动态化（CHANGE-20260713-005）：`unread>0` 时菜单链接为 `/messages?filter=unread`，否则为 `/messages`；消息项右侧显示未读数 badge（`>99` 显示 `99+`），数据来自 `useUnreadCount`。
- 研究上下文纯函数：`buildStructureSummary`（`frontend/src/features/research-context/buildStructureSummary.ts`）从 `primary[timeframe].cost_position` 等真实 DTO 路径提取结构状态摘要（合并 degraded_reasons/warmup_notes、日线/15m 摘要、成本位置/节点）；`buildUserEventExplanation`（`frontend/src/features/research-context/buildUserEventExplanation.ts`）只消费白名单字段（event_time/event_type/payload.facts[].text_content/summary）并校验 `event.instrument_id` 与 `currentInstrumentId` 一致性（不一致时隐藏价格，显示"该事件属于其他股票"）。两个纯函数无 React 依赖，可被 `node --test` 直接运行。
- **板块筛选已恢复（CHANGE-20260713-006）**：`/market` DSA 列表支持行业/概念筛选，数据源仍为 published DSA run（`usePublishedRuns` + `useStrategyRunResults`），禁止同时请求 `/market/stocks` 拼接结果；`MarketToolbar` 渲染"搜索、行业、概念"同一行布局；`MarketStockTable` 已删除，由 `StrategyDataTable` + `getTrendSelectionColumns` 替代；`/market/boards` API 提供板块目录，`boards.available=false` 时行业/概念输入禁用但显示（placeholder "板块数据暂不可用"）；后端通过共享 `board_filter_helper.build_board_filter_conditions` 构造 EXISTS 子查询，`strategy_result_repository` 和 `market_stocks_service` 共用；industry+concept 同时提供时为 AND 语义。

## 3. 页面职责

### 盘后流水线详情（/admin/after-close）

- 顶部状态卡：整体状态（not_started/running/succeeded/failed/blocked/skipped）+ 交易日 + 市场时段 + watchlist_ready + 不可用原因 + 是否已有完整回补；
- 8 步骤垂直时间线：refreshing_daily → checking_coverage → creating_dsa → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → watchlist_ready，每步显示 status/started_at/finished_at/duration/counts/error_message；
- 数据新鲜度卡：行情数据（latest_daily_trade_date / daily_coverage / 15m / 60m / is_behind）+ 选股策略（latest_compute_trade_date / latest_published_trade_date / status / total / failed / published_at）；
- 编排状态详情：after_close_orchestrator job_run 摘要（status/orchestrator_status/started_at/finished_at/worker/heartbeat/lease_expires/last_completed_step/error）+ feature_snapshot_run 摘要（run_type/scope/snapshot_count/failed_count/published_at）；
- 最近 20 次运行列表：after_close_orchestrator + snapshot_run 混合，显示类型/交易日/状态/编排阶段/快照数/失败/开始/结束/ID；
- 事件日志抽屉：展示最近 100 条 job_run_events（来自 pipeline.events，含 step/level/message/payload/created_at）；
- 轮询策略：running 状态 10s 轮询，非 running 60s 轮询，页面不可见暂停（refetchIntervalInBackground=false）；
- 操作按钮：触发当日 after_close 编排（POST /admin/after-close/pipeline/run，幂等，已有任务返回 existing）；
- 系统概览（/admin/overview）中的 AfterClosePipelineCard 改造为摘要卡，提供进入 /admin/after-close 的链接。

### 趋势选股

> `/screener` 现为兼容重定向 → `/market`（无页面加载）；趋势选股相关表格能力由 `/market` 的 `MarketStockTable` 承载。

- 以全量 active 股票 universe 为展示主表（`strategy_run_items`），DSA 指标为 LEFT JOIN 附加字段；
- 默认无隐式筛选时显示全量股票（succeeded 有指标、skipped/failed 指标为空但仍显示）；
- succeeded 行应正确显示 35 个 DSA 指标（如 `bb.position`、`bb.width`、`node.position` 等）；后端通过 `(run_id, instrument_id)` 关联 `strategy_results` 加载指标（因 `strategy_run_items.result_id` 当前未回填，见 ALIGN-033）；
- skipped 行（reason_code=insufficient_history）显示股票代码和名称，指标列显示 "-"；
- failed 行显示股票代码和名称，指标列显示 "-"，附带 reason_code/error_message；
- 展示 source_total、filtered_total、成功、失败、跳过和覆盖率；
- 批次不完整显示阻断，不伪装正常；
- 行 key 使用 `instrumentId`（不依赖 `result_id`，skipped/failed 行也能选中加入自选）；
- "筛选结果" 标签替代原 "命中"（`filtered_total` 是当前筛选条件下的数量，不是命中数）；
- **批量加入自选**：`handleBatchAdd` 必须按 `r.instrumentId` 匹配 `selectedKeys`（与 `rowKey` 一致），禁止用 `r.resultId` 匹配（全量 universe 下 skipped/failed 行 `resultId=''`）；对 `instrumentId` 去重避免重复加入；选中后无可加入股票时 toast 提示而非静默；成功/失败 toast 真实反映数量；保留 `useAddToWatchlist` 现有缓存失效逻辑；
- **当日涨跌幅独立列（CHANGE-20260714-001 更新）**：`change_pct` 作为独立列展示（key=`change_pct`、title=当日涨跌幅、shortTitle=涨跌幅、dataType=percent、sortable=true、filterable=true、width≈86），render 改用 `row.latestChangePct`（从 `bars_daily` 表用 window function 计算最新完成交易日涨跌幅，**不回退旧 run 值**）；无两根有效日线（新股/停牌/退市/数据缺失）时显示 `--`；render 使用 `fmtChange` + A股涨红跌绿颜色（`changePctColorClass`，`latestChangePct > 0` 红、`< 0` 绿、`null` 中性）；`change_pct` 作为 `sort_by` 和 `metric_filters` 的特殊 key 走 `bars_daily` 子查询，**不在 manifest `filterable`/`sortable` 白名单中也允许**（由 `strategy_result_repository` 层 `CHANGE_PCT_METRIC_KEY` 常量特殊处理）；`latest_change_trade_date` 作为 tooltip 辅助显示对应交易日；`change_pct` 是已为百分比数值的字段，筛选输入 3% 传 3，不要乘除错；详见 `docs/current/02-data-api-contracts.md` 第 17 节；
- **表格视图配置 preset**：`StrategyDataTable` 元信息栏新增"配置"入口（`TablePresetMenu` 组件），支持保存当前配置为新 preset、应用已有 preset、覆盖已有 preset config、重命名、设为默认、删除；默认配置进入页面后自动应用（每个 `tableId:strategyKey` 组合只应用一次，`useRef` 防重复）；保存成功后通过 `presetsQuery.refetch()` 立即刷新列表，保持下拉打开并清空输入框，失败时在下拉内显示后端错误并 toast；切换策略/批次时清空选中股票（`selectedKeys`），不保留选中状态；config 只保存 `keyword/sort/filters/hiddenColumns/pageSize`，禁止保存 `selectedKeys/page/activeRunId/rows/resultData`（后端 Pydantic schema 强制白名单）；preset API 按 JWT `user_id` 隔离，普通用户只能操作自己的配置；权限与趋势选股一致（active subscription + trend_selection feature，admin 豁免）；每 `user+table_id+strategy_key` 最多 20 个 preset；`is_default` 同维度至多 1 个 true（设置新默认时旧默认自动取消）；
- **preset=none 门控（CHANGE-20260714-001）**：用户清除筛选（清空 keyword/filters/板块等）时，URL 写入 `preset=none`，表示"用户已主动清除筛选，不要自动应用默认 preset"；默认 preset 自动应用 effect 检查 `preset=none` 标记后跳过应用（每个 `tableId:strategyKey` 组合的 `useRef` 守卫配合 URL `preset=none` 双重防重入）；用户再次应用某 preset 或手动设置筛选条件时清除 `preset=none` 标记，恢复正常 preset 自动应用语义；`preset=none` 进入 URL 状态由 `screenerUrlState` 管理，与 `keyword/sort/filters/page/page_size` 同级。
- **sticky 表头与选择列**：`StrategyDataTable` 支持 `stickyHeaderMode` prop（`container` 默认 / `viewport`）；趋势选股页使用 `stickyHeaderMode="viewport"`，页面滚动时表头吸附在 topbar 下方（`.table-wrap.viewport-sticky { overflow: visible }` + `.table-wrap.viewport-sticky .data-table th { top: var(--topbar); z-index: 18 }`），不抢占局部滚动容器；普通表格保留 `container` 模式，`thead th` sticky top:0 z-index:4；sticky 首列/选择列 sticky left:0 z-index:3；角落单元格（表头+sticky 列）z-index:5（最高）；选择列存在时首列通过相邻兄弟选择器 `.table-select-column + th.sticky-col { left: 40px }` 偏移选择列宽度，避免 sticky 重叠；`box-sizing: border-box` 确保选择列宽度含 padding。
- **URL 状态持久化**：趋势选股页将当前策略 key、keyword、sort、filters、page、pageSize 同步到 URL query；`StrategyDataTable` 负责 keyword/sort/filters/page/pageSize 的 encode/decode，`ScreenerPage` 负责 strategy key 的同步与切换；filters 使用 compact JSON 只保存 `key/op/value/value2`，不保存 rows/selectedKeys/activeRunId/resultData；decode 时按当前有效列 key 集合丢弃陈旧 filter/sort key；切换策略时更新 `strategy=` 并重置 `page=1`；从趋势选股查看详情改进入 `/market?scope=market&symbol=...&source=selection&strategy=dsa_selector&returnTo=<当前 ScreenerPage URL>`（`buildMarketEntryFromScreener`），不再进入 `/stock/:symbol`；`/market` 和 `/stock/:symbol` 返回按钮优先使用 URL `returnTo` 参数，其次 `location.state.returnTo`，没有时按 `source` fallback 到 `/screener` 或 `/market?scope=watchlist`。

### 我的自选

- 展示股票、价格、涨跌幅、上下节点、POC、最近事件；
- 新增/删除/恢复后刷新服务器状态；
- 到期用户不加载列表，进入续期路径；
- 已存在、软删除、额度不足提示不同；
- 桌面端表格不显示每行状态栏，交易/非交易日状态统一在页眉用 `MonitorStatusBadge` 全局展示；
- 移动端卡片头不显示每行状态徽章；
- 数据列开启表头过滤（`filterable=true`）；
- 表格使用 `compact-table` 与趋势选股页字体/布局对齐；
- **指标数据源 = `stock_feature_snapshots.summary_payload`（不再走实时计算 fallback）**：
  - 后端 `GET /api/v1/watchlist/monitor-status` 响应每项包含 `calculation_status` 三态字段与 `metrics` 对象；
  - 前端按 `calculation_status` 三态展示：
    - `SUCCEEDED`：`metrics` 来自 `summary_payload`，正常渲染指标列；页眉可基于 `freshness_seconds` 显示数据新鲜度；
    - `WAITING_SNAPSHOT`：交易日已收盘但盘后 orchestrator 未生成 snapshot，`metrics` 为空 dict，前端指标列展示占位符（如 `—`）并提示「盘后快照生成中」；
    - `NO_SNAPSHOT`：非交易日或交易日内，`metrics` 为空 dict，前端指标列展示占位符（如 `—`），不提示错误；
  - 前端只渲染后端返回的 `metrics` 字段（`poc_price` / `nearest_node_above` / `nearest_node_below` / `daily_developing_swing_dir` / `m15_developing_swing_dir` / `current_price` / `change_pct` 等），**不重新计算**任何 DSA/BB/swing/temporal 因子；
  - 前端不调用 `MonitorSnapshotService` 实时计算路径，不依赖 `MonitorState.payload` fallback；
  - `MonitorEvaluation` 的 `evaluation_status` / `retry_count` / `error_code` / `source_bar_time` 仅用于展示评估状态徽章，不作为 metrics 数据源。

### 个股详情

- K 线、指标和截图共享行情快照；
- 展示 quote 的 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`，以及 bars 的 `data_source`/`as_of`/`is_partial`/`degraded`/`degraded_reason`；
- DSA 与 Node 图层可开关；
- **图层可见性单一真源（CHANGE-20260713-001，CHANGE-20260715-001 扩展为 8 键）**：所有图层开关状态由 `ChartLayerVisibility` 类型统一管理（8 键：`trend/node/boll/volume/macd/sqzmom/breakout/smc`），localStorage key 为 `panji:chart-layer-visibility:v2`；`IndicatorToolbar` 只 dispatch `onToggleLayer(key)`，`StockResearchWorkspace` 持有唯一 `layerVisibility` state 并通过 `layers` prop 传入 `StrategyChart`；禁止子组件 `useState` 维护独立图层状态、禁止 `indicatorVisibility`/`detail-chart-strategy-groups`/`setLayers` 等旧散落状态源；
- **SQZMOM_LB 图层开关**：位于技术指标分组，默认关闭；开启后在 K 线下方新增独立副图，使用后端返回的 `val` 渲染 histogram、`bcolor` 渲染柱色、`scolor` 渲染 0 轴 squeeze marker；前端只消费后端 DTO，不重新计算 `val`/`sqzOn`/`sqzOff`/`noSqz`；API 未返回 `sqzmom_lb` 时页面不崩溃；
- **SMC 图层开关（CHANGE-20260715-001，CHANGE-20260715-002 Pine parity）**：`CHART_LAYER_MANIFEST` 新增 `smc` 图层（name="智能资金"，kind="main"，默认 `false`）；仅 `/stock/:symbol` 个股详情请求时携带 `include_smc=true`，`/market` 右栏小 K 线不携带（SMC 只进入个股详情指标链，不进入行情工作区小 K 线）；前端只消费后端 `data.smc` DTO 渲染市场结构关键点位，不重新计算；**FVG 完全排除**（不计算、不返回、不缓存、不渲染）；API 未返回 `smc` 时页面不崩溃；
- **SMC renderer 对齐 Pine（CHANGE-20260715-002，CHANGE-20260716-001 修正）**：`SmcEvent` 接口 `kind?` → `internal?: boolean`（true=internal，false/缺失=swing），`SmcOrderBlock` 字段统一为 `anchor_index`（**CHANGE-20260716-001**：前端不得读取 `bar_index`，必须使用 `anchor_index` 与后端 DTO 一致；`bar_index` 旧字段已废弃）；BOS/CHoCH 线型按 scope 区分（internal=虚线 `[4,3]` + tiny 8px，swing=实线 + small 11px），不再按事件类型区分；**标签不加 `·I` 后缀**（CHANGE-20260716-001，与 TV 文字一致；internal/swing 区别仅靠线型）；标签位置为中点 `(x1+x2)/2` + `'center'` 对齐（不再左端 `x1+2`）；trailing 文案"强高/弱高/强低/弱低"，`swing_bias` 直接从后端 DTO `swing_bias` 字段读取（**CHANGE-20260716-001**：值域 {1,-1,0}，前端禁止从可见事件猜测；强高 if `swing_bias===-1` else 弱高；强低 if `swing_bias===1` else 弱低；旧文案 SH/SL 禁用）；OB 半透明 box（active alpha 0.12，mitigated alpha 0.05）；颜色多头红 `#FF4D4F`、空头绿 `#22C55E`（A 股红涨绿跌，品牌绿 `#00F6C2` 只用于 SMC 开关本身）；Historical 模式绘制全部相交事件（不因标签碰撞删除，只允许调整标签偏移）；**viewport 区间求交（CHANGE-20260716-001）**：BOS/CHoCH/EQH/EQL/OB 只要区间与 viewport 相交就绘制——anchor 在左侧时 `x1=plotLeft` 并标记 `clipped_left`，confirmed/mitigation 在右侧时 clamp 到 `plotRight`，仅完全不相交时跳过；**不得要求 anchor 与 confirmed 都在 displayTimes 中**；**OB 选择（CHANGE-20260716-001）**：只显示数组头部最近 5 个 `internal && !mitigated` OB，活动 OB 从 anchor_index 延伸到 mitigation 或右端；EQH/EQL 视觉线端点使用 `second_pivot_index`（CHANGE-20260716-001：anchor=前一 pivot，second_pivot=新 pivot 所在 `i-size`，confirmed=当前检测 Bar `i`；因果/回放使用 confirmed 字段）；**纵轴候选（CHANGE-20260716-001）**：加入可见 event.level、OB high/low、EQH/EQL level、trailing top/bottom，避免事件存在但被 Canvas 裁掉；拖拽/缩放/复位/周期切换后所有线/标签/OB 与 K 线共用相同 viewport 映射；**纯函数拆分（CHANGE-20260716-001）**：映射、区间求交、OB 选择、价格候选拆分为纯函数 `frontend/src/components/smcRendering.ts`，配合 Canvas mock 行为测试（禁止只用源码正则）；`smcToDisplay` 通过时间匹配自动过滤展示区外事件（后端 SMC 输出不截断，time 数组保持完整长度对齐 anchor/confirmed 索引）；
- **成交量分布**：Phase 5 前保持禁用，工具栏只显示真实能力；`consensus_zone` 保持禁用并显示"成交量分布尚未开放"，不得实现假筹码共识；
- **图层用户文案契约（CHANGE-20260713-005）**：仅改用户可见文案，不改内部 id/DTO/算法；`CHART_LAYER_MANIFEST`（`frontend/src/features/stock-research/stockResearchTypes.ts`）中 `sqzmom` 显示为"挤压动量"（description 含"波动收窄后的方向与强弱"），`node` 显示为"筹码共识价"（description 注明"基于历史成交量分布的估算代理，非股东真实持仓成本"）；`StrategyChart` 节点价格标签 `POC 峰`→"核心共识价"，`峰`→"共识价"；POC 中心线水平线标签显示"核心共识价"（非裸 `POC`）；tooltip 中 `is_poc`→" · 核心共识价"，`is_peak`→" · 共识价"；数据缺失提示为"筹码共识价暂不可用"；内部字段 `n.poc`/`profile.pocPrice`/`row.is_poc`/`is_peak`/`'poc'` layer key 必须保留（不改 DTO/算法）；不得恢复已删除的 `ConsensusZone`，也不得修改 profile/node/poc 字段名；"筹码共识价"是基于历史成交量分布的估算代理，不是股东真实持仓成本。
- **K 线初始 viewport 定位（CHANGE-20260713-001 P0-5）**：个股详情初始进入时，viewport 必须基于真实 `calc.length` 创建，`viewport.toIndex === calc.length`，默认显示最后 N 根 K 线；禁止用 `createDefaultViewport(0)` 构造假 viewport；切换股票时（`symbol` 变化）必须重置到该股票最新 K 线（viewport 复合 key `${symbol}:${timeframe}`）；切换周期时首次进入定位该周期末尾，已在当前股票当前周期内主动平移/缩放时可保留用户视区；新行情追加时，用户位于最右端则自动跟随最新 bar 并保持原可见根数，用户已平移到历史区域则不强制拉回；"复位"/"1月/3月/6月/1年/全部"等范围按钮全部以最新 bar 为右边界；
- **K 线 Pointer Events 拖拽契约（CHANGE-20260713-005）**：`StrategyChart` 使用 Pointer Events（`pointerdown`/`pointermove`/`pointerup`/`pointercancel`）替代旧 mouse 事件；`pointerdown` 调用 `setPointerCapture`，`pointerup`/`pointercancel` 调用 `releasePointerCapture`；`dragRef` 保存 `{startClientX, startViewport, pointerId}`，`pointermove` 从 `startViewport` 计算总位移（禁止在 stale viewport 上累计）；向右拖查看更早数据，向左拖回到最新；`dragMovedRef` 4px 阈值抑制 click（避免拖动误触节点/事件点击）；cursor 为 `grab`/`grabbing`；鼠标移出 canvas 后仍可继续拖动（依赖 setPointerCapture）；保留滚轮锚点缩放、双击复位和移动端双指缩放；`chartDrag.test.ts` 覆盖源码契约。
- **K 线右侧留白与交互契约（CHANGE-20260713-008）**：`StrategyChart` 引入 `RIGHT_PADDING_RATIO = 0.20`（20% 留白，落在 18%-22% 区间）；`step = effectivePlotW / display.length`，`effectivePlotW = plotW * (1 - RIGHT_PADDING_RATIO)`；bars 只占据绘图区前 80%，最新 K 线位于约 80% 位置；所有交互坐标映射（十字线/滚轮锚点/Pointer 拖拽/双击复位/节点/事件命中）统一使用 `step`，自动同步；网格线和十字线水平线仍延伸到 `g.plotRight`（保持全宽）；时间轴标签使用 `effectivePlotW`；不修改 Node/Profile/POC 算法、indicator_contract、盘中监控或 Capture 口径；`chartRightPadding.test.ts` 覆盖留白契约。
- 截图区设置 render-ready 标志；
- 按 timeframe 请求对应根数（1d=250、15m=4000、1h=1200、1w=260、1mo=120），与 Node Cluster / indicator_contract 对齐；`1m` 不在工具栏暴露；
- 个股详情 K 线实时状态以 `/bars` 返回的 `data_source/is_partial/last_live_bar_time/as_of` 为准；`mergeRealtimeQuoteIntoBars()` 只做兜底视觉增强，仅当 `quote.is_realtime === true && quote.source === "pytdx" && quote.freshness_seconds <= 60` 时才合并到最后一根 K 线，不参与指标计算，不替代后端 partial bar；daily_fallback / 延迟 / 降级行情只用于顶部报价 fallback/状态提示，不混入 `displayBars`；1d 保留日期语义并跨日追加实时 bar，intraday（15m/1h 等）使用 `quote.update_time`；`baseBars` 仍用于指标计算，避免污染算法输入；
- 顶部报价条优先使用实时报价，fallback 到最后一根 bar；
- **行情状态徽章**：根据 quote 来源/实时性/新鲜度/降级状态显示“实时行情 / 日线回退 / 数据延迟 / 行情降级”，并显示 `update_time`；不再固定显示“实时行情”；
- **K 线状态条**：显示 bars 的 `data_source`、`as_of`、`is_partial`、`degraded`、`degraded_reason`；交易时段 1d 返回 `is_partial=true` 时，状态条明确提示“盘中 partial bar（未收盘）”；
- **1d K 线实时性**：`include_realtime=true` 且交易时段时，1d bars 最后一根为当日 partial daily bar（由已完成 1m 聚合），收盘后自动恢复为完整日线；
- **轮询与性能**：`useRealtimeQuote` 交易时段 10s 轮询；`useBars`/`useIndicators` 交易时段 30s 轮询；均设置 `refetchIntervalInBackground: false`，页面 hidden 时停止后台轮询。
- **结构状态因子面板（V1.8）**：右侧 340px 新增 `StockStructuralStatePanel` 组件，双列布局（图表 + 因子面板）；面板含 5 张卡片（DSA 段质量/Swing 结构位置/成本节点/动量波动/成交参与），双周期 tabs（1d/15m）切换；V1.8 约 50 字段（含 dsa_segment 段收益/斜率/效率/段级成交量、swing_range/price_position/retracement、price_vs_poc_atr/value_area_position、distance_to_bb_*_atr/sqz_on/sqz_off/sqzmom_abs_percentile、current_vs_prev_volume_ratio、客观 relation 字段 primary_dir/secondary_dir/trend_alignment/primary_slope_atr 等）；前端只渲染后端 DTO，禁止重新计算因子；API 失败显示"暂无数据"，null 字段显示"-"，`degraded_reasons` 显示警告条；bool 字段（sqz_on/sqz_off）以"是/否"展示；数据源 `useStructuralFactors` hook → `GET /api/v1/instruments/{id}/structural-factors`，交易时段 60s 轮询。
  - **V1 默认隐藏**：面板默认不渲染，用户点击图表上方 toolbar 右侧「显示结构状态」开关后显示；`localStorage.showStructuralState` 持久化用户选择（默认 `null`/非 `"true"` 时隐藏）；按钮文案动态切换（隐藏时「显示结构状态」，显示时「隐藏结构状态」）；
  - **强制隐藏**：URL 参数 `?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 任意一个命中即强制隐藏按钮和面板且禁用开关按钮（`toggleStructuralState` 回调 early return），忽略 `localStorage`；
  - **截图模式**：盘中监控截图发送飞书默认必须隐藏结构状态面板（`capture=feishu` 自动命中强制隐藏规则），截图默认只包含 K 线和基础信息；
  - **时序特征 V1 卡片**：`StockStructuralStatePanel` 末尾折叠卡片渲染 `temporal-features` API DTO（daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段 + meta），前端只渲染 DTO 不重算，null 显示「-」，`warmup_notes`/`degraded_reasons` 有内容时显示提示；卡片受同一个结构状态开关控制（默认随面板隐藏，用户打开面板后显示）；
  - 窄屏（≤1250px）保持现有单列行为。
- **Swing 摘要卡 V1.10 developing swing 字段**（修复 active swing 仍不代表当前状态的问题）：
  - 摘要卡只显示 Developing swing 字段：`developing_swing_dir`、`developing_swing_high`、`developing_swing_low`、`bars_since_developing_swing_high`、`bars_since_developing_swing_low`、`price_position_in_developing_swing_0_1`、`distance_to_developing_swing_high_atr`、`distance_to_developing_swing_low_atr`；
  - active major leg 字段（`active_swing_high`/`active_swing_low`/`bars_since_active_swing_high`/`bars_since_active_swing_low`/`price_position_in_active_swing_0_1`/`distance_to_active_swing_high_atr`/`distance_to_active_swing_low_atr`）只放在 Swing 结构位置明细 JSON 中，不放在摘要卡；
  - confirmed pivot 字段（`confirmed_swing_high`/`confirmed_swing_low`/`bars_since_confirmed_swing_high`/`bars_since_confirmed_swing_low`/`price_position_in_confirmed_swing_raw`/`confirmed_swing_breakout_state`）只放在 Swing 结构位置明细 JSON 中，不放在摘要卡；
  - 禁止使用模糊标签「最近 swing high/low」「Swing 位置[0,1]」；摘要卡位置标签必须明确「Developing 位置[0,1]」，明细卡位置标签必须明确「Confirmed raw 位置」或「Active 位置[0,1]」；
  - 时序特征卡片中位置字段标签必须含 `developing` 或 `confirmed` 前缀，禁止使用无前缀的「Swing 位置」标签，禁止使用 `Active high`/`Active low` 作为主字段；
  - 摘要卡 `developing_swing_dir` 显示方向：`1` → "上涨段"，`-1` → "下跌段"，`None` → "fallback"。
- **capture 布局 V1.9 单列修复**（修复 capture 模式右侧空白问题）：
  - `isCaptureMode` 判定：URL 参数 `capture=feishu` 或 `capture=1` 或 `hideStructuralState=1` 任一命中即 `isCaptureMode=true`；
  - capture 模式下不渲染：结构状态开关按钮、右侧结构状态列（`StockStructuralStatePanel`）、Temporal Features 折叠卡片；
  - capture 模式 CSS：`.tv-side-column { display: none; }` 隐藏右侧列，`.tv-chart-column { width: 100%; }` 让图表列占满宽度；
  - capture 模式 `data-testid="tv-chart-column"` 必须挂在 `.tv-chart-column` 元素上（不再挂在 `.tv-content`），确保截图 testid 与单列布局对齐；
  - 非 capture 模式保持原双列布局（图表列 + 结构状态列）。
- **DSA overlay source mismatch 保护**（修复 15m/1h 误报"DSA 数据源不一致"）：
  - 图表在渲染 DSA overlay 前比较 `displayTimes` 与 `indicators.source_bar_times` 的 canonical key 交集；
  - canonical key 由 `frontend/src/utils/chartTime.ts::normalizeChartTime(time, timeframe)` 计算：15m/1h 用 `"YYYY-MM-DD HH:MM"`（提取前 16 字符），1d 用 `"YYYY-MM-DD"`；忽略 `+08:00` 时区后缀和秒数，使 K线（aware）与 `source_bar_times`（naive）产生相同 key；
  - 交集比例 `matched / klineKeys.size < 0.5` → 触发 "DSA 数据源不一致，已暂停渲染" banner，DSA overlay 不渲染，但 structural/temporal 因子卡片仍可显示；
  - 后端 `compute_source_bar_times` / `compute_source_bar_hash` 必须按当前 `timeframe` 使用对应 macd_bars，格式随 timeframe（1d=`YYYY-MM-DD`，15m/1h=`YYYY-MM-DDTHH:MM:SS`）；禁止 15m/1h source_bar_times 仍返回日线日期格式；
  - 15m/1h `bars.trade_time` 必须返回 aware ISO（`+08:00` 后缀），避免前端 `new Date("2026-07-06T15:00:00")` 在非亚洲时区浏览器中当作本地时间导致时区误判（如显示 `2026-07-07 03:00`）。
  - 15m/1h 时间轴刻度 `timeTicks` 使用 `Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai' })` 格式化，A 股交易时间正确显示，不应出现 `03:00` 这类非交易时段错误时间。
- **DSA overlay 周期策略（全周期支持，PR #32 + PR #33 前端硬编码清理）**：
  - DSA（Pine 标签 + VWAP）支持全周期渲染（1d/15m/1h/1w/1mo）；1d 是主结构锚，非 1d 是验证图层；
  - DSA overlay 按钮在所有周期可点击（不 disabled），`title` 由 `DSA_TITLE_HINT(timeframe)` 提供：1d 显示"DSA VWAP 日线结构锚。"，非 1d 显示"DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。"；
  - DSA toggle 全周期可切换（PR #33 修复 PR #32 遗留 `if (groupId === 'dsa' && timeframe !== '1d') return` 硬编码 disable），由 `shouldToggleDsa(groupId, isCaptureMode, captureLayers)` 集中决策；capture 模式仍锁定 DSA 不可关闭；
  - DSA 渲染决策由 `shouldRenderDsaLayer(layerId, layers, dsaSourceMismatch, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return` 硬编码 skip）：开关 / source mismatch / 周期支持三要素全周期判断，不再按 timeframe 跳过；
  - DSA 纵轴范围候选由 `shouldIncludeDsaInPriceRange(layerId, layers, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')` 硬编码）：DSA 全周期参与 y-axis range，避免非 1d DSA 渲染后被轴范围挤掉；
  - DSA source mismatch 校验由 `shouldCheckDsaMismatch(timeframe)` 控制：全周期返回 `true`（DSA 全周期渲染，全部需校验 source 对齐）；
  - 仍保留 source mismatch 保护：匹配率 < 50% 时暂停渲染并提示，不允许无校验强画；
  - 右侧 `StockStructuralStatePanel` 仍可显示 daily DSA 背景和 m15 response（结构状态因子不受图层渲染影响）；
- **BB/MACD/SQZMOM overlay 跟随当前周期（PR #31/#32 + PR #33 前端硬编码清理）**：
  - 后端 `indicator_service._adapt_watchlist_bb` 在 15m/1h/1w/1mo 必须用 `macd_bars`（当前 timeframe bars）调用 `compute_bollinger(macd_bars, length=20, mult=2.0)` 重新计算 BB，禁止用日线阶梯线伪装成当前周期 BB；
  - 1w/1mo 不再移除 BB 字段（PR #32 修复：之前直接 `pop` BB 字段导致前端无 BB overlay）；
  - BB 渲染决策由 `shouldRenderBbLayer(layerId, layers, timeframe)` 集中控制（PR #33 修复 PR #32 遗留 `if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return` 硬编码 skip）：1w/1mo 不再被前端跳过，开关 / 周期支持两要素全周期判断；
  - BB overlay 时间轴必须用 `buildDisplayIndexMap` 按 canonical time 对齐，禁止尾部截取（tail slice）；
  - MACD / SQZMOM 同理：必须用 `macd_bars`（当前 timeframe）计算，不允许串日线；
- **?debugIndicatorAlignment=1 诊断工具（PR #31 + PR #34 DSA segment matched）**：
  - `StrategyChart` 支持通过 URL 参数 `?debugIndicatorAlignment=1` 输出 overlay 对齐诊断到 console.table；
  - 输出 `bars`（timeframe/count/first/last/canonical_first/canonical_last）、`dsa_mismatch`（check_enabled/mismatched/source_bar_hash/source_bar_times_count）、`indicators.layers`（layer_id/renderer/fields/time_count）；
  - `renderDsaPolyline` 额外调用 `computeDsaSegmentMatchStats(segments, displayTimes, timeframe)`（`frontend/src/utils/dsaSegmentMatch.ts`，PR #34）输出 `console.warn('[DSA segment match]', { timeframe, total, matched, ratio, degradedReason, firstSegTime, lastSegTime, firstDisplayTime, lastDisplayTime })`；
  - `degradedReason` 取值：`null`（正常匹配 ratio > 0.5）/ `no_segments`（segments 为空）/ `no_points`（segments 非空但 points 总数 0）/ `no_display_times`（displayTimes 空）/ `segment_time_no_match`（ratio ≤ 0.5，含 15m 旧 YYYY-MM-DD segment times 退化为日期场景）；
  - 默认不打印，不刷日志，仅用于诊断 15m/1h DSA 开关打开但 canvas 看不到线的问题。
- **DSA overlay 依赖 visual_segments 时间与 K 线 canonical 对齐（PR #34）**：
  - `dsa_polyline` renderer 不直接画 `dsa_vwap` 数组，而是画 `visual_segments.points`；
  - 每段 `points[].time` 经 `normalizeChartTime(pt.time, timeframe)` 产生 canonical key，再与 K 线 `displayTimes` 的 canonical key 集合匹配；
  - 后端 `format_dsa_time(x)` 必须按 timeframe 序列化（15m/1h 含 `THH:MM:SS`，1d/1w/1mo 为 `YYYY-MM-DD`），否则 15m/1h 下 `normalizeChartTime` 返回 `null`，renderer matched=0，开关打开也画不出线；
  - `computeDsaSegmentMatchStats` 提供独立的 matched ratio 计算（pure function），用于回归测试与 debug 诊断，不替代 source mismatch 校验（source mismatch 校验 top-level `source_bar_times`，segment matched 校验 `visual_segments.points.time`，两者互补）。

### K线主标题与截图页实时状态

- **K线主标题显示股票名称**：`StrategyChart` 主标题优先显示股票名称，格式 `名称（代码）`（如 `宁德时代（300750）`），名称缺失时回退为代码；`displayName` 由页面传入（`StockDetailPage` / `CaptureStockPage` 传 `inst.name || symbol`）。
- **截图页（CaptureStockPage）实时链路**：
  - 按 URL `timeframe` 初始化（无则 1d）；截图模式不锁定日线，支持 15m 等周期；
  - 请求 snapshot 携带 `force_refresh=1` 与 `source_bar_time=...`，确保指标/行情为当前实时数据，不复用旧指标；
  - 状态栏展示 K线 `data_source` / `is_partial` / `last_live_bar_time` 与 quote status，供人工核对图片为实时数据；
  - 后端 partial daily bar 为真（`/bars?timeframe=1d&include_realtime=true` 返回 `is_partial=true`），前端 `mergeRealtimeQuoteIntoBars()` 仅作兜底视觉增强，不替代后端 partial bar。

### 消息与飞书

- 消息数量 SSOT（CHANGE-20260713-005）：`MessagesPage` 使用 `useUnreadCount`（`GET /messages/unread-count`，queryKey `['messages', 'unread-count']`）作为未读权威数量；"全部"显示后端列表 `messagesQuery.data?.total`（不用 `items.length`）；页头显示"共 X 条 · 未读 Y 条"；分段按钮仅 `all`/`unread` 显示计数，`selection`/`price`/`system`/`process` 不显示误导数字；标记单条/全部已读后 `useMarkMessageRead`/`useReadAllMessages` 的 `onSuccess` invalidate `['messages']`，自动刷新列表 + unread-count + 菜单角标。
- 消息跳转目标（CHANGE-20260713-005）：单只股票消息点击进入 `/stock/:symbol?event_id=...&returnTo=/messages`（不再 `/market?symbol=`）；`selection_composite` 类型消息进入 `/market`（不再 `/screener`）；多只股票抽屉"查看"按钮同样进入 `/stock/:symbol?event_id=...&returnTo=/messages`；无股票消息保持在消息页；`returnTo` 必须经 `normalizeInternalReturnTo` 校验。
- 消息显示股票、事件时间、详情入口；
- 文字和图片显示独立状态；
- partial_failed 展示失败步骤和仅重试图片；
- Worker 不可用时不显示整体成功。

#### 飞书渠道配置（SettingsPage）

- 普通用户与管理员均可在 `/settings` 配置自己的 `feishu_platform_app` 渠道；
- 表单字段：配置名称、App ID、App Secret、接收者 ID、接收者类型（`user_id`/`open_id`/`chat_id`/`union_id`），平台将 `receive_id_type` 原样透传给飞书接口；
- 保存按钮文案为「保存配置」，保存后渠道状态为 `pending`（未验证），不会自动变为 `active`；
- 渠道卡片对所有人显示「发送测试消息」（`active` 状态）或「测试并启用」（`pending` 状态），调用 `POST /notification-channels/{id}/test`；
- 测试成功 toast 显示「测试成功，飞书渠道已启用」，并刷新渠道列表使状态变为 `active`；测试失败展示后端返回的 `delivery.error_code` / `error_message`；
- 「管理员实测最近事件」按钮仅管理员可见，调用 `POST /notification-channels/{channel_id}/test-latest-event`，普通用户点击会收到 403 并提示使用普通测试接口。

### 管理页面

- 所有按钮调用真实 API；
- 启用、禁用、授予、续期、撤销、改套餐、重试都有 loading/error/refresh；
- 禁止用本地 state 或 Toast 模拟成功；
- AdminJobsPage 提供 "Worker 心跳" Tab，展示 worker_name/instance_id/status/health_state/heartbeat_at/age/build_sha/current_job_id，10 秒轮询，health_state 由后端计算（fresh/stale/stopped）。

## 4. UI 状态

所有页面统一支持：loading、refreshing、empty、error、partial、permission、success。

行情、策略结果、任务和消息页面必须显示真实数据时间。图表不连虚假线，partial Bar 有视觉区别。

## 5. 视觉原则

深色、专业、研究型；不夸张承诺收益；上涨红、下跌绿，同时用文字或形状辅助，避免只依赖颜色。图表提供文本摘要，可访问性不能丢。

## 6. 盘迹品牌视觉 V1.0（CHANGE-20260713-006）

- **视觉真源**：`ref/盘迹品牌视觉资产包_v1.0/`（ref 路径不作为运行时依赖，仅供设计参考）。
- **视觉 token 真源**：`frontend/src/styles/variables.scss` 为唯一视觉 token 真源，禁止在组件中硬编码颜色（必须使用 `v.$color-*` 或 `var(--*)`）。
- **品牌主色**：莹感绿 `#00F6C2`（`$color-brand`），只承担品牌焦点和关键交互（主按钮、选中 tab、focus 轮廓、Logo 末端节点），不得用于表达涨跌；大面积背景使用深石墨黑 `#0A0F14`（`$color-bg`），避免荧光绿铺满页面。
- **A 股涨跌色不变**：红涨 `#FF4D4F`（`$color-up`）/ 绿跌 `#22C55E`（`$color-down`），品牌主色不干扰涨跌语义。
- **V1.0 token 体系（CHANGE-20260713-007）**：`variables.scss` 为唯一 token 真源，完整色板：品牌 `#00F6C2`/`#39F5CF`/`#00B28A`；背景 `#0A0F14`/`#111A23`/`#161F29`；文字 `#F2F6F8`/`#98A1B3`/`#657281`；边框 `#263440`；上涨 `#FF4D4F`，跌幅 `#22C55E`；info `#3882F6`，warning `#F59E0B`；品牌绿只用于 Logo、主按钮、选中、focus 和关键节点，不能替代涨跌色或所有信息蓝。
- **BrandLogo**：使用批准 PNG 资产（CHANGE-20260713-007）：`logo_symbol_128.png`（sidebar）/ `logo_horizontal_dark.png`（landing/footer），禁止恢复手绘 SVG；运行资产位于 `frontend/src/assets/brand/`，ref 不作为运行时依赖。
- **品牌资产**：`frontend/src/assets/brand/`（`logo_symbol_128.png` / `logo_symbol_256.png` / `logo_horizontal_dark.png`）。
- **字体**：中文字体 MiSans/HarmonyOS Sans SC/PingFang SC，fallback Noto Sans CJK SC，数字用等宽字体。
- **卡片与边框**：圆角 10-14px、1px 边框；禁止重阴影和大面积玻璃拟态。
- **硬约束**：视觉改造不得改变 DSA、Node Cluster、盘中监控、Capture 计算口径。
