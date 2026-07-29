# 行情与个股体验 PRD

状态：已确认  
最后确认日期：2026-07-28  
对应 Map：`../maps/40-market-stock-experience.md`  
需求所有权：行情页、个股详情、图层、筛选、排序和导航上下文

## 1. 行情列表

### MX-01 主入口

行情主页面为 `/market`。

### MX-02 页面结构

页面以主表格为核心，并包含可折叠的 EventStatePanel。

不使用旧的侧边栏、概览页或重复 K 线布局。

### MX-03 筛选

支持与实际数据源一致的行业、概念及其他已确认筛选。

行业筛选不得被错误限制为单一层级。

### MX-04 排序

列表排序必须稳定，进入个股详情后来源列表的顺序不得跳变。

### MX-05 筛选上下文保持

从筛选后的行情列表进入个股详情时，详情页的来源列表必须保持原筛选和排序上下文，不得自动切换为自选列表。

## 2. 个股详情

### MX-10 路由

个股详情路由为 `/stock/:symbol`。

### MX-11 单一 K 线

详情页只保留一个 K 线主图。

### MX-12 图层清单

通过统一 `indicatorLayerManifest` 或等价权威配置控制图层，包括：

- 筹码共识区域；
- Bollinger；
- 价格结构；
- 成交量；
- MACD。

### MX-13 用户标签

用户界面不显示内部英文标签；结构相关表达使用中文产品术语。

### MX-14 调试入口

调试仅通过明确管理路由，例如 `/admin/stocks/:id/debug`。不使用 `?debug=1` 作为正式调试入口。

### MX-15 数据状态

页面明确区分：

- loading；
- empty；
- unavailable；
- error；
- permission denied；
- stale。

“筹码共识价暂不可用”等状态不得被空值静默替代。

### MX-20 列表视图第一金字塔全量字段（CHANGE-20260728-008）

行情列表 API 对当前分页 `instrument_ids` 一次批量读取最近完成交易日的 published+full 快照，复用 `summary_payload.first_pyramid`，单次合并，禁止 N+1；无最近日快照返回 null 和 source 状态，不即时逐股计算。

后端唯一扁平化函数（`first_pyramid_flatten.flatten_first_pyramid`）和前端唯一 ColumnRegistry（`firstPyramidColumns.tsx`）必须覆盖 99 个 `fp_` 键，分为 8 组：快照 7 / 趋势 18 / 结构 8 / 结构事件 21 / 动量 13 / 动量事件 9 / 筹码 10 / 量能 13。所有键必须可选，不能少、不能改名。

列设置按分组展示；99 列全部可显示、隐藏、拖拽排序。复用现有 `TableViewPreset` 的 `hiddenColumns`/`columnOrder` 保存，不新建配置表。保存、刷新、重新登录后顺序和显隐恢复；旧配置缺新字段时兼容。

默认只打开基础列 + 约 20 个核心金字塔列，其余默认隐藏。null 统一显示"—"，不得补 0；方向用中文标签和 A 股颜色；分位/BB 位置用小轨道；事件显示最近一次。

`inputHash`、`parameterHash`、`algorithmVersion`、`profile_hash`、`evidence`、`barIndex` 不进入普通列。

## 3. 验收标准

- 筛选后进入详情再返回，列表筛选和排序保持一致。
- 行情列表与详情来源列表的自选排序一致。
- 图层开关由单一权威清单控制。
- 权限不足时不泄露详情数据。
- 列表 API 批量读取快照，无 N+1；99 个 `fp_` 字段全部可选、可显隐、可排序、可保存。
- Map 能指向路由、Store、组件、API 和状态拥有者。

## 4. 个股详情发送飞书稳定条款（CHANGE-20260728-010）

### MX-30 固定组合图，无指标选择器

个股详情“发送到飞书”弹窗不再显示指标视图选择器（不再有 `node_cluster` / `bollinger` / `smc` 单选）。手动发送固定使用同一张“结构 + 筹码共识”组合图，对应固定 Capture Preset：`node` / `profile` / `poc` / `volume` + `smc`，`boll=false`。

### MX-31 后端忽略旧 indicator_view 字段

前端请求体不再携带 `indicator_view`；后端 `POST /instruments/{instrument_id}/send-feishu` 兼容接收旧字段，但必须忽略，不再据此切换图层或文字卡片字段。文字卡片固定展示“结构 + 筹码共识”组合字段，不再出现 Bollinger 三轨字段。

### MX-32 截图超时对齐

截图调用方 timeout 必须大于 Capture 渲染最大 90 秒，固定设为 120 秒；监控自动截图同样对齐。

### MX-33 普通详情页图层工具栏不在本轮范围

普通个股详情页布林带图层工具栏不在本轮删除范围，继续保留 Bollinger 图层开关。本轮仅删除“飞书分享弹窗”与“监控自动截图”链路中的 Bollinger 视图。

## 5. 个股详情自选按钮位置（CHANGE-20260729-007）

### MX-40 删除顶部大号自选按钮

个股详情页顶部 `.actions` 区域不再包含"加入/移出自选"大号按钮；该区域只保留上一只/下一只/全屏等操作。

### MX-41 紧凑自选按钮（22×22px）

在左侧来源列表当前活动股票行（`s.symbol === 当前 symbol`），股票名称右侧放置 22×22 紧凑按钮：
- 未自选显示"+"（品牌青绿色 `#2dd4bf`）
- 已自选显示"−"（弱红色 `#f87171`）
- 复用现有 `handleToggleWatchlist`，不新增 API
- `onClick` 必须 `stopPropagation`，避免触发切股
- `disabled` 覆盖无 instrumentId 及 add/remove pending 状态
- 完整无障碍属性：`type=button`、`title`、`aria-label`、`aria-pressed`、`aria-busy`
- 使用 `.tv-source-name-row` 和 `.tv-watchlist-toggle-mini`，名称保持 ellipsis
- 不改变左栏宽度和行高

### MX-42 Direct 访问 fallback 按钮

direct 访问、来源失效或当前股票不在 sourceStocks 时，为避免功能消失，在顶部股票名称旁显示同款紧凑按钮作为 fallback。capture 模式全部隐藏自选按钮。

### MX-43 自选移除后留在当前详情

加入/移除后依赖现有 watchlist/monitor-status 缓存失效，页面不跳转；自选来源移除当前股后仍留在当前详情，按钮切回"+"。
