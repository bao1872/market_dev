# 行情与个股体验 PRD

状态：已确认  
最后确认日期：2026-08-15
对应 Map：`../maps/40-market-stock-experience.md`  

需求所有权：行情页、个股详情、图层、筛选、排序、导航上下文、全局股票搜索、Workspace 层级与 base universe 合同、列设置（显隐/顺序持久化）

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

### MX-06 三层 Workspace 责任层级（CHANGE-20260815-004）

盘迹 workspace 页面采用三层责任结构，PRD 只定义语义层级，不锁定像素或 CSS：

- **Global Header**：跨 workspace 的全局层，不绑定任何单一 workspace。承载一个可输入检索的 **Stock Search**。
- **Module Navigation**：承担 行情 / 自选 / 复盘 / 竞价 切换。
- **Workspace Controls / Content**：仅影响当前 workspace，承载一个 **Industry Search** 与一个 **Concept Search**，以及其它表格筛选、排序、分页、列设置等。

盘迹只有三个用户可见的搜索输入：Global Header 的 Stock Search、Workspace Controls 的 Industry Search 与 Concept Search。Industry Search 与 Concept Search 是两个独立、可输入检索的控件，不承担个股详情导航、不负责自选 add/remove、不改变任何 workspace 的 base universe、不升级为 Universal Query Builder。PRD 只定义语义层级与职责，不锁定像素、宽度或颜色。

新增的 Global Stock Search 位于 Global Header 层级，不属于任何 workspace 的局部筛选器。

### MX-07 全局股票搜索（CHANGE-20260815-004）

Global Stock Search 是现有 Market-local 股票搜索 UI 的 **RELOCATION + RESPONSIBILITY CHANGE**：

- 股票搜索 UI 从 Market workspace 移到 Global Header；
- 它不再作为 Market / Watchlist 表格筛选器；
- 点击股票进入 canonical 个股详情；
- 可以 add/remove canonical 自选成员（见 WI-05）。

Market workspace 不得再保留第二个用户可见的 stock-search input。盘迹至多只有三个用户可见搜索输入（见 MX-06）。

注意：UI ownership 迁移与底层后端 query capability（如 keyword/筛选能力）是否保留是两个独立问题；本合同时不要求删除任何已有后端检索能力。

搜索支持当前正式数据能力已有的匹配维度：

- 股票代码
- 股票名称
- 拼音/首字母（**仅当 ACTUAL 已支持时；当前未确认 ACTUAL 支持，标 FUTURE，不得虚构**）

搜索结果允许两类动作：

- A. 进入 canonical 个股详情（复用 MX-10~MX-12 的 canonical 详情导航合同）；
- B. 通过 canonical 自选成员路径（见 WI-05）添加/删除自选。

Global Stock Search MUST NOT：

- mutate 当前 workspace 的 filter state（Market 或 Watchlist）；
- 缩小当前 workspace 的 base universe；
- 创建第二套股票详情导航（route builder）；
- 创建第二套自选状态（watchlist state）。

### MX-08 Workspace Base Universe 合同（CHANGE-20260815-004）

形式化语义：

```
WorkspaceResult = BaseUniverse ∩ ActiveWorkspaceFilters
```

- **Market base universe** = canonical market universe（全市场发现、扫描与横截面对比）。Market workspace 的 industry / concept / 表格筛选只能在 Market base universe 内过滤。
- **Watchlist base universe** = 当前 authenticated user 的 canonical active 自选成员 universe。任何筛选条件不得把非自选股票引入 Watchlist 结果。

（具体 query 参数、scope/ universe 取值等实现细节见 CHANGE-20260815-004 §3 Code Owner Map 与对应 Map。）

### MX-09 行业 / 概念筛选范围（CHANGE-20260815-004）

Workspace Controls 提供两个独立、可输入检索的 search controls：

- **Industry Search**：只负责当前 workspace 行业过滤；
- **Concept Search**：只负责当前 workspace 概念过滤。

两者：

- 是独立的检索控件，不是 dropdown / command palette / universal search 的替身；
- 不承担个股详情导航；
- 不负责 add/remove 自选；
- 不改变 base universe（MX-08）；
- 不升级为 Universal Query Builder。

