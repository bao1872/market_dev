# 07 - Atomic Fact Contract V1 个股状态观察

> 文档状态：CURRENT（实现已交付，生产待部署验证）  
> 研究真源：`experiment/hierarchical-scene-state-v3` 的 V4.13 冻结文件  
> 关联 CHANGE：`docs/changes/records/CHANGE-20260716-002.md`  
> 关联代码：`backend/app/contracts/atomic_fact_contract_v1.json`、`backend/app/services/atomic_fact_contract_service.py`、`backend/app/api/stock_context.py`、`frontend/src/features/research-context/AtomicFactsPanel.tsx`

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

1. **T2 / M2 / M3 显示真实原始值**，单位正确（ATR/bar、ATR、无单位），禁止伪造成 [-1, 1] 固定区间。
2. **T5 / V3 阈值未确认**：合同 `thresholds.t5_slope_ratio` / `thresholds.v3_ratio` 的 `lower`/`upper` 均为 `null` 且 `engineering_confirmation_required=true`（THR-001）。因此 UI **只显示原始比值**，并标注「分类未启用」；不得给出加速/减速、高/低/相近等分类结论。
3. **S3 严格 0.33 / 0.67 边界**：`0–0.33 → 偏低`、`0.33–0.67 → 中间`、`0.67–1.0 → 偏高`；边界 0.33 与 0.67 均归属「中间」（0.63 → 中间）。
4. **S7 / S8 禁止显示负距离**：`d >= 0 → "尚未到达 |d| ATR"`，`d < 0 → "已越过 |d| ATR"`。
5. **M3 零值容差统一 1e-6**（对应 `sqzmom_delta_1` 存储精度，非数据分位数）。
6. **V3 是段均量比**（`(cur_vol_sum/cur_age) / (prev_vol_sum/prev_age)`），不是累计量比，禁用「放量 / 缩量」措辞。
7. **V1 永久禁用**：V1_cumulative_volume_ratio 仅作为 DB 调试值保留，永不进入用户 payload、摘要或状态卡（`availability.v1Present` 恒为 `False`，`rejectedPresent` 恒为 `False`）。
8. **T3 / T6 默认关闭**：`FEATURE_FLAGS = {"T3_trend_efficiency": False, "T6_efficiency_delta": False}`（EFF-001/EFF-002 未修复前，普通用户完全不显示；admin debug 中 `featureFlag=false`）。
9. **缺失事实直接省略**（不填 0 / 空串 / 中性状态伪装），`missing=true` 由前端按行隐藏。
10. **近期变化（recentChanges）不是 V4.13 Core Fact**：它是相邻已发布快照间对 14 个 Core 事实的只读对比，不属于合同定义的事实集；普通用户 UI 仅在 `/stock/:symbol` expanded 模式的「近期变化」区块展示。

## 5. 后端实现：单一纯函数

`backend/app/services/atomic_fact_contract_service.py::compute_atomic_facts(structural_payload, temporal_payload)` 是唯一计算入口：

- 纯函数，不查库、不联网、不复制底层指标公式、不使用未来数据；
- 旧已发布快照 fallback 与新 `summary_payload.atomic_fact_contract_v1` **共用同一纯函数**，不存在两套公式；
- `feature_snapshot_service.build_summary_payload` 在写入**新**快照时调用它填充 `summary_payload.atomic_fact_contract_v1`；旧已发布快照受 upsert `WHERE` 保护不覆盖，Context API 仍从 `structural_payload`/`temporal_payload` 重算（不回写 DB）；
- `compute_recent_changes(snapshots)`：接收 ≤10 个升序已发布快照，对 14 个 Core 事实逐对对比 `category`/`value` 变化，最多 30 条；只读计算，不写 `stock_state_events`。

