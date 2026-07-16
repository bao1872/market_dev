# 07 - Atomic Fact Contract V1 个股状态观察

> 文档状态：CURRENT（backend/frontend 早期部署验证，worker 仍为旧镜像、尚未升级；当前页面主要依赖旧快照 fallback）
> 研究真源：`experiment/hierarchical-scene-state-v3` 的 V4.13 冻结文件  
> 关联 CHANGE：`docs/changes/records/CHANGE-20260716-003.md`、`docs/changes/records/CHANGE-20260716-004.md`、`docs/changes/records/CHANGE-20260716-005.md`、`docs/changes/records/CHANGE-20260716-006.md`
> 关联代码：`backend/app/contracts/atomic_fact_contract_v1.json`、`backend/app/contracts/atomic_fact_presentation_v1.json`、`backend/app/contracts/atomic_fact_product_observations_v1.json`、`backend/app/services/atomic_fact_contract_service.py`、`backend/app/api/stock_context.py`、`frontend/src/features/research-context/AtomicFactsPanel.tsx`、`frontend/src/features/research-context/AtomicFactsDrawer.tsx`、`frontend/src/features/research-context/ConfirmedPositionRow.tsx`、`frontend/src/features/stock-research/stockDetailNavigation.ts`

## 1. 定位与边界

Atomic Fact Contract V1（以下简称 AFC V1）是个股状态观察的**只读事实层**，替换旧的 `StockState` / `StateEventDTO` 普通用户表达层。

它只回答「这只股票当前状态是什么」，不回答「该买卖吗」。所有结论均来自已发布的 DSA 盘后快照（`structural_payload.primary.1d` + `temporal_payload.daily_context`），不引入任何新算法、不拼接、不预测。

**产品边界（硬约束）**：
- 只描述事实，不证明投资价值；
- 不出现综合分、反转概率、买卖、成熟/衰竭、便宜/昂贵、止损、安全、放量/缩量等禁用词；
- 不删除底层 MACD / SQZMOM / Swing / DSA 计算，只删除旧的普通用户表达层（`stock_context` 不再构建 `state`/`events`）；
- 不新增数据库 migration，不新增依赖。

## 2. 合同真源：V4.13 冻结

Canonical Registry 为 `backend/app/contracts/atomic_fact_contract_v1.json`，从研究分支 `experiment/hierarchical-scene-state-v3` 的 V4.13 冻结文件导出，是**唯一真源**（计数 / 顺序 / 阈值 / 边界）。

- 研究状态：`closed`，`research_phase_closed=true`，`v4_14_recommended=false`；
- 不允许重新打开研究、不允许修改合同、不允许部署前改 main；
- 生产实现只以 JSON Registry 为准，计算公式在 `atomic_fact_contract_service.py` 以纯函数重写，不依赖实验脚本运行。

## 3. 事实计数（严格）

| 类别 | 数量 | 说明 |
|---|---|---|
| Core | **14** | 用户主观察面固定四组：趋势 4 + 动量 4 + 结构 5 + 成交 1 |
| Auxiliary | **10** | 默认隐藏，不在普通用户 UI 展示 |
| Rejected | **1** | V1 累计成交量比，永不进入用户 payload / 摘要 / 状态卡 |

四组 Core 顺序（固定）：`trend → momentum → structure → volume`：
- 趋势（4）：T1_trend_direction、T2_aligned_slope、T4_trend_age、T5_slope_ratio
- 动量（4）：M1_momentum_alignment、M2_aligned_momentum、M3_aligned_momentum_delta、M5_squeeze_state
- 结构（5）：S1_confirmed_boundary_relation、S2_active_dir_relation、S3_active_position、S7_dist_favorable_boundary、S8_dist_adverse_boundary
- 成交（1）：V3_avg_volume_ratio

**S2 必须存在**（Active Swing 方向与 DSA 关系），属于 Core 结构组。

## 4. 关键展示规则（生产硬门禁）