单选/多选的 selection semantics 按已有正式能力执行；本轮不新增 multi-select contract。

单选/多选：先按 ACTUAL 记录（当前 MX-03 已确认行业筛选不得错误限制为单一层级；具体 multi-select contract 以 ACTUAL 为准）。本轮不新增尚未成熟的 multi-select 合同。

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

Market workspace 与 Watchlist workspace 的列偏好相互独立：两者 base universe 与任务不同，列配置经由同一 canonical TableViewPreset preference owner 按 workspace 维度隔离。用户在某 workspace 的显隐/顺序修改不影响另一 workspace。列设置不改变底层数据语义与 MX-08 的筛选合同。

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

## 6. 第一金字塔折叠与类型化筛选（CHANGE-20260730-014）

### MX-50 第一金字塔折叠交互（资格与偏好拆分）

个股详情页第一金字塔 detail 区域的折叠状态必须按"资格（availability）+ 偏好（preference）"两段拆分，禁止用单一布尔值表达：

- **`firstPyramidAvailable`（资格）**：只读，由后端返回。表示该股票当日是否存在已发布的 `stock_core` 快照且 99 字段中至少有一个非 `null`。`false` 时整个第一金字塔区域显示结构化 unavailable 状态（不复用"加载中"），用户折叠按钮不可用。
- **`firstPyramidCollapsed`（偏好）**：用户可点击的折叠按钮，仅在 `firstPyramidAvailable=true` 时显示；默认展开（`false`）；状态写入 `localStorage` 键 `panji:first-pyramid-detail-collapsed:v1`，与用户当前选中的 symbol 解耦（一个全局偏好，不按股票保存）。
- **持久键命名**：固定 `panji:first-pyramid-detail-collapsed:v1`；未来键结构变化必须升级版本号 `v2`，不就地覆盖旧键。
- **`StockResearchWorkspace` 收起/展开按钮**：按钮位于第一金字塔 detail 区域顶部右侧，使用 `aria-expanded` 反映状态；点击不触发数据请求，只切换 DOM 折叠；折叠时仍保留顶部一行摘要（趋势/结构/动量/筹码 4 个 chip），便于一眼可见。
- **capture 模式**：`capture=feishu` 时不渲染折叠按钮，强制展开（截图需要完整字段）。
- **首次访问**：无 localStorage 记录时默认展开（`firstPyramidCollapsed=false`）；用户主动收起后，下次访问保持收起状态。
- **`compact` 模式**：`/market` 右栏的 compact 第一金字塔不参与折叠交互，本条款仅适用于 detail 模式。

### MX-51 类型化筛选操作符合同

`/market` 列表 99 字段筛选必须按字段 `data_type` 严格匹配允许的 operators，禁止任意字段使用 `contains`：

| data_type | 允许的 operators | 默认 operator | 值控件 |
|---|---|---|---|
| `text` | `eq` / `ne` / `contains` / `starts_with` / `ends_with` / `is_empty` / `is_not_empty` | `contains` | 文本输入框；`is_empty` / `is_not_empty` 不显示值控件 |
| `enum` | `eq` / `ne` / `in` / `not_in` / `is_empty` / `is_not_empty` | `eq` | 下拉单选；`in` / `not_in` 切换为多选下拉；**禁止默认 `contains`** |
| `boolean` | `eq` | `eq` | 三态开关（true / false / any）；`any` 表示不筛选 |
| `number` | `eq` / `ne` / `gt` / `gte` / `lt` / `lte` / `between` / `is_null` / `is_not_null` | `eq` | 数字输入框；`between` 显示两个输入框 |
| `percent` | `eq` / `ne` / `gt` / `gte` / `lt` / `lte` / `between` | `gte` | 数字输入框，0—100；后端按 0—1 浮点存储，前端显示按百分号 |
| `datetime` | `eq` / `ne` / `gt` / `gte` / `lt` / `lte` / `between` / `is_null` / `is_not_null` | `gte` | 日期选择器；`between` 显示两个日期 |
| `multi_enum` | `in` / `not_in` / `contains_any` / `contains_all` / `is_empty` / `is_not_empty` | `in` | 多选下拉；`contains_any` 表示任一命中、`contains_all` 表示全部命中 |

合同约束：

