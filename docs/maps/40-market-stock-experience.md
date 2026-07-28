# 行情与个股体验 Map

核验状态：待重建  
最后核验日期：未核验  
核验分支：未核验  
核验提交：未核验  
核验范围：尚未基于最新代码完整核验  
对应 PRD：`../prd/40-market-stock-experience.md`  
事实所有权：前端路由、页面、组件、筛选排序状态、详情来源列表和图层清单

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| MX-01 | `/market` 路由待核验 | 部分已知 | 未核验 |
| MX-02 | 主表格和 EventStatePanel 待核验 | 部分实现 | 未核验 |
| MX-03 | 行业/概念筛选待核验 | 部分实现 | 未核验 |
| MX-04 | 列表排序待核验 | 已知曾有偏差 | 未核验 |
| MX-05 | 来源列表上下文待核验 | 已知曾有偏差 | 未核验 |
| MX-10 | `/stock/:symbol` 待核验 | 部分已知 | 未核验 |
| MX-11 | 单一 K 线待核验 | 部分实现 | 未核验 |
| MX-12 | `indicatorLayerManifest` 待核验 | 部分实现 | 未核验 |
| MX-13 | 中文标签待核验 | 部分实现 | 未核验 |
| MX-14 | 管理调试路由待核验 | 部分已知 | 未核验 |
| MX-15 | 页面状态待核验 | 未核验 | 未核验 |
| MX-20 列表视图第一金字塔全量字段 | `backend/app/services/first_pyramid_flatten.py`（99 键扁平化函数）；`backend/app/services/market_stocks_service.py`（批量读取快照，无 N+1）；`backend/app/schemas/market_stocks.py`（`MarketStockRow.first_pyramid`）；`frontend/src/features/market-workspace/firstPyramidColumns.tsx`（ColumnRegistry 99 列）；`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`（集成 `useMarketStocks`）；`frontend/src/components/StrategyDataTable.tsx`（`defaultHiddenColumns` prop）；复用 `TableViewPreset` 保存显隐与顺序 | 已实现未运行核验 | 31 个纯单元测试通过（`test_first_pyramid_flatten.py`）；TSC+ESLint 通过；浏览器真实链路验收待部署后执行 |

## 2. 路由

| 路由 | 页面组件 | 权限 | 数据入口 |
|---|---|---|---|
| `/market` | 待核验 | 待核验 | 待核验 |
| `/stock/:symbol` | 待核验 | 待核验 | 待核验 |
| `/admin/stocks/:id/debug` | 待核验 | 管理员 | 待核验 |

## 3. 行情页组件

| 组件 | 路径 | 状态拥有者 | API |
|---|---|---|---|
| 主表格 | 待核验 | 待核验 | 待核验 |
| 筛选器 | 待核验 | 待核验 | 待核验 |
| EventStatePanel | 待核验 | 待核验 | 待核验 |
| 行列表 | 待核验 | 待核验 | 待核验 |

## 4. 个股详情组件

| 组件 | 路径 | 数据来源 | 责任 |
|---|---|---|---|
| K 线 | 待核验 | 待核验 | 主图 |
| 图层控制 | 待核验 | `indicatorLayerManifest` 待核验 | 图层开关 |
| 来源列表 | 待核验 | 待核验 | 上下文导航 |
| 状态提示 | 待核验 | 待核验 | loading/empty/error 等 |

## 4.1 一级导航（CHANGE-20260728-006）

普通用户一级导航固定为「行情｜自选｜复盘」，承载于 `UserAppShell` 顶栏。

| 导航项 | 链接 | 权限 | 备注 |
|---|---|---|---|
| 行情 | `/market`（scope != watchlist） | 可进入 `/market` 的用户 | 默认入口 |
| 自选 | `/market?scope=watchlist` | admin 或 `self_selection` active | 复用 `MarketWorkspacePage`，不新建 WatchlistPage |
| 复盘 | `/replay` | admin 或 `research_replay` active | 后端权限守卫不变 |

实现要点：