1. **valueText 为短原子值（非完整中文长句）**：T1=`上行/下行/中性`，T2=`+0.0123`（+secondaryText 从 presentation `secondaryLabel` 映射），T4=`18`（+secondaryText 从 presentation 映射），T5=`1.23×`（+secondaryText `分类未启用`），M1/M5/S1/S2=仅 categoryLabel（valueText 为 null），M2=`+0.002`，M3=`+0.000300`（+categoryLabel），S3=`0.63`（+轨道 `0.63 · 中间`），S7/S8=`1.23 ATR`（+categoryLabel `尚未到达/已越过`），V3=`1.11×`（+`分类未启用`）。
2. **统一格式器 `_fmt_atomic_value(fact_id, value)`**：精度/后缀均来自 presentation `valuePrecision`，ratio→`1.23×`，distance→`1.23 ATR`（绝对值，禁止负距离），signed（T2/M2/M3）→正数前加 `+`，禁止各处手写 `.4f/.6f/.2f`。**secondaryLabel 为单位/弱说明的唯一真源**：所有 secondaryText 由 presentation 合同映射生成（`_secondary_text_for` 辅助函数），分类未启用时使用 `unclassifiedLabel`；服务中禁止散落 `ATR / 根日K`、`个交易日` 等重复常量。
3. **visualKind 统一枚举**：`metric | value_with_category | relation | position | distance | ratio`（旧 `value`→`metric`，`category`→`value_with_category`）；前端按 visualKind 渲染，**禁止解析中文推断类型/状态**。
4. **T2 / M2 / M3 显示真实原始值**，单位正确（ATR/bar、ATR、无单位），禁止伪造成 [-1, 1] 固定区间。
5. **T5 / V3 阈值未确认**：合同 `thresholds.t5_slope_ratio` / `thresholds.v3_ratio` 的 `lower`/`upper` 均为 `null` 且 `engineering_confirmation_required=true`（THR-001）。因此 UI **只显示原始比值**（`secondaryText=分类未启用`）；不得给出加速/减速、高/低/相近等分类结论。
6. **S3 严格 0.33 / 0.67 边界**：`0–0.33 → 偏低`、`0.33–0.67 → 中间`、`0.67–1.0 → 偏高`；边界 0.33 与 0.67 均归属「中间」（0.63 → 中间）。
7. **S7 / S8 禁止显示负距离**：`d >= 0 → categoryLabel="尚未到达"`，`d < 0 → categoryLabel="已越过"`；valueText 始终显示 `|d| ATR`（绝对值）。
8. **M5 任一缺失即缺失**：`sqz_on`/`sqz_off` 任一为 null → fact 缺失（不进入用户数组）；双 true → 缺失 + `m5_inconsistent` warning；categoryLabel `ON→正在收紧 / OFF→正在释放 / NORMAL→正常`。
9. **M3 阈值未确认（THR-001）**：`thresholds.m3_zero_tolerance.value=null` 且 `engineering_confirmation_required=true`。因此 M3 **不声称 1e-6 容差已确认**；仅按原始值 `raw>0 → 增加`、`raw<0 → 减少`、`raw==0 → 基本不变` 展示，`thresholdEnabled=false`。
10. **V3 是段均量比**（`(cur_vol_sum/cur_age) / (prev_vol_sum/prev_age)`），不是累计量比，禁用「放量 / 缩量」措辞。
11. **V1 永久禁用**：V1_cumulative_volume_ratio 仅作为 DB 调试值保留，永不进入用户 payload、摘要或状态卡（`availability.v1Present` 恒为 `False`，`rejectedPresent` 恒为 `False`）。
12. **T3 / T6 默认关闭**：`FEATURE_FLAGS = {"T3_trend_efficiency": False, "T6_efficiency_delta": False}`（EFF-001/EFF-002 未修复前，普通用户完全不显示；admin debug 中 `featureFlag=false`）。**普通用户 payload 中 T3/T6/V1 永不出现**，expanded「更多观察」只渲染其余 8 项 Auxiliary。
13. **缺失事实由后端直接省略**：`compute_atomic_facts` 的 Core 分组只放入非缺失项；分母固定 14，`availability.coreMissing` 用 publicKey 列表；普通用户 `core` 数组不含 `missing` 项，前端只渲染返回内容，不存在「灰显/伪中性状态」伪装。
14. **近期变化（recentChanges）不是 V4.13 Core Fact**：它是相邻已发布快照间对 14 个 Core 事实的只读对比，不属于合同定义的事实集；每条含中文 `label`（非 publicKey）、`from→to`、`deltaText`（变化类型文案）和 `asOf` 日期；**按每个 Fact 的 presentation `valuePrecision` 量化**（禁止统一 `round(...,4)`），事实消失时通过 `FACT_DIMENSION_BY_ID`（从冻结合同导出）仍返回正确维度（禁止默认 trend）；`from`/`to` 组合短值和 category（`_combine_text`），避免丢失 M3 双文本状态；普通用户 UI 仅在 `/stock/:symbol` expanded 模式的「近期变化」区块展示，**禁止显示 publicKey**。