Context API（`backend/app/api/stock_context.py`，复用既有路由，不新增平行接口）：
- `GET /api/v1/stocks/{symbol}/context` → `AtomicFactsContextResponse`（contractVersion / asOf / core / auxiliary / availability / recentChanges / dataQuality），普通用户只读；
- `GET /api/v1/admin/stocks/{symbol}/debug` → `AdminStockDebugResponse`（在用户响应基础上补充 `rawDebug` + `atomicFactsDebug`，含 Fact ID / 真实路径 / raw value / 阈值来源 / feature flag）；
- **GET 请求零数据库写入**（仅 `SELECT`）；`as_of` 严格 point-in-time：仅查 `succeeded + published + full` run，禁止返回未来快照或未来变化（`recent_changes` 按 `as_of` 过滤 ≤ 该日期的已发布快照）。

## 6. 前端实现

`frontend/src/features/research-context/AtomicFactsPanel.tsx`（替换已删除的 `EventStatePanel.tsx`）：

- `variant="compact"`（`/market` 右栏）：`MarketRightPanel` 组合 `MiniKlineCard`（顶部）+ `AtomicFactsPanel compact`（底部），按固定四组顺序展示，分母 14，面板内滚动不压缩小 K 线；
- `variant="expanded"`（`/stock/:symbol` 详情右面板）：概览（四组 Core）+ 近期变化 + 默认收起「更多信息」（Auxiliary 默认隐藏）；
- 复用 `useStockContext`（`GET /api/v1/stocks/{symbol}/context`）单一 query key `['stock-context', symbol, params]`；`/market` 与 `/stock/:symbol` 共用同一 query key 去重；
- 面板收起（右栏 `eventPanelCollapsed` / `rightPanelCollapsed`）时父组件不挂载本组件，`useStockContext` 不发起请求（Context 请求为 0）；
- 小 K 线周期切换（15m/60m/1d/1w/1mo）只改图表显示周期，**不改变日线 Atomic Facts**（Facts 始终来自 1d payload）；
- 通俗中文；禁用词由合同 `global_display_rules.forbidden_words` 约束；
- admin debug 页（`AdminStockDebugPage`）消费 `atomicFactsDebug` / `recentChanges`，展示 Fact ID、raw value、真实路径、阈值来源。

## 7. 测试覆盖

| 测试 | 类型 | 覆盖 |
|---|---|---|
| `backend/tests/test_atomic_fact_contract_service.py` | 纯函数 | Registry 14/10/1、顺序、ID 唯一、V1 永久缺席、T3/T6 默认隐藏、T2/M2/M3 真实值、T5/V3 阈值未启用、M5 三态+双 true 异常、S3 边界、S7/S8 非负距离、单公式 fallback、compact 完整 14 含 S2、无禁用词、无旧 MACD/SQZMOM 状态/布林位置、recentChanges 无未来、schema 装配 |
| `backend/tests/test_stock_context_atomic_facts.py` | API/集成（测试库 `bz_stock_test`） | context 空态、有快照返回 Core+Aux、GET 零写入、as_of 无未来、summary 持久化+旧快照 fallback（单公式）、admin debug 权限（member 403）+ 字段可追溯 |
| `frontend/src/features/research-context/__tests__/atomic-facts.test.ts` | 契约 | Registry 14/10/1、顺序、ID 唯一、V1 缺席、T3/T6 隐藏、前端类型 |
| `frontend/src/features/market-workspace/__tests__/change010Contract.test.ts` | 契约回归 | MarketRightPanel 组合 MiniKlineCard + AtomicFactsPanel |

## 8. Known Gap / 未确认项

- **THR-001**：T5 slope_ratio、V3 avg_volume_ratio、M3 zero tolerance 阈值需从 V4.12 q20/q80、q25/q75、q33/q67 候选确认；当前 UI 仅显示比值 + 「分类未启用」。
- **EFF-001 / EFF-002**：T3 趋势效率、T6 效率差存在工程 bug（效率 >1、net 用 DSA 线值而非收盘价），`feature_flag` 默认关闭，未进入普通用户 UI。
- **V1-REJ**：累计成交量比与段年龄比高度相关（Pearson=0.6405），已被 V3 段均量比替代（Pearson=-0.0149），仅保留 DB 调试值。
- 近期变化（recentChanges）非 V4.13 Core Fact，仅作相邻快照对比展示。