- 真源：`frontend/src/navigation/appNavigation.ts` 导出 `USER_NAV_ITEMS`、`WATCHLIST_NAV_PATH`、`resolveActiveNav`、`buildScopeSwitchUrl`。
- `UserAppShell.tsx` 不依赖 `NavLink` 的 pathname 判断两个 `/market` 入口，使用 `resolveActiveNav(pathname, searchParams, itemPath)` 自定义 active：
  - `/market` 且 `scope != watchlist` → 行情 active
  - `/market` 且 `scope == watchlist` → 自选 active
  - `/replay` → 复盘 active
- 点击行情/自选时通过 `buildScopeSwitchUrl(currentParams, newScope)` 保留 `keyword/industry/concept/sort/dir/filters/page_size`，更新 `scope`，删除 `selected`，将 `page` 重置为 1。
- `MarketToolbar.tsx` 彻底删除 `scopeTabs/scope/onScopeChange/canAccessWatchlist` 相关 UI 和 Props，只保留股票搜索、行业、概念筛选。行情和自选下均完整显示同一 Toolbar。

## 4.2 右栏布局（CHANGE-20260728-006）

`MarketRightPanel.tsx` + `MarketRightPanel.module.scss` 固定为「小K线固定区 + 状态滚动区」两段 Flexbox 布局，防止小K线被压缩。

DOM 结构：

```
.panel (flex column, height:100%, overflow:hidden)
├── .klineFixed (flex:0 0 230px, height:230px, overflow:visible)
│   └── <MiniKlineCard symbol={symbol}/>
└── .stateScroll (flex:1 1 auto, min-height:0, overflow-y:auto)
    ├── <FirstPyramidPanel symbol variant="compact"/>  (symbol 存在时)
    └── .moreObservation                                    (symbol 存在时)
        ├── toggle 按钮（▶/▼ 更多观察）
        └── <AtomicFactsPanel symbol variant="compact"/>  (moreOpen=true 时才挂载)
```

`global.scss` 同步修正 `.mini-kline-card`（flex:0 0 auto, flex-shrink:0, height/min-height:230px, overflow:visible）、`.mini-kline-tabs`（flex:0 0 26px）、`.mini-kline-chart`（flex:0 0 190px, height:190px），保证 15m/60m/日/周/月按钮及图表底部时间轴完整可见，下方第一金字塔不压缩小K线。

## 4.3 第一金字塔双页面落点

| 落点 | 组件 | variant | 路径 |
|---|---|---|---|
| `/market` 右栏 | `FirstPyramidPanel` | `compact` | `frontend/src/features/market-workspace/MarketRightPanel.tsx` |
| `/stock/:symbol` Drawer | `FirstPyramidPanel` | `detail` | `frontend/src/features/research-context/AtomicFactsDrawer.tsx` |

- 共享组件：`frontend/src/features/stock-research/FirstPyramidPanel.tsx`
- ViewModel：`frontend/src/features/stock-research/firstPyramidViewModel.ts`（DTO→VM 类型安全转换，禁止解析 statusText 推断多空）
- 样式：`frontend/src/features/stock-research/FirstPyramidPanel.module.scss`
- API：`GET /api/v1/stocks/{symbol}/first-pyramid`（共用 React Query 缓存，不重复请求）
- StockDetailPage 底部不再渲染独立 FirstPyramidPanel（全页只有一个实例）

## 4.4 第一金字塔视觉字段合同（CHANGE-20260728-006 重构）

compact 与 detail 复用同一组 VisualCard，不复制业务判断。compact 单列纵向；detail 两列（趋势/动量），结构跨两列，事件最多 5 条。

### 4.4.1 compact DOM 结构

```
.panel.compact
├── Header (标题 + tradeDate)
├── StateRibbon (1 行 4 个紧凑状态标签，高度 28px)
├── VolumeWaterLevel (20日/200日两条分位轨道)
└── .dimensionsCompact (单列)
    ├── TrendVisualCard
    ├── StructureVisualCard
    ├── MomentumVisualCard
    └── ChipVisualCard
```

### 4.4.2 禁止显示