## 5. 后端实现：单一纯函数（公开 / 调试 / 持久化分离）

`backend/app/services/atomic_fact_contract_service.py`：

- `_compute_emissions(structural_payload, temporal_payload)`：内部实现，返回 `core/auxiliary/availability/debug`（含 debug）。
- `compute_atomic_facts(structural_payload, temporal_payload)`：**公开纯函数**，仅返回 `core/auxiliary/availability`（**不含 debug**）；debug 由管理员请求时按需即时生成，保证持久化 summary_payload 不写入 debug 数组。
- `compute_atomic_fact_debug(structural_payload, temporal_payload)`：**管理员调试**，仅返回 debug 列表（factId/publicKey/sourcePath/rawValue/thresholdRef/thresholdEnabled/featureFlag/missing），按需即时生成。
- `build_persisted_afc_payload(structural_payload, temporal_payload)`：生成可持久化到 `summary_payload.atomic_fact_contract_v1` 的公开快照，含四版本字段（`payloadVersion=1`/`researchContractVersion`/`researchFreezeVersion=V4.13`/`presentationVersion`）+ `core/auxiliary/availability`，**不含 debug**。
- 纯函数，不查库、不联网、不复制底层指标公式、不使用未来数据；
- 旧已发布快照 fallback 与新 `summary_payload.atomic_fact_contract_v1` **共用同一纯函数**（`compute_atomic_facts`），不存在两套公式；persisted-first 与 fallback 公开结果一致（均新格式）。
- `feature_snapshot_service.build_summary_payload` 在写入**新**快照时调用 `build_persisted_afc_payload` 填充 `summary_payload.atomic_fact_contract_v1`（含四版本字段，无 debug）；旧已发布快照受 upsert `WHERE` 保护不覆盖，Context API 仍从 `structural_payload`/`temporal_payload` 重算（不回写 DB）；
- **Context API 优先读取已持久化 `summary_payload.atomic_fact_contract_v1`**：`_is_valid_stored_afc` 使用 `PersistedAtomicFactsPayload` Pydantic schema 严格校验（`extra="forbid"` + `model_validator`）：四版本完全匹配、core 键恰好 `trend/momentum/structure/volume`、每一项通过 `PublicAtomicFactItem`、publicKey 属于正确维度且无重复/未知、T3/T6/V1 不存在、availability 与实际数组及固定分母 14 一致、不含 debug；任一不满足 → 同一纯函数 fallback 重算（**不回写旧快照**，不返回 500）。旧 worker 镜像写入的旧格式（缺版本/含 debug）由 validator 判定 fallback，保证 API 兼容。
- `compute_recent_changes(snapshots)`：接收 ≤10 个升序已发布快照，对 14 个 Core 事实按**每个 Fact 的 presentation `valuePrecision`** 逐对对比（`_quantize_fact_value`，禁止统一 `round(...,4)`），事实消失时通过 `FACT_DIMENSION_BY_ID`（从冻结合同导出的 `fact_id → dimension` 映射）返回正确维度（禁止默认 trend），`from`/`to` 通过 `_combine_text` 组合短值和 category（避免丢失 M3 双文本状态）；返回 `label/publicKey/dimension/fromText/toText/deltaText/asOf`（label 为中文 publicLabel，relation 类 fromText/toText 回退 categoryLabel），最多 30 条；只读计算，不写 `stock_state_events`。

