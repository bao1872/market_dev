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
| MX-20 列表视图第一金字塔全量字段 | `backend/app/services/first_pyramid_flatten.py`（99 键扁平化函数）；`backend/app/services/market_stocks_service.py`（批量读取快照，无 N+1；**[CHANGE-008]** `_build_snap_lateral(snapshot_run_id=...)` 严格绑定已发布 `factor_publications` 的 `data_run_id`，无 pointer 时回退每股 latest；**[CHANGE-009]** 单一数据源，删除前端双分页合并架构，统一使用 `useMarketStocks`；`MarketStockRow` schema 扩展 `payload/data_run_id/factor_ready/factor_error/factor_actual_bars/factor_required_bars/chip_status` 字段；`_compute_factor_ready` 区分新股数据不足（`INSUFFICIENT_DAILY_BARS`）与程序异常；`_build_chip_status_struct` 从 `stock_chip_consensus_snapshots` 严格匹配返回结构化状态）；`backend/app/schemas/market_stocks.py`（`MarketStockRow.first_pyramid` + `ChipStatus`）；`frontend/src/features/market-workspace/firstPyramidColumns.tsx`（ColumnRegistry 99 列）；`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`（**[CHANGE-009]** 删除 `useStrategyRunResults`，仅使用 `useMarketStocks`）；`frontend/src/features/trend-selection/adapters.ts`（`adaptMarketStockToTrendRow` 全量字段映射）；`frontend/src/components/StrategyDataTable.tsx`（`defaultHiddenColumns` prop）；复用 `TableViewPreset` 保存显隐与顺序 | 已实现未运行核验 | 25 个后端纯单元测试通过（`test_market_stocks_helpers.py`）+ 8 个前端 adapter 测试（`adapter.test.ts`）；TSC+ESLint 通过；浏览器真实链路验收待生产部署后执行 |

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

## 4.6 个股详情自选按钮位置（CHANGE-20260729-007）

| 位置 | 实现 | 触发条件 |
|---|---|---|
| 顶部 `.actions` | **已删除**大号"加入/移出自选"按钮 | 全部场景 |
| 左栏活动行 | `WatchlistToggleButton`（`.tv-watchlist-toggle-mini` 22×22px） | `s.symbol === 当前 symbol` |
| 顶部股票名称旁 | `WatchlistToggleButton` fallback | direct 访问 / 来源失效 / 当前股不在 sourceStocks |

- 组件入口：`frontend/src/pages/StockDetailPage.tsx` 内 `WatchlistToggleButton`
- 样式：`frontend/src/styles/global.scss`（`.tv-source-name-row` + `.tv-watchlist-toggle-mini`，+ 品牌青绿 `#2dd4bf`，− 弱红 `#f87171`）
- 复用：`detailActions.handleToggleWatchlist`，不新增 API
- capture 模式（`capture=feishu`）全部隐藏自选按钮
- 无障碍：`type=button`、`title`、`aria-label`、`aria-pressed`、`aria-busy`；`onClick` 使用 `stopPropagation` 避免切股
- pending/disabled：`addWatchlistPending || removeWatchlistPending || !instrumentId`

## 4.7 行情列表单一数据源与内联自选按钮（CHANGE-20260729-009）

**核验状态：未运行核验（2026-07-29 新增）**

### 4.7.1 单一数据源架构

`/market` 行情列表统一使用 `/market/stocks` 单一服务端分页接口，**禁止**前端再调用 `/strategies/runs/{run_id}/results` 双分页合并。

| 入口 | 实现 | 说明 |
|---|---|---|
| 前端数据 hook | `useMarketStocks`（`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`） | 唯一数据源；删除 `useStrategyRunResults` |
| 前端适配器 | `adaptMarketStockToTrendRow`（`frontend/src/features/trend-selection/adapters.ts`） | 直接将 `MarketStockRow` 转换为 `TrendSelectionRow`；不再合并两份数据 |
| 后端 API | `GET /market/stocks`（`backend/app/api/market.py`） | 一次性返回页面所需全部字段 |
| 后端服务 | `get_market_stocks`（`backend/app/services/market_stocks_service.py`） | LATERAL JOIN 一次取出 snapshot + chip + watchlist，避免 N+1 |

### 4.7.2 `MarketStockRow` 完整字段合同

```python
class MarketStockRow(BaseModel):
    # 基础
    instrument_id: str
    symbol: str
    name: str
    latest_price: float | None
    change_pct: float | None
    industry: str | None
    concepts: list[str]
    # DSA + 事件
    dsa_state: str | None
    structure_state: str | None
    latest_event_title: str | None
    latest_event_time: str | None
    # 自选
    is_watchlisted: bool
    # 第一金字塔 99 字段
    first_pyramid: dict | None
    # DSA payload（与 first_pyramid 分离）
    payload: dict | None
    # 已发布数据版本
    data_run_id: str | None
    # 因子就绪状态
    factor_ready: bool
    factor_error: str | None        # INSUFFICIENT_DAILY_BARS / 程序异常
    factor_actual_bars: int | None
    factor_required_bars: int | None
    # 筹码状态（结构化）
    chip_status: ChipStatus | None
```

### 4.7.3 `ChipStatus` 结构化合同