- compact 禁止渲染 `PyramidSummaryStrip(statusText)`；`statusText` 只保留在 DTO，不进入 compact DOM。
- 任何模式禁止向普通用户显示 `DSA/Swing/Internal/CHoCH/Squeeze/dir_bars/Node/algorithmVersion/inputHash/parameterHash` 等内部英文与 Hash。
- 禁止显示原始 volume 大整数，仅显示 ratio。
- 不得使用 VAH/VAL 替代 POC。

### 4.4.3 StateRibbon 字段

一行 4 个紧凑状态标签，字号 11px、单行、省略、title 提供完整中文。

| 标签 | 字段来源 | 取值 |
|---|---|---|
| 趋势 | `vm.trend.direction` | 偏多/偏空/未确认 |
| 结构 | `vm.structure.swingDirection` | 主要结构方向 |
| 动量 | `vm.momentum.squeezeOn + direction` | 挤压/释放 + 方向图标 |
| 筹码 | `vm.chipConsensus.positionLabel` | POC上方/下方/贴合/可选 |

### 4.4.4 趋势卡（TrendVisualCard）

| 元素 | 字段来源 | 显示规则 |
|---|---|---|
| 标题方向箭头 | `vm.trend.direction` | 1=↑偏多, -1=↓偏空, 0=→未确认 |
| 方向轨道 marker | `vm.trend.direction` | 偏空=0%, 未确认=50%, 偏多=100% |
| 持续N根 | `dsa_dir_bars` → `continuousBars` | null 不显示 |
| 距 DSA VWAP | `dsa_vwap_dev_pct` → `vwapDeviationPct` | null 不显示，不补 0 |
| 当前段量比 | `current_vs_prev_volume_ratio` → `segmentVolumeRatio` | 0~2x 映射到 0~100%，超出 2x 封顶；右侧保留真实倍率 |
| 趋势强度 | `trend_strength` → `trendStrength` | null 不显示 |

### 4.4.5 结构卡（StructureVisualCard）

第一行两个独立状态块：[主要结构方向] [短线结构方向]；下方最多 3 个事件（compact）/ 5 个事件（detail），每个事件一行独立 chips，禁止 join 为长字符串。

| chip | 字段来源 | 显示规则 |
|---|---|---|
| 事件名称 | `EVENT_TYPE_LABEL` | BOS=结构突破, CHoCH=结构转折, OB_ENTRY=进入订单区域, EQH=连续高点, EQL=连续低点 |
| 级别 | `extra.structure_level` | swing=主要级别, internal=短线级别；EQH/EQL 为空时不显示级别 chip |
| 方向 | `event.direction` | up=上行, down=下行 |
| 新鲜度 | `freshnessBars` | 今日/1根前/N根前；detail 额外显示 occurredAt 与 price |
| 量能徽标 | `event.volumeBadge` | 放量/缩量/正常 |

### 4.4.6 动量卡（MomentumVisualCard）

| 元素 | 字段来源 | 显示规则 |
|---|---|---|
| 状态 chip | `squeeze_on` | 挤压中/已释放 |
| 方向 chip | `sqzmom_val` 符号 → `direction` | 偏多/偏空/中性 |
| BB 位置轨道 | `bb_position` → `bbPosition` | 0=下轨, 0.5=中轨, 1=上轨；marker 限制 0~1 |
| 动量变化 | `sqzmom_val` vs `sqzmom_val_prev` → `momentumChangeLabel` | 增强/减弱/转多/转空/持平 |
| 量价标签 | `vol_divergence` → `volDivergence` | 直接显示 |
| 原始值 | `sqzmom_val/bb_width/release_vs_squeeze_volume_ratio` | 仅 detail 模式小字显示，compact 不显示 |

### 4.4.7 筹码卡（ChipVisualCard）

| 元素 | 字段来源 | 显示规则 |
|---|---|---|
| POC 中心位置轨道 | `poc_price`/`last_close` | 中心为 POC；当前价 marker 按 ±10% 范围映射并 clamp |
| 距离百分比 | `distancePct = (lastClose - pocPrice) / pocPrice * 100` | 真实显示 |
| 峰数量 | `n_peak_nodes` → `nPeakNodes` | 真实显示 |
| 空态 | `pocPrice == null` | 灰色「可选维度 · 暂无有效筹码峰」 |

### 4.4.8 量能水位