Context API（`backend/app/api/stock_context.py`，复用既有路由，不新增平行接口）：
- `GET /api/v1/stocks/{symbol}/context` → `AtomicFactsContextResponse`（`contractVersion` / `meta` / `asOf` / `core` / `auxiliary` / `availability` / `recentChanges` / `dataQuality`），普通用户只读；`meta` 含 `payloadVersion`/`researchFreezeVersion`/`presentationVersion` 三版本字段（**前端禁止硬编码 V4.13，必须从 `meta.researchFreezeVersion` 读取**）；
- `GET /api/v1/admin/stocks/{symbol}/debug` → `AdminStockDebugResponse`（在用户响应基础上补充 `rawDebug` + `atomicFactsDebug`，admin debug 由 `compute_atomic_fact_debug(snapshot.payloads)` 即时生成，含 Fact ID / 真实路径 / raw value / 阈值来源 / feature flag）；
- **GET 请求零数据库写入**（仅 `SELECT`）；`as_of` 语义为**截止日期**：查 `trade_date <= as_of`，按 `trade_date`/`published_at`/`finished_at` 倒序取最新 1 条；周末和无批次日期必须返回之前最近发布状态（禁止返回未来快照或未来变化，`recent_changes` 按 `as_of` 过滤 ≤ 该日期的已发布快照）。
- **Legacy snapshot 降级原因**：legacy snapshot 存在但 `source_run_id` 缺失/歧义时，不得清除原因；`reasonCode`（如 `snapshot_run_not_linked`/`legacy_snapshot_ambiguous`）加入 `dataQuality.degradedReasons`；无 snapshot 才使用 `reasonCode` 作为唯一空态原因。

## 6. 前端实现

`frontend/src/features/research-context/AtomicFactsPanel.tsx`（替换已删除的 `EventStatePanel.tsx`）+ `AtomicFactsDrawer.tsx`（/stock 右侧 overlay 抽屉）：

**双合同分离**：
- 冻结研究合同 `atomic_fact_contract_v1.json` 只含事实 / 顺序 / 公式 / 阈值 / 路径（V4.13 原字段），**不混入产品层语义**（无 `public_key`/`public_label`）；
- 产品展示合同 `atomic_fact_presentation_v1.json` 按 Fact ID 映射：`publicKey`/`publicLabel`/`visualKind`/`valuePrecision`/`groupTitle`/`secondaryLabel`；生产服务**同时读取**两份合同（frozen 决定事实与计算，presentation 决定产品文案与 UI 类型）。

**Compact（`/market` 右栏）**：
- `MarketRightPanel` 组合 `MiniKlineCard`（顶部）+ `AtomicFactsPanel compact`（底部）；
- Header 两行：第一行「个股状态观察」+「日线 · {meta.researchFreezeVersion}」+ `N/14`（**从 API `meta.researchFreezeVersion` 读取，禁止硬编码 V4.13**）；第二行观察日期；
- 四张组卡（趋势运行/动量配合/结构位置/成交参与），每组一卡；**事实行非卡片**（CSS Grid 透明行 `minmax(0,1fr) auto` + `grid-template-areas "label value" / ". secondary"`，secondary 位于第二行右列且 `text-align: right`，仅底部分隔线）；
- `FactRow` 按 `visualKind` 渲染：metric（数值 mono+高字重+secondaryText）、value_with_category（数值+categoryLabel 徽章）、relation（仅 categoryLabel 徽章一次）、position（完整轨道）、distance（徽章+数值各一次）、ratio（数值+secondaryText）；
- S3/S6 位置轨道使用**独立布局**（`.positionRow`）：第一行 label 左 / `0.63 · 中间` caption 右；第二行轨道横跨整组宽度（`grid-template-areas "label caption" / "track track"`），轨道下方四刻度 低位 / 0.33 / 0.67 / 高位（`railScale` `space-between` 均匀分布），预留刻度高度（`min-height`）禁止刻度与 caption 重叠；
- 四组配色只用 `variables.scss` 现有 token：趋势 `$color-info`、动量 `$color-brand`、结构 `$color-purple`、成交 `$color-warning`，禁止硬编码十六进制；
- 面板内滚动，不改变左侧列表与小 K 线高度；