```python
class ChipStatus(BaseModel):
    status: str | None          # succeeded / skipped / failed / unavailable
    reason_code: str | None    # M15_BARS_INSUFFICIENT / INSUFFICIENT_DAILY_BARS
    reason_text: str | None    # 中文文案，可直接显示给用户
    actual_bars: int | None    # 实际 15m 根数
    required_bars: int | None  # 需要 15m 根数（500 最低 / 4000 完整）
    computed_at: str | None    # ISO 时间戳
```

`_build_chip_status_struct` 实现：

- 严格匹配 `(instrument_id, trade_date, core_run_id, algorithm_version, status=succeeded)`
- 成功 → `status=succeeded + reason_text="已计算"`
- 无记录/失败 → 调用 `first_pyramid_service.compute_chip_status_for_stock` 计算原因
- 000021 实际值：`status=skipped + reason_code=M15_BARS_INSUFFICIENT + actual_bars=354 + required_bars=500`

### 4.7.4 `factor_ready` 判定规则

| 场景 | factor_ready | factor_error | 说明 |
|---|---|---|---|
| 趋势/结构/动量三维度 `available=true` | `true` | `null` | 正常 |
| 任一维度 `available=false` + 日线<60 | `false` | `INSUFFICIENT_DAILY_BARS` | 新股数据不足，**不算 failed** |
| 任一维度 `available=false` + 程序异常 | `false` | 程序异常错误信息 | 才算 failed |

`factor_actual_bars` / `factor_required_bars` 始终填充（即使 factor_ready=true 也可显示）。

### 4.7.5 行情列表内联自选按钮

| 位置 | 实现 | 触发条件 |
|---|---|---|
| 股票名称列同行 | `WatchlistToggleButton`（22×22px） | 所有行 |
| 独立 action 列 | **已删除** | 全部场景 |

- 组件入口：`frontend/src/features/market-workspace/columns.tsx` 内 `WatchlistToggleButton`
- 复用现有 `useAddWatchlist` / `useRemoveWatchlist` mutation、pending 状态、query invalidate
- `stopPropagation` 防止触发行点击
- 无障碍：`type=button` / `title` / `aria-label` / `aria-pressed` / `aria-busy`
- 其他复用 `/market` 的页面（如 `/replay`）不受影响

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

## 6.1 飞书发送链路（CHANGE-20260728-010）

个股详情"发送到飞书"链路：

- 入口组件：`frontend/src/pages/StockDetailPage.tsx`（弹窗）
- Hook：`frontend/src/features/stock-research/useStockDetailFeishu.ts`
- API 函数：`sendStockDetailFeishu`（`frontend/src/api/endpoints.ts`）
- 后端路由：`POST /instruments/{instrument_id}/send-feishu`（`backend/app/api/stock_detail_feishu.py`）
- 服务层：`backend/app/services/stock_detail_feishu_service.py`

[CHANGE-20260728-010] 固定组合图合同：

- 弹窗无指标选择器（移除 node_cluster/bollinger/smc 三选一 radio）
- 文案固定显示"将发送：结构 + 筹码共识组合图（含日线 SMC 结构与成交量节点）"
- 请求体不携带 `indicator_view`（旧字段兼容接收但忽略）
- 后端强制使用 `FEISHU_CAPTURE_VIEW='structure_node'`
- 截图调用方 timeout=120s（`CAPTURE_HTTP_TIMEOUT_SECONDS`）
- 文字消息使用 `build_monitor_event_text(indicator_view='structure_node')`，含 node + smc 字段（无 BB 字段）

Capture 页面链路：

- 入口：`frontend/src/pages/CaptureStockPage.tsx`（路由 `/capture/stock/:symbol`）
- 舞台组件：`frontend/src/components/MobileIndicatorStage.tsx`
- 图表组件：`frontend/src/components/StrategyChart.tsx`（`isCaptureMode=true`）
- 后端 API：`GET /capture/stocks/{instrument_id}/snapshot`（`backend/app/api/capture.py`）

[CHANGE-20260728-010] Capture 固定组合视图：

- 前端固定 `indicatorView = FEISHU_CAPTURE_VIEW`（'structure_node'）
- Capture query 透传 `indicator_view=structure_node`，后端忽略该参数渲染逻辑（仅用于缓存键维度）
- 图层固定使用 `FEISHU_CAPTURE_LAYER_PRESET`（node=true, smc=true, boll=false）
- combined Ready = `nodeReady && smcContractReady`（纯函数 `computeCombinedReady`，位于 `frontend/src/features/stock-research/captureReady.ts`）
- SMC DTO 结构必须存在（events/order_blocks 为数组允许为空；swing_bias 为有限 number，1/-1/0）
- module-label 显示 "结构 + 筹码共识"（`INDICATOR_VIEW_LABELS['structure_node']`）

[CHANGE-20260728-010 P0 修复补丁（2026-07-29）]
- 根因：旧 `computeCombinedReady` 错误要求 `Array.isArray(swing_bias)`，但 `swing_bias` 是 number(1/-1/0)，
  导致组合截图永远无法 Ready，Capture Worker 30s 超时返回 502。
- 修复：提取为独立纯函数 `captureReady.ts`，修正 swing_bias 类型判断为 `typeof === 'number' && Number.isFinite`。
- 同时简化 `sendStockDetailFeishu`：删除无效 `payload` 参数，固定 POST `{}`。

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