20日/200日两条分位轨道。单项 null 时该行显示「样本不足」，不整块消失或填 0。百分位显示「72%」格式（不是裸数字）。badge 显示「放量/缩量/正常」。

### 4.4.9 compact 尺寸与颜色

- 外边距 0；padding 10px；卡片单列；gap 8px；卡片 padding 9px 10px；圆角 6px；标题 12px；正文 11px。
- A 股颜色：偏多红、偏空绿、中性灰；品牌莹感绿只用于量能、轨道和选中状态。禁止四块大面积彩色背景。

## 4.5 第一金字塔历史落点（CHANGE-20260728-005，已被 006 部分重构）

- 历史实现曾包含 2x2 SummaryGrid、PyramidSummaryStrip、原始 volume 大整数；CHANGE-20260728-006 已删除并替换为 VisualCard + 轨道 + chip 设计。

## 5. 状态所有权

重点核验：

- 行情筛选状态；
- 排序状态；
- 当前来源列表；
- 自选排序；
- 详情当前 symbol；
- 返回后的上下文；
- 图层开关；
- 权限状态。

| 状态 | 权威拥有者 | URL | Store | Local State |
|---|---|---|---|---|
| 筛选 | 待核验 | 待核验 | 待核验 | 待核验 |
| 排序 | 待核验 | 待核验 | 待核验 | 待核验 |
| 来源列表 | 待核验 | 待核验 | 待核验 | 待核验 |
| 图层 | 待核验 | 待核验 | 待核验 | 待核验 |

## 6. 与 PRD 的已知偏差

需重点重新验证：

- 筛选后进入详情，来源列表是否仍跳回自选；
- 行情页与详情页自选排序是否一致；
- 图层是否全部由统一清单控制；
- `?debug=1` 是否彻底移除。

## 7. 验证入口

以用户真实交互路径验证，不使用 IDE 截图代替行为核验。

## 8. 前端验证结果（Phase 5B-0）

**验证环境**：本地原生 Backend (port 8000) + Frontend (port 8008) + SSH 隧道（panji-prod 43.136.118.82）；admin token 认证；2026-07-27。

**验证方式**：HTTP 状态码 + API 响应 + 浏览器运行错误（不安装浏览器自动化依赖，不以截图为唯一证据）。

| 路由 | 页面加载 | 主要 API | 数据展示 | 权限 | 阻塞原因 |
|---|---|---|---|---|---|
| `/` | 失败（无限刷新） | - | - | 公开 | 本地 Vite 无 Nginx 前置，`LandingPage` `window.location.replace('/')` 触发循环；生产环境 Nginx 精确分流不受影响 |
| `/login` | OK | - | 登录表单 | 公开 | - |
| `/market` | OK | `/market/stocks` 200、`/market/boards` 200、`/market/status` 200 | 行情列表 | 需登录 | - |
| `/replay` | OK | `/strategies` 200 | 策略列表 | 需订阅 | - |
| `/stock/000001` | OK | `/api/v1/stocks/000001/context` 200、`/api/v1/instruments/{id}/bars` 200、`/indicators` 200、`/structural-factors` 200、`/temporal-features` 200、`/quote` 200、`/chart-snapshot` 200 | 个股详情 + K 线 + 指标 | 需订阅 | - |
| `/settings` | OK | `/me` 200、`/me/access` 200、`/me/membership` 404（admin 无订阅） | 用户设置 | 需登录 | - |
| `/messages` | OK | `/messages` 200、`/messages/unread-count` 200 | 消息列表 | 需登录 | - |
| `/admin/stocks/000001/debug` | OK | `/api/v1/admin/stocks/000001/debug` 200 | 调试面板 | 管理员 | - |

**重定向路由**（SPA 客户端重定向，HTTP 200）：`/overview`、`/watchlist`、`/screener`、`/admin/strategies`、`/admin/stock-debug/:symbol`、通配符 `*`。

**结论**：除 `/` 受本地 Vite 限制外，所有用户级和管理员路由均正常加载，主要 API 返回 200，数据展示正确，权限模型符合预期。详细 API 响应记录在 `docs/maps/80-system-runtime.md` §9。