**Expanded（`/stock/:symbol` 详情）**：
- 点击「显示状态观察」打开右侧 overlay `AtomicFactsDrawer`，宽度 `min(1080px, calc(100vw - 48px))`，固定 overlay **不压缩主 K 线**；
- 四组 Core 在抽屉内响应式横排（宽屏 4 列 / 普通桌面 2 列 / 小屏 1 列）；下方全宽「近期变化」（显示中文 label、`from→to`、`deltaText` 和日期，**禁止显示 publicKey**）；「更多观察」默认收起，Auxiliary 按 **动量补充/结构补充/成交补充** 分组渲染 8 项（T3/T6/V1 永不出现，**10 个 Aux 中仅 8 个可展开**）；
- **Drawer 焦点管理**：打开后聚焦关闭按钮、焦点 trap（Tab/Shift+Tab 限制在抽屉内，**正向 Tab 和 Shift+Tab 均处理焦点离开 drawer 的情况** `!drawer.contains(active)`）、关闭后恢复打开前焦点、body 滚动锁定；
- Escape / 点击遮罩 / 关闭按钮均可关闭，`role="dialog"` `aria-modal` 完整；

**通用约束**：
- 普通用户 DOM 不得出现 `factId` / 字段路径 / 内部英文术语（DSA / SQZMOM / Segment / Active / Developing / bar / raw）；
- 复用 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一 query key `['stock-context', symbol, params]`；`/market` 与 `/stock/:symbol` 共用同一 query key 去重；
- 面板收起（`eventPanelCollapsed` / Drawer 关闭）时父组件不挂载本组件或 Drawer，`useStockContext` 不发起请求（Context 请求为 0）；
- 小 K 线周期切换（15m/60m/1d/1w/1mo）只改图表显示周期，**不改变日线 Atomic Facts**（Facts 始终来自 1d payload）；
- admin debug 页（`AdminStockDebugPage`）消费 `atomicFactsDebug` / `recentChanges`，展示 Fact ID、raw value、真实路径、阈值来源。

## 7. 测试覆盖

| 测试 | 类型 | 覆盖 |
|---|---|---|
| `backend/tests/test_atomic_fact_contract_service.py` | 纯函数 | Registry 14/10/1、顺序、ID 唯一、V1 永久缺席、T3/T6 默认隐藏、T2/M2/M3 真实值、T5/V3 阈值未启用、M5 三态+双 true 异常、M5 四个单侧缺失组合（None/False、False/None、None/True、True/None 均不进 Core）、S3 边界、S7/S8 非负距离、单公式 fallback、compact 完整 14 含 S2、无禁用词、无旧 MACD/SQZMOM 状态/布林位置、recentChanges 无未来、各 Fact 展示精度变化边界、事实由存在变缺失时 dimension 正确（FACT_DIMENSION_BY_ID）、schema 装配（含 meta） |
| `backend/tests/test_atomic_fact_contracts.py` | 双合同结构 | frozen 无产品字段（publicKey/publicLabel）、presentation 恰好 14 Core + 8 Aux、Fact ID 一一对应、T3/T6/V1 不进 presentation、V1 无映射 |
| `backend/tests/test_stock_context_atomic_facts.py` | API/集成（测试库 `bz_stock_test`） | 用户接口不返回 factId/sourcePath/formula/thresholdRef、缺失 Core 省略分母仍 14、M3 未确认无 1e-6、M5 双 true 进 dataQuality、S1 未知枚举缺失、S3 越界省略、S7/S8 admin sourcePath 随趋势变化、summary 优先读取、summary 缺失/旧格式/版本不符 fallback、persisted 与 fallback 一致、as_of SQL LIMIT 前过滤、GET 零写入、recentChanges 按展示精度过滤浮点噪声、admin 完整追溯、普通用户访问 admin 接口 403、**response meta 三版本字段**、**as_of 周末/节假日前最近批次**、**legacy snapshot reason 进 degradedReasons**、**persisted schema 严格 fallback（7 种损坏类型）**、**persisted 全缺失合法** |
| `frontend/src/features/research-context/__tests__/atomic-facts.test.ts` | 契约 | Registry 14/10/1、顺序、ID 唯一、V1 缺席、T3/T6 隐藏、前端类型、presentation 14+8 排除 T3/T6/V1、frozen 无产品字段、用户面板源码无内部术语、**factRow secondary 位于第二行右列**、**PositionRow 独立布局轨道全宽**、**RecentChanges 显示 deltaText**、**Header 从 meta 读取 researchFreezeVersion 禁止硬编码 V4.13**、**Drawer 正反 Tab 焦点限制** |
| `frontend/src/features/market-workspace/__tests__/change010Contract.test.ts` | 契约回归 | MarketRightPanel 组合 MiniKlineCard + AtomicFactsPanel |
| `frontend/src/features/stock-research/__tests__/stockDetailNavigation.test.ts`（CHANGE-20260716-006） | 契约 | originScope 优先级（显式 > returnTo.scope > 默认 watchlist）、`contextMismatch` 冲突检测、三入口（MarketWorkspacePage/useStockDetailActions/StockDetailPage）`buildStockDetailUrl` 一致、旧 `detailNavigation.ts:buildStockDetailUrl` 已删除、`sourceForOriginScope`/`strategyForOriginScope` 映射 |
| `frontend/src/pages/__tests__/detailNavigation.test.ts`（CHANGE-20260716-006 扩展） | 契约 | 过时超长 returnTo 用例 300→5000；originScope 兼容旧 URL |