- 字段元数据 SSOT（`FP_QUERY_FIELD_SPECS`）必须为每个字段声明 `data_type` / `operators` / `enum_values` / `input_control`；前端不得自行推断 operators。
- 前端筛选器组件按 `data_type` 渲染操作符下拉和值控件；用户切换 operator 时若当前值不兼容（如 `contains` → `eq`），必须清空值并提示。
- `enum` 字段不得默认 `contains`；旧 URL 中 `enum+contains` 的迁移规则见 MX-53。
- 后端 `/market/stocks` 接收 `(field, operator, value)` 三元组，按 `data_type` 校验 operator 合法性，非法组合返回 422 + 结构化 `detail`（见 MX-52）。
- `is_empty` / `is_null` 等无值 operator 不接收 `value` 参数；后端必须显式拒绝带 `value` 的请求。

### MX-52 字段元数据 API

新增 `GET /api/v1/market/filter-specs`，返回 `/market` 列表所有可筛选字段的元数据，前端筛选器组件初始化时拉取：

```json
{
  "version": "fp-query-specs-v1",
  "fields": [
    {
      "field": "fp_trend_direction",
      "label": "趋势方向",
      "group": "trend",
      "data_type": "enum",
      "operators": ["eq", "ne", "in", "not_in", "is_empty", "is_not_empty"],
      "default_operator": "eq",
      "enum_values": [
        {"value": 1, "label": "偏多"},
        {"value": -1, "label": "偏空"},
        {"value": 0, "label": "未确认"}
      ],
      "input_control": "single_select"
    },
    {
      "field": "fp_volume_ratio_20d",
      "label": "20日量比",
      "group": "volume",
      "data_type": "percent",
      "operators": ["eq", "ne", "gt", "gte", "lt", "lte", "between"],
      "default_operator": "gte",
      "input_control": "number"
    }
  ]
}
```

合同约束：

- SSOT：`backend/app/config/fp_query_field_specs.py` 的 `FP_QUERY_FIELD_SPECS` 是字段元数据唯一来源；`/market/filter-specs` 直接序列化该 SSOT，禁止在 API 层或前端硬编码字段列表。
- 版本化：`version` 字段反映 SSOT 版本；新增字段或修改 operators 必须升级版本号（如 `fp-query-specs-v2`），前端按版本缓存。
- 权限：任何登录用户可读；不需要 admin。
- 缓存：响应可由 Redis 缓存（key 含 version），TTL 默认 3600 秒；SSOT 升级版本号时旧缓存自然失效。

### MX-53 旧 URL 筛选迁移规则

历史 URL 中可能存在不符合 MX-51 类型化合同的筛选参数（如 `enum` 字段使用 `contains`）。前端解析 URL 时必须按以下规则迁移或提示：

| 旧组合 | 迁移规则 | 用户提示 |
|---|---|---|
| `enum + contains` 且值精确匹配某个 `enum_values[].value` 或 `enum_values[].label` | 自动迁移为 `enum + eq`，使用匹配到的 `value` | 不提示（静默迁移） |
| `enum + contains` 且值不匹配任何枚举值 | 不迁移，operator 保持 `contains` 但后端按 §MX-51 拒绝 | 顶部 banner 提示"字段 X 不支持模糊匹配，请从下拉选择有效值" |
| `text + eq` 但值包含通配符（`*` / `?`） | 不迁移，提示用户改用 `contains` 或 `starts_with` | 顶部 banner 提示 |
| `number + contains` | 非法组合，直接丢弃该筛选条件 | 顶部 banner 提示"字段 X 不支持文本匹配" |
| `boolean + ne` | 不支持，迁移为 `boolean + eq` 并取反值 | 不提示（静默迁移） |
| `percent + eq` 且值 > 100 或 < 0 | 不迁移，提示用户输入 0—100 范围 | 顶部 banner 提示 |
| 缺失 operator（旧 URL 只有 `field=value`） | 按 `default_operator` 补齐 | 不提示（静默迁移） |

合同约束：