## 8. 产品观察扩展（CHANGE-20260716-006）

AFC V1 冻结 Core 14 **不修改**。产品观察（Product Observations）是独立第三合同 `backend/app/contracts/atomic_fact_product_observations_v1.json`，从已存在 `structural_payload.primary.1d` 字段派生新的产品展示项，作为顶层 `productObservations` 字段返回，**不计入 Core 14 / `availability.coreDenominator`**。

**Confirmed Swing Position（`confirmed_swing_position`）**：
- **publicKey**：`confirmed_swing_position`；**visualKind**：`confirmed_position`（新增枚举）；**scope**：`product`（与 Core/Auxiliary 区分）；
- **sourceFields**：`raw`=`price_position_in_confirmed_swing_raw`、`confirmed_high`=`confirmed_swing_high`、`confirmed_low`=`confirmed_swing_low`（均来自 `structural_payload.primary.1d`，不修改 worker 持久化链、不修改 `structural_factor_service.py`）；
- **inside 派生**：`inside = raw if 0<=raw<=1 else None`（区间外数值不静默 clip，前端显示「低于确认区间」/「高于确认区间」）；
- **0.33/0.67 边界与 S3 一致**：`0–0.33 → 偏低`、`0.33–0.67 → 中间`、`0.67–1.0 → 偏高`；<0 / >1 不进入 0–1 轨道，仅显示文本；
- **前端**：`ConfirmedPositionRow.tsx` 组件插入结构组「价格与已确认区间关系」之后（保留现有 S3「价格在当前主要波段的位置」），轨道样式与 `PositionRow` 一致；
- **Active Swing vs Confirmed Swing 公式差异**：
  - **Active Swing（S3，当前主要波段，已 clip 到 [0,1]）**：`(active close - active_low) / (active_high - active_low)`，用户值 clip 到 [0,1]；
  - **Confirmed Swing（产品观察，最近确认区间，raw 不 clip）**：`(close - confirmed_low) / (confirmed_high - confirmed_low)`，raw 不 clip，`inside` 仅 0≤raw≤1 时有效。
- **Worker 旧镜像 Known Gap**：当前生产 worker 仍为旧镜像，未持久化新 `productObservations` 字段；但产品观察由 API 层从已存在 `structural_payload.primary.1d` 字段（`price_position_in_confirmed_swing_raw`）派生，**不需要 worker 升级**；fallback 链路同样从旧快照 `structural_payload` 重算，与 persisted-first 输出一致。

## 9. Known Gap / 未确认项

- **THR-001**：T5 slope_ratio、V3 avg_volume_ratio、M3 zero tolerance 阈值需从 V4.12 q20/q80、q25/q75、q33/q67 候选确认；当前 UI 仅显示比值 + 「分类未启用」，M3 不声称 1e-6 已确认。
- **EFF-001 / EFF-002**：T3 趋势效率、T6 效率差存在工程 bug（效率 >1、net 用 DSA 线值而非收盘价），`feature_flag` 默认关闭，未进入普通用户 UI。
- **V1-REJ**：累计成交量比与段年龄比高度相关（Pearson=0.6405），已被 V3 段均量比替代（Pearson=-0.0149），仅保留 DB 调试值。
- 近期变化（recentChanges）非 V4.13 Core Fact，仅作相邻快照对比展示。
- **Worker 仍为旧镜像（Known Gap）**：当前生产 worker 尚未升级，不会持久化新 `summary_payload.atomic_fact_contract_v1`；页面主要依赖旧快照 fallback（同一纯函数重算），新快照持久化链路尚未在 production worker 验证。这是明确的早期验证决策，非错误。