- 迁移发生在前端 URL hydration 阶段，迁移后立即更新 URL（`history.replaceState`），避免用户刷新后重复迁移。
- 静默迁移不弹任何提示；非静默迁移必须显示 banner，且 banner 可被用户关闭。
- 后端 `/market/stocks` 接收到非法组合时返回 422 + 结构化 `detail`（含 `field` / `operator` / `reason` / `allowed_operators`），前端按 `detail` 提示用户。
- 迁移规则不写入后端；后端只校验最终接收到的 `(field, operator, value)` 三元组是否合法。

## 7. 统一列表数据源 + 删除DSA旧列 + 详情同源 + 空值语义（CHANGE-20260801-001）

### MX-60 列表唯一数据源 SSOT

**`/market` 行情列表（含自选范围）的唯一后端数据源为 `GET /api/v1/market/stocks`**。

- 禁止前端或列表再读取 `StrategyRun`、`/strategy-results`、`/dsa-results`、`strategyKey` 或任何 DSA-only 运行结果作为列表数据源。
- `/market/stocks` 响应内的 `items[].first_pyramid` 是筛选、排序、列展示、左栏来源列表、导出的唯一字段合同。
- `flatten_first_pyramid`（后端 flatten 层）→ `/market/stocks.items[].first_pyramid`（API 响应）字段一一对应；缺失必须显式标记 `null + reason`，不得在 flatten 层填 0、空字符串或旧值。
- 自选范围（scope=watchlist）与市场范围（scope=market）使用同一 `/market/stocks` 合同，仅 `scope`、`watchlist_id` 或 `user_id` 过滤条件不同。

### MX-61 删除列表DSA-only列（13列）

**立即删除以下旧 DSA 列**，包括：列定义、默认列配置、列设置面板、筛选、排序、preset、导出映射、以及对应测试：

- 趋势
- 连续天
- VWAP差
- 段涨跌
- 斜率
- 强度
- 主要结构
- 短线结构
- 对齐
- OB数
- 事件
- 新鲜度
- 动量

保留：基础列（股票代码/名称/价格/涨跌幅/行业/自选操作）+ 99 个第一金字塔列。

约束：
- 不得删除第一金字塔内部同概念字段（如 `fp_trend_direction`、`fp_structure_state` 等是第一金字塔字段，保留）。
- 不得删除底层第一金字塔计算逻辑。
- 公共 schema 兼容字段（如 `StrategyRun` 相关反序列化）仅在还有其他非列表消费者时保留为 `deprecated: true + null`，前端不得消费。

### MX-62 详情页来源列表同源同序

**个股详情左栏"来源列表"必须与进入详情前的 `/market` 列表使用同一查询合同和同一请求快照。**

1. 同源查询参数：`scope / query / industry / concept / fp_filter / fp_sort / page / page_size` 必须从列表页原样传递到详情页，不得再把第一金字塔筛选参数传给 `strategy-results` 或任何 DSA 旧 API。
2. URL 只保存版本化、可解析的 **market canonical query（简称 MCQ）** 或短 context id，禁止再传 `sourceRunId` + `canonicalQuery`（DSA旧格式）。
3. 当前 symbol 必须使用 6 位规范 A 股代码，不得使用 UUID、DB row index 或非 6 位 alias 作为导航锚点。
4. 左栏顺序与筛选列表**当前页**完全一致；点击"上一只/下一只"按左栏顺序跳转；返回列表页后保留筛选、排序、分页（`history.back()` 后列表请求参数不跳变）。
5. 无效上下文（MCQ 版本无法解析、filter-specs 版本不匹配、page 越界等）必须在左栏顶部显示显式 `reason` banner；合法筛选不得**降级为自选/全市场 direct**。
6. 新生成的详情 URL 只允许使用稳定 `originScope + returnTo + mcq`（以及可选 timeframe/capture）合同；不得再生成或依赖旧 DSA-only `sourceRunId / cq / source / strategy`。刷新、详情内切股和浏览器后退必须保留同一筛选、排序与分页；来源请求错误必须绑定该 MCQ，不得因 fresh run 或旧 run 参数漂移。

### MX-63 空值语义合同（三层一致）

禁止用 0、空字符串、均值或前值来"填补"任何第一金字塔空值。三层（A. DB summary_payload.first_pyramid → B. flatten_first_pyramid 输出 → C. API `items[].first_pyramid`）字段的 `null` 必须一致，并提供显式 `reason`。空值 reason 分四类：

| 分类 | 语义 | 例子 |
|---|---|---|
| `conditional_null` | 合理条件空值 | 单结构段无 prev 段、今日无事件、chip skipped/failed、筹码五元组不匹配、历史观测不足门槛（但仍可展示raw） |
| `insufficient_history` | 数据/历史不足 | 上市不足 60 交易日、15m 数据不足 20 根 K 线 |
| `compute_failed` | 计算失败 | 上游特征抛异常、子任务返回 failed 且不可重试、NaN 被拒绝写入 |
| `mapping_lost` | flatten/API 映射丢失 | 上游 A/B 层非 null 但 API 层字段名错配或未反序列化 |

API 响应字段：
- 第一金字塔字段本体：`null` 表示空，非空表示实际值；
- 字段级 `status`（若已实现）：对应上表；
- `insufficient_history=true` 时，`rawValue` 仍可展示；只有 normalized/分位/筛选信号变空。

> **[字段级 availability 合同 2026-08-04]** 条件性可空因子的字段级原因必须具体化。
> `FirstPyramidSnapshot` **与盘后 `FirstPyramidCoreSnapshot`** 均承载 `fieldAvailability`
> （key=字段路径，value=FieldAvailability），即时完整视图与盘后 stock_core/Review 主链持久化
> 必须一致携带，禁止只在即时路径暴露。
> 合法 reasonCode 共六类：`not_applicable`（语义不适用，如无挤压时挤压期均量）、
> `insufficient_history`（历史样本不足）、`upstream_unavailable`（上游数据缺失）、
> `failed`（计算异常）、`stale`（结果过期）、`missing`（producer 未写该字段）。
> 每个 FieldAvailability 返回：`availability / reasonCode / reasonText / observationCount /
> sourceRunId / calculatedAt`；其中 `sourceRunId`/`calculatedAt` 在盘后主链由编排器
> 按 run 统一注入（同一 run 全股票共享，与 snapshot 级溯源一致），单股即时路径无 run 来源时
> 保持 `None`（不伪造溯源）。此前高空值字段（`momentum.squeeze_avg_volume`、
> `momentum.volume_relation`、`momentum.sqzmom_value`）在维度可用但无挤压时必须
> 标记 `not_applicable`，上游缺失时标记 `upstream_unavailable`，禁止无原因的空 `null`。

> **[FP 失败完整性 2026-08-04]** 第一金字塔属 core 必选结果。`compute_review_core_for_trade_date`
> 中第一金字塔计算异常不得以无原因的 `first_pyramid=None` 冒充成功：必须记录明确的
> `first_pyramid_status=FP_COMPUTE_FAILED` 与 degraded reason，且该股票不得计入
> publish-ready coverage（batch 路径计入 failed_count 且不 upsert；run-items 路径标记 item failed），
> 否则"盘后任务成功但第一金字塔字段大量为空"无法被发布门识别。

### MX-64 导出合同

列表导出字段必须与 `/market/stocks` 的筛选、排序合同一致。禁止在导出后端重新查询 DSA run 结果或拼接第一金字塔外的字段。

导出的唯一 SSOT 是 `/market/stocks + 同一份 filter-specs` 的响应数据子集。

## 8. 网关、筛选与详情闭环合同（2026-08-01）

- 浏览器 API client 的 `baseURL` 固定为 `/api`，业务 endpoint 固定写 `/v1/...`；Vite 与 Nginx 各只去除一次 `/api`。
- canonical operator 输出只允许 `neq/empty/not_empty/has_any/has_all/not_has_any/date_eq`；
  `ne/is_empty/is_null/is_not_empty/is_not_null/contains_any/contains_all/not_contains_any`
  仅作为旧输入兼容，保存与响应必须 canonical 化。
- `/market/stocks` 的 MCQ 同时驱动列表、详情来源、左右导航和上一只/下一只；URL hydration 不得覆盖合法筛选。
- symbol 用于用户 URL，UUID 用于后端实体关联，两者不得互换。
- 自选切换先更新当前行即时状态，再按 `market-stocks` query key 失效缓存。
- 第一金字塔 99 字段、chip skipped 原因、结构/筹码截图和飞书详情链接继续复用同一 canonical 数据合同。
