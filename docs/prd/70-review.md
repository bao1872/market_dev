# 复盘模块 PRD V1.0

状态：已确认
最后确认日期：2026-08-06
对应 Map：`../maps/70-review.md`
需求所有权：复盘模块完整产品与工程合同（目标行为、数据契约、API、筛选器、归因、追踪、编排）

> 本文件是 Review 指标、历史、横截面、归因、信号、发布和页面状态的唯一需求真源。[`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只定义 Review 与上游节点之间的依赖、lineage 和闭环场景，不替代本文件的 P/Q/U/C/V 公式及门禁。

> 本文件是复盘模块的权威产品与工程合同。实现时不得根据页面方便性重新发明业务逻辑；前端不计算聚合变量、筛选器或归因结论。

## 0. 领域输入

Review 依赖以下正式领域输入：

- 个股第一金字塔：趋势、结构、动量为必选维度，筹码共识为可选维度；
- 至少满足滚动窗口要求的第一金字塔历史状态和日线事实；
- 行业/概念板块的趋势、结构、动量、量能、事件分布和 PIT membership；
- 个股 core 与板块聚合的正式 publication lineage；
- 行情列表、个股详情和板块详情的稳定导航身份。

Review 业务链为：

第一金字塔与历史状态
→ 板块第二金字塔
→ P/Q/U/C/V聚合变量
→ 三类偏差筛选器
→ 两级范围扫描
→ 板块归因
→ 个股验证
→ 用户追踪
→ 次日状态复核与历史反馈

## 1. 产品目标与边界

### 1.1 页面目标

复盘页必须让用户完成以下固定流程：

- 找到今天哪里发生异常；
- 查看命中的偏差类型和证据；
- 下钻到行业、概念和成员股票，解释异常来源；
- 用个股第一金字塔验证代表股票；
- 将信号、板块或股票加入追踪；
- 查看过去信号今天是确认、持续、减弱、失效还是转化。

### 1.2 不做的内容

- 不预测明日涨跌；
- 不生成买卖建议；
- 不做黑箱"机会分""风险分"或板块综合总分；
- 不在前端重算P/Q/U/C/V、筛选条件或归因；
- 不把全部99个第一金字塔字段塞入复盘页；
- 不把通用板块排行榜当作复盘主流程；
- 不以自然语言总结替代结构化证据；
- 不建立第二套K线或行情筛选器。

## 2. 权威业务链

```text
A. 已发布 stock_core pointer
B. 已发布 board_analysis / market_aggregation pointer
C. 第一金字塔历史基线（默认120个交易日，最低60日）
        ↓
市场/指数/风格/一级行业范围聚合
        ↓
生成每个范围的 P/Q/U/C/V 当前值、变化与历史分位
        ↓
运行三类偏差筛选器
        ↓
对命中范围进行第二级下钻
        ↓
生成板块与成员归因
        ↓
生成个股与板块关系
        ↓
发布 Review Run
        ↓
前端五阶段工作台
        ↓
用户保存追踪
        ↓
下一交易日重新计算追踪状态
```

## 3. 页面路由、权限与URL状态

### 3.1 路由

主路由：

```
/review
```

URL参数：

```
/review?date=2026-07-29
       &stage=signals
       &scopeType=industry_l1
       &scopeKey=electronics
       &signalId=<uuid>
       &boardId=<uuid>
       &symbol=000021
       &trackingTab=history
```

规则：

- URL是页面状态的唯一可分享入口；
- 首次加载先解析URL，再写入组件状态，禁止 hydration 后被默认值覆盖；
- 切换阶段、信号、板块、股票时更新URL；
- 浏览器前进/后退必须正确恢复状态；
- URL不得携带算法内部阈值或大段JSON。

### 3.2 权限

沿用"复盘管理独立授权"：

- review:read：读取已发布复盘；
- review:track：新增、修改、关闭自己的追踪；
- review:admin：重算、canary、发布、查看partial与错误；

普通用户不得看到未发布run；管理员可通过显式include_partial=true查看partial结果。

## 4. 后端模块结构

建议新增目录：

```
backend/app/domain/review/
  metric_registry.py
  metric_engine.py
  filter_definitions.py
  filter_engine.py
  attribution_engine.py
  tracking_state_machine.py

backend/app/services/
  review_orchestrator.py
  review_scope_service.py
  review_signal_service.py
  review_attribution_service.py
  review_tracking_service.py
  review_publication_service.py

backend/app/api/
  review.py
  admin_review.py

backend/app/schemas/
  review.py

backend/scripts/
  review_compute_cli.py
```

禁止把所有计算、SQL、筛选器和API塞入一个review_service.py。

## 5. 数据模型

若迁移074、075已经应用，使用下一可用前向迁移：

```
076_market_review_workbench.py
```

不得修改已应用的074（board_analysis_v1）与 075（market_data_quality）。

### 5.1 market_review_runs

表示某交易日完整复盘版本。

```
id UUID PK
trade_date DATE NOT NULL
source_core_run_id UUID NOT NULL
source_board_run_id UUID NOT NULL
algorithm_version VARCHAR NOT NULL
filter_version VARCHAR NOT NULL
baseline_window INTEGER NOT NULL DEFAULT 120
status VARCHAR NOT NULL
expected_scope_count INTEGER NOT NULL
succeeded_scope_count INTEGER NOT NULL
failed_scope_count INTEGER NOT NULL
signal_count INTEGER NOT NULL DEFAULT 0
coverage_ratio NUMERIC NOT NULL
started_at TIMESTAMPTZ
completed_at TIMESTAMPTZ
published_at TIMESTAMPTZ
metadata_json JSONB NOT NULL DEFAULT '{}'
created_at / updated_at
```

唯一约束：

```
trade_date + source_core_run_id + source_board_run_id + algorithm_version + filter_version
```

状态：

```
created / computing / partial / signals_ready / published /
completed_with_errors / failed / cancelled
```

### 5.2 market_review_run_items

按范围和阶段做检查点。

```
review_run_id
scope_type
scope_key
phase: metrics / signals / attribution / tracking
status: pending / running / succeeded / failed / skipped
attempt_count
input_hash
lease_epoch
lease_expires_at
last_error
started_at / completed_at
```

唯一约束：

```
review_run_id + scope_type + scope_key + phase
```

### 5.3 market_review_scope_snapshots

保存每个市场范围的P/Q/U/C/V和证据。

```
id UUID PK
review_run_id UUID FK
trade_date DATE
scope_type VARCHAR
scope_key VARCHAR
scope_name VARCHAR
parent_scope_type VARCHAR NULL
parent_scope_key VARCHAR NULL
source_board_snapshot_id UUID NULL
eligible_count INTEGER
ready_count INTEGER
coverage_ratio NUMERIC
status VARCHAR
p_payload JSONB
q_payload JSONB
u_payload JSONB
c_payload JSONB
v_payload JSONB
data_quality_json JSONB
created_at / updated_at
```

唯一约束：

```
review_run_id + scope_type + scope_key
```

### 5.4 market_review_signals

保存三类筛选器命中结果。

```
id UUID PK
review_run_id UUID FK
trade_date DATE
filter_family VARCHAR     # A/B/C
signal_type VARCHAR
scope_type VARCHAR
scope_key VARCHAR
scope_name VARCHAR
status VARCHAR            # new/continuing/confirmed/weakened/invalidated/transformed
first_seen_date DATE
previous_signal_id UUID NULL
transformed_to_signal_id UUID NULL
trigger_payload JSONB
baseline_payload JSONB
evidence_payload JSONB
confirmation_rule JSONB
invalidation_rule JSONB
coverage_ratio NUMERIC
rank_key JSONB
created_at / updated_at
```

唯一约束：

```
review_run_id + filter_family + signal_type + scope_type + scope_key
```

### 5.5 market_review_signal_attributions

保存第二级范围下钻结果。

```
id UUID PK
signal_id UUID FK
child_scope_type VARCHAR
child_scope_key VARCHAR
child_scope_name VARCHAR
relation_type VARCHAR
contribution_value NUMERIC
contribution_rank INTEGER
metrics_payload JSONB
evidence_payload JSONB
coverage_ratio NUMERIC
created_at
```

### 5.6 market_review_signal_instruments

保存代表股票和其对信号的贡献。

```
id UUID PK
signal_id UUID FK
instrument_id UUID FK
symbol VARCHAR
name VARCHAR
board_role VARCHAR
relation_to_scope VARCHAR
contribution_value NUMERIC
contribution_rank INTEGER
first_pyramid_payload JSONB
fresh_events_payload JSONB
source_snapshot_id UUID
created_at
```

board_role只允许：

```
core / second_line / elasticity / follower / laggard / unclassified
```

relation_to_scope只允许：

```
synchronized_strengthening
synchronized_weakening
instrument_leads_scope
scope_strong_instrument_lags
instrument_strong_scope_unsupported
unconfirmed
```

### 5.7 market_review_trackings

保存用户追踪。

```
id UUID PK
user_id UUID
source_signal_id UUID
tracking_type VARCHAR     # signal / scope / instrument
scope_type/scope_key NULL
instrument_id NULL
status VARCHAR            # active / confirmed / invalidated / closed
confirmation_conditions JSONB
invalidation_conditions JSONB
note TEXT NULL
created_at / closed_at
```

### 5.8 market_review_tracking_evaluations

保存逐日追踪结果。

```
tracking_id UUID
review_run_id UUID
trade_date DATE
previous_state VARCHAR
current_state VARCHAR
evaluation_payload JSONB
created_at
```

唯一约束：

```
tracking_id + trade_date
```

## 6. 范围定义与两级扫描

### 6.1 第一级扫描范围

固定只扫描：

```
market
major_index
style
industry_l1
```

范围来源：

- market：全部有效A股；
- major_index：复用现有指数成分服务；
- style：使用已有、版本化的风格股票池定义；
- industry_l1：复用现有行业板块成员关系。

禁止在第一级直接扫描全部概念和全部股票。

### 6.2 第二级下钻范围

仅对命中信号的父范围扫描：

```
industry_l2
industry_l3
concept
instrument
```

下钻必须保留父子路径：

```
market → style/index/industry_l1
→ industry_l2/l3 or related concept
→ instrument
```

概念范围只有满足以下条件才参与：

- 与命中父范围存在成员交集；
- ready_count达到最小样本数；
- coverage达到门禁；
- 不允许为展示而扫描所有无关概念。

### 6.3 Historical Membership Source Contract（Phase 4A3 新增）

本小节定义 `major_index` / `style` / `industry_l1` 历史 PIT membership 的数据来源合同。
Phase 4A3（2026-08-09）完成 source selection 与授权分析；当前 schema
（`BoardDefinitionVersion` + `BoardMembershipHistory`，`UniverseDefinition` + `UniverseMembership`，
均由 migration `079_board_hierarchy_batch_identity` 建立）**已完全支持 PIT 半开区间**
`[effective_from, effective_to)`，无需新增表或 migration。缺口仅为**来源数据填充**。

#### 6.3.1 硬规则（禁止项）

- **PIT 唯一性**：历史日期 T 只能使用 T 日 `effective` 的 membership；禁止 `latest` / `current` 回填历史区间。
- **禁止手工伪造**：无来源数据时必须输出 `bootstrap_unavailable`，不得构造 member 列表。
- **禁止静默 fallback**：`blocked_external_population` / `unpopulated` 状态下不得假装 `ready`。
- **来源授权前置**：新增 external provider（含 tushare / akshare / 交易所爬取 / 第三方 API）
  必须先修改本 PRD 获得授权，禁止在代码中私自接入（`rules/90 §11`、`rules/20 §180-184`）。
- **来源 lineage**：每条 membership 必须记录 `source` + `source_key` + `taxonomy_version` /
  `membership_version`，与 `taxonomy_compatibility_key` 保持一致。
- **历史修正**：source 后续修正时，新 version 不得覆盖已被 Stage B observation 引用的
  `taxonomy_compatibility_key`；如需变更必须 bump version 并标记 lineage。
- **Tushare 永久禁止**：Tushare **永远不是允许的数据源**；不得推荐、接入或依赖 Tushare
  作为任何 Review / 板块 / 指数 / 风格成员来源。
- **申万行业来源**：若需申万行业分类，优先扩展现有 `pywencai` 问句（使用“申万行业”相关关键词）
  获取；此扩展**不代表允许 current membership 回填历史**——历史 PIT 缺口仍须 `bootstrap_unavailable`。

#### 6.3.2 各 family 来源状态（Phase 4A3 结论）

| family | 当前状态 | 授权来源 | 历史窗口 2026-02-06→2026-08-07 可用性 |
|---|---|---|---|
| industry_l1 | 官方来源 = `pywencai`（wencai），**current-only**；`BoardMembershipHistory` 仅在两次 sync 间发生变更时才产生新版本，首 sync 之前无历史快照 | `wencai`（rules/90 唯一授权） | **SOURCE_NOT_AVAILABLE**（授权来源无法提供历史窗口；见 §6.3.3） |
| csi300 | migration `079` 已建 placeholder `UniverseDefinition`（key=`csi300`，`population_status=blocked_external_population`，`source=authoritative-provider-required`），无已填充 connector | 待授权（候选见 §6.3.4） | **SOURCE_SELECTION_REQUIRED**（候选存在但未授权） |
| csi500 | 同上（key=`csi500`） | 待授权（候选见 §6.3.4） | **SOURCE_SELECTION_REQUIRED** |
| style（large_cap / small_cap） | migration `079` 已建 placeholder（`large_cap_style` / `small_cap_style`，`blocked_external_population`），PRD §6.1 称“使用已有、版本化的风格股票池定义”，但**项目内无任何 origin / 构造规则** | 无 | **STYLE_PRODUCT_DECISION_REQUIRED**（产品定义缺口，非 ingestion bug） |

#### 6.3.3 industry_l1 决策

- `pywencai` 是 rules/90 唯一授权的板块来源，固定查询 `"同花顺概念，行业分类"`，
  返回**单一当前快照**，无 `as_of` / `trade_date` / 历史参数 → 确认 current-only。
- 因此对于 Review MVP 所需的 `2026-02-06 → 2026-08-07` 历史 PIT 窗口，
  授权来源**无法提供** → 结论 **SOURCE_NOT_AVAILABLE**。
- 可选出路（需用户决策，不在此擅自选择）：
  1. MVP 接受 industry_l1 **仅 forward-only**（自 2026-08-09 起每日 snapshot），历史窗口标记为 `bootstrap_unavailable`；
  2. 授权一个具备历史行业分类的替代来源（需先改本 PRD）。

#### 6.3.4 csi300 / csi500 候选矩阵（需授权后选用）

候选按 §10 优先级（现有已授权 > 官方 > 新依赖）排序：

1. **CSIndex 官方 constituent 下载**（www.csindex.com.cn）：提供成分股及 effective date，
   PIT 语义最干净；官方一级来源，优先推荐。
2. **交易所官网（SSE/SZSE）成分公告**：官方但需解析公告，cadence 不规整。
3. **Tushare `index_member` / `index_weight`**（需 token，项目当前未依赖）：
   具备 `start_date`/`end_date` PIT 查询，但属新增 external dependency，需 PRD 授权。

当前均**未授权**，故 csi300/csi500 = **SOURCE_SELECTION_REQUIRED**。

#### 6.3.5 style 产品决策缺口（OPEN DECISION）

- PRD §6.1 仅声明“使用已有、版本化的风格股票池定义”，未定义：
  - large_cap / small_cap 的**来源**（官方风格指数？自定义池？）；
  - 还是**运行时按市值排名构造**（top N / 分位）。
- 禁止自行发明 `top300=large` / `top20%=small` / market-cap threshold（§8）。
- 此为 **PRODUCT_DEFINITION_GAP**，必须由用户决定产品定义后，才能确定 source 或构造规则。
- PRD 此处标记为 **OPEN DECISION**，不补规则。

#### 6.3.6 最小 normalized shape（与现有表对齐）

INDUSTRY（→ `BoardMembershipHistory`）：

```
instrument_id, board_id, taxonomy_version, membership_version,
effective_from, effective_to, source, source_key
```

INDEX / STYLE（→ `UniverseMembership`）：

```
universe_definition_id, instrument_id, effective_from, effective_to,
weight, source, source_key
```

Idempotency 以现有 unique constraint 为准：
`uq_board_membership_history_identity`（board_id, instrument_id, effective_from）；
`uq_universe_memberships_identity`（universe_definition_id, instrument_id, effective_from）。

#### 6.3.7 Required historical coverage

第一阶段只要求完整覆盖 `2026-02-06 → 2026-08-07`（当前 120 交易日 baseline）。
不要求 10 年历史，除非来源免费自然提供且不显著增加复杂度。

#### 6.3.8 Review MVP 发布就绪门禁（Phase 4C 校正）

原 §11.1 发布门禁将 `major_index` / `style` / `industry_l1` readiness 作为 whole-Review
硬性阻塞项。Phase 4C（2026-08-09）按产品决策校正为 **渐进式 scope readiness**：

- **MARKET = Review MVP 强制历史基线（HARD GATE）**：
  - 必须存在、状态 `ready`、满足现有 market coverage/quality 要求（含 P/Q/U/C/V 五项
    `normalized_ready`）；
  - 不满足 → 发布门禁 CLOSED。
- **industry_l1 / major_index / style = 渐进式可选 scope（OPTIONAL）**：
  - 真实就绪 → 正常参与产品输出；
  - `bootstrap_unavailable` / `insufficient_history` / `blocked_external_population` /
    PIT unavailable → 保留真实状态与诊断，但**不加入 whole-product blocker**；
  - 禁止把 optional scope 状态伪装成 `ready`；
  - 禁止 current membership × historical date / latest snapshot 回填 / forward-fill。
- **Concept = AUXILIARY_DEFERRED**；**BJ = DEFERRED**。
- 此校正**不改变指标公式**：仅调整 scope readiness / publication readiness 语义。
- 实现约束（`review_publication_service.evaluate_publish_gate`）：market 仍为唯一强制 scope；
  optional scope 不可用仅记为诊断，不阻塞整个 Market Review MVP 发布。

## 7. P/Q/U/C/V指标合同

### 7.1 通用结构

每个聚合变量返回：

```json
{
  "value": 63.4,
  "rawValue": 0.572,
  "delta1d": -4.1,
  "delta5d": 6.7,
  "historyPercentile120d": 78.2,
  "crossSectionPercentile": 84.0,
  "historyObservationCount": 120,
  "components": [],
  "coverage": 0.982,
  "status": "ready"
}
```

规范：

- value范围0—100；
- 默认按该范围自身120日历史分位归一化；
- 历史少于60日时status=insufficient_history，不得伪造分位；
- delta1d/delta5d使用归一化值变化；
- 每个component必须保留原始值、方向、分母、字段来源和权重；
- 所有字段映射必须通过ReviewMetricComponentRegistry引用现有权威扁平字段，禁止在业务代码中散落JSON path。

聚合方式：

```
value = available component normalized values 的版本化加权平均
```

初始权重可全部为1，但必须在registry中显式配置并写入algorithm_version；不得隐藏在函数内部。

### 7.2 P：价格表现强度

初始components：

- scope_return_1d：优先官方指数/板块价格序列；无官方序列时使用成员等权中位数，并记录price_source=member_equal_weight；
- advance_ratio = change_pct > 0 的成员数 / ready_count；
- trend_price_alignment_ratio = 趋势向上且当日上涨成员数 / ready_count；
- new_high_ratio：进入可配置近期高位区间的成员比例；
- price_position_median：成员价格在自身滚动区间的位置中位数。

P只描述表面表现，不等价于内部质量。

### 7.3 Q：内部结构质量

初始components：

- 上行趋势成员比例；
- 主要结构向上比例；
- 短线结构向上比例；
- 趋势、结构、动量一致性比例；
- structure_net_event_rate = bullish结构事件率 - bearish结构事件率；
- 结构破坏扩散率，作为反向component。

结构事件必须使用已落库事件和新鲜度，不得按前端当前状态猜测。

### 7.4 U：参与范围

初始components：

- 至少两个核心维度同步改善的成员比例；
- 正动量或动量增强覆盖率；
- 新鲜结构事件覆盖率；
- 非头部成员参与比例；
- 龙头、二线与普通成员共同确认比例。

U表示宽度，不使用成交额权重替代成员参与。

### 7.5 C：集中程度

C越高表示越集中，不表示越好。

初始components：

- 绝对价格变化贡献Top5占比；
- 事件贡献Top10%成员占比；
- 成员绝对变化贡献HHI；
- 龙头与成员中位数表现差；
- 有可靠成交额数据时加入Top5成交额占比。

无官方权重时使用等权成员贡献并明确weight_mode=equal，不得伪装为官方指数贡献。

### 7.6 V：成交活跃与效率

初始components：

- 放量成员比例；
- 成交额扩张成员比例；
- 成员20日成交量分位中位数；
- 成员200日成交额分位中位数；
- 趋势段平均量相对前段改善比例；
- 价格变化/相对成交额的效率中位数。

所有除法使用明确epsilon并过滤异常值。

## 8. 三类筛选器

筛选器必须由版本化配置驱动，建议：

```
backend/config/review_filters.yaml
```

并使用Pydantic schema校验。不得把阈值散落在多个service。

初始工程默认值仅用于形成可运行基线，上线前必须用历史回放校准；配置变化必须升级filter_version。

### 8.1 A类：表面表现与内部质量偏差

**A1 surface_strong_internal_weak**

初始条件：

```
P.historyPercentile120d >= 70
(P.value - Q.value) 的自身历史分位 >= 90
Q.delta1d <= 0 或 U.delta1d <= 0
coverage >= 0.95
```

**A2 surface_weak_internal_improving**

```
P.historyPercentile120d <= 40
Q.delta1d 的历史分位 >= 70
U.delta1d 的历史分位 >= 60
coverage >= 0.95
```

### 8.2 B类：当前状态与变化速度偏差

**B1 high_level_slowing**

```
P/Q/U/V中至少2项历史分位>=70
Q/U/V中至少2项1日变化分位<=30
```

**B2 low_level_repair**

```
P/Q/U中至少2项历史分位<=40
Q与U的1日变化分位>=70
结构破坏扩散率不再继续上升
```

### 8.3 C类：成交、参与与集中度偏差

**C1 volume_without_breadth**

```
V历史分位>=70或V变化分位>=70
U变化分位<=40
C历史分位>=70或C继续上升
```

**C2 breadth_without_volume**

```
U变化分位>=70
V历史分位<=50或V变化分位<=50
```

**C3 synchronized_expansion**

```
U变化分位>=70
V变化分位>=70
C未处于异常高位或未继续上升
```

### 8.4 D类：第二金字塔维度偏差

> [P0-7 2026-07-30] 新增 D 族筛选器，对应 PRD §24 第二金字塔 6 维度。
> D 族只在 industry/concept scope 评估（需 pyramid_v2 数据）；
> market/major_index/style scope 无 board_analysis，D 族不命中。

**D1 state_migration_positive**

```
positive_migration_count >= 5
positive_ratio >= 0.6
negative_migration_count <= positive_migration_count
```

**D2 event_freshness_high**

```
decay_weighted_density >= 0.3
today_count >= 1 或 last_5d_count >= 3
```

**D3 breadth_expansion**

```
participation_coverage >= 0.3
total_migration_count >= 5
```

**D4 concentration_high**

```
hhi >= 0.1 或 top5_contribution >= 0.4
leader_median_gap > 0
```

**D5 relative_strength_strong**

```
vs_market.ratio >= 1.1
equal_weight_diff > 0
```

### 8.5 信号排序

不生成综合黑箱分。排序键依次为：

- 偏差历史分位；
- 当日变化分位；
- 持续日数；
- coverage；
- scope_type固定优先级；
- scope_name稳定第二键。

rank_key必须把上述分项保存下来。

## 9. 板块归因逻辑

筛选器只负责发现，归因负责解释。

### 9.1 子范围贡献

对每个命中信号：

- 找到父范围的直接子范围和关联概念；
- 计算子范围对父范围P/Q/U/C/V变化的贡献；
- 保留正贡献和负贡献；
- 按绝对贡献排序；
- 保存前N项，但API支持分页读取全部。

归因不得仅按涨幅排序。

### 9.2 个股贡献

每只成员计算：

- 对P的表面变化贡献；
- 对Q的趋势/结构/动量贡献；
- 对U的参与确认；
- 对C的集中度贡献；
- 对V的成交贡献；
- 新鲜结构/动量事件；
- 与板块状态的关系。

角色分类与因子状态分开保存。角色可使用相对贡献和历史稳定性生成，但必须保留role_evidence。

## 10. 信号生命周期与追踪状态机

### 10.1 系统信号

```
new
→ continuing
→ confirmed
→ weakened
→ invalidated
→ transformed
```

规则：

- 同一scope同一signal_type连续命中：continuing；
- 达到filter配置中的确认条件：confirmed；
- 偏差减弱但尚未失效：weakened；
- 达到失效条件：invalidated；
- 转为另一信号类型：旧信号transformed并关联新信号。

禁止前端根据颜色自行判断状态。

### 10.2 用户追踪

用户可以追踪：

- 一条信号；
- 一个命中范围；
- 一只代表股票。

每天Review Run完成后自动生成evaluation。用户关闭追踪不删除历史。

## 11. 任务编排与发布

盘后顺序：

```
stock_core published
→ board_analysis published
→ create market_review_run
→ compute level-1 scope metrics
→ evaluate filters
→ compute level-2 attribution for matched signals
→ map representative instruments
→ evaluate active trackings
→ quality gate
→ publish review pointer
```

要求：

- 每个scope独立item、短事务、可恢复；
- 一个scope失败不回滚其他scope；
- 重启只处理pending/可重试failed/过期running；
- 相同输入hash和版本的succeeded item不得重算；
- 信号和归因幂等；
- pointer切换失败只重试发布，不重算；
- 依赖按矩阵解析：`stock_core` 是必需依赖，板块依赖缺失时允许明确的 `core_only` 降级；run 元数据必须记录每项来源 pointer/run、解析方式与降级原因；
- 指标同时保留 raw 与 normalized；历史读取必须满足 `observation.trade_date < run.trade_date`，并按算法版本、scope 类型、scope key 及兼容版本隔离，禁止未来数据和跨范围污染；
- run coverage 表示底层有效样本覆盖率，不能用成功 scope 数比例冒充；
- quality gate、Review pointer upsert 与 run published 状态在同一调用方事务内完成；任一步失败必须回滚并保留旧 pointer。

### 11.1 发布门禁

单scope：

- underlying coverage >= 0.95
- P/Q/U/C/V必要组件状态可用

整套Review：

- market范围必须ready；
- 配置的主要指数和风格范围必须ready；
- 一级行业ready比例达到配置门槛；
- signal evaluation无系统性异常；
- source_core_run_id和source_board_run_id均指向当前正式pointer。

## 12. API合同

统一前缀：

```
/api/v1/review
```

### 12.1 日期与总览

```
GET /api/v1/review/dates
GET /api/v1/review/latest
GET /api/v1/review/{trade_date}/overview
```

overview返回：

```json
{
  "reviewRunId": "uuid",
  "tradeDate": "2026-07-29",
  "status": "published",
  "sourceCoreRunId": "uuid",
  "sourceBoardRunId": "uuid",
  "algorithmVersion": "review-1.0.0",
  "filterVersion": "filters-1.0.0",
  "baselineWindow": 120,
  "coverage": {
    "market": 1.0,
    "indices": 1.0,
    "styles": 1.0,
    "industryL1": 0.98
  },
  "signalSummary": {
    "new": 5,
    "continuing": 3,
    "confirmed": 2,
    "weakened": 1,
    "invalidated": 2
  }
}
```

### 12.2 市场扫描

```
GET /api/v1/review/{trade_date}/scopes
```

参数：

- scope_type
- parent_scope_type
- parent_scope_key
- sort
- page
- page_size
- include_partial=false

返回每个范围的P/Q/U/C/V、变化、历史分位和命中数量。

### 12.3 信号

```
GET /api/v1/review/{trade_date}/signals
GET /api/v1/review/signals/{signal_id}
```

筛选参数：

- filter_family
- signal_type
- status
- scope_type
- scope_key
- page/page_size

### 12.4 归因与个股

```
GET /api/v1/review/signals/{signal_id}/attributions
GET /api/v1/review/signals/{signal_id}/instruments
```

股票接口支持：

- role
- relation
- sort
- page/page_size

### 12.5 追踪

```
GET    /api/v1/review/trackings
POST   /api/v1/review/trackings
PATCH  /api/v1/review/trackings/{id}
DELETE /api/v1/review/trackings/{id}   # 实际为关闭，不物理删除
GET    /api/v1/review/trackings/{id}/evaluations
```

### 12.6 管理端

```
POST /api/v1/admin/review/runs
POST /api/v1/admin/review/runs/{id}/resume
POST /api/v1/admin/review/runs/{id}/publish
GET  /api/v1/admin/review/runs/{id}/status
```

所有写操作要求幂等键。

## 13. 前端目录与组件

建议目录：

```
frontend/src/features/review/
  api.ts
  types.ts
  queryKeys.ts
  urlState.ts
  ReviewHeader.tsx
  ReviewStageNav.tsx
  MarketScanPanel.tsx
  FilterDiscoveryPanel.tsx
  BoardAttributionPanel.tsx
  StockValidationPanel.tsx
  TrackingReviewPanel.tsx
  EvidenceDrawer.tsx
  ScopeMetricsTable.tsx
  SignalCard.tsx
  AttributionTable.tsx
  ReviewInstrumentTable.tsx
  ReviewDataQualityBadge.tsx
  review.module.scss

frontend/src/pages/ReviewPage.tsx
```

现有BoardAnalysisPage.tsx不删除。应抽取可复用的：

- BoardMetricsSummary
- BoardDistributionPanel
- BoardEventDistribution

供板块分析页和复盘归因阶段共同使用，禁止复制两套计算和展示逻辑。

## 14. 页面信息架构

### 14.1 固定顶部

展示：

- 交易日与前后交易日；
- Review发布状态；
- Core/Board Run；
- 股票与板块覆盖率；
- 算法版本、筛选器版本、历史基线；
- 数据质量入口。

顶部不得显示AI自由生成的市场结论。

### 14.2 五阶段导航

1. 市场扫描
2. 筛选发现
3. 板块归因
4. 个股验证
5. 追踪复核

阶段共享同一上下文。顶部面包屑显示：

```
全市场 > 科技风格 > 电子 > 光模块 > 000021
```

### 14.3 阶段一：市场扫描

主表字段：

- 范围名称
- 范围类型
- P/Q/U/C/V当前值
- 1日变化
- 120日分位
- 命中数量
- coverage
- 数据状态

每个变量单元格显示：

- 值 + 方向箭头 + 历史分位细条

不使用雷达图。

点击一行：

- 更新URL scope；
- 进入该范围信号列表；
- 不直接跳转个股。

### 14.4 阶段二：筛选发现

固定三组：

- A 表面/质量偏差
- B 状态/速度偏差
- C 成交/参与偏差

一张SignalCard必须显示：

- 范围；
- 信号类型；
- 生命周期状态；
- 首次出现日期和持续日数；
- 触发变量；
- 历史分位；
- coverage；
- 结构化解释；
- 查看归因、查看历史、加入追踪。

不显示黑箱总分。

### 14.5 阶段三：板块归因

页面分四块：

- 信号证据链；
- 第二金字塔：趋势、结构、动量、内部分布；
- 子范围贡献表；
- 代表股票预览。

归因说明由模板根据结构化字段生成，例如：

```
P保持高位 → Q下降 → U收缩 → C上升
```

禁止调用大模型自由编写结论作为唯一依据。

### 14.6 阶段四：个股验证

精简表字段：

- 股票
- 板块角色
- 与板块关系
- 趋势
- 主要结构
- 短线结构
- 动量
- 量能
- 新鲜事件
- 贡献
- 自选 +/-

操作：

- 打开/stock/:symbol；
- 加入/移除自选；
- 加入本信号追踪；
- "查看全部"跳转/market并传递标准筛选参数。

复盘页不得重新实现99字段列设置和导出。

### 14.7 阶段五：追踪复核

内部三个子Tab：

- 过去发现
- 自选映射
- 事件演化

"过去发现"字段：

- 首次日期
- 信号
- 范围
- 当前状态
- 连续天数
- 状态变化
- 后续证据

"自选映射"回答：

- 自选股属于哪些今日命中范围；
- 个股与板块同步还是背离；
- 今日新增结构/动量事件；
- 是否进入确认或失效条件。

### 14.8 证据抽屉

右侧统一EvidenceDrawer，由任何指标、信号、归因或股票打开。

内容：

- 定义
- 当前值/昨日值/5日变化
- 120日历史分位
- 分母与coverage
- components
- 底层字段来源
- 贡献板块/股票
- 缺失原因
- source run与算法版本

主页面保持简洁，但所有结论可追溯。

## 15. 前端数据与状态规则

- 使用React Query；
- query key必须包含reviewRunId/tradeDate/resource/id/filters；
- 已发布历史复盘使用较长staleTime，不每30秒刷新；
- 最新交易日处于computing时仅轮询run status，发布后停止；
- 页面组件不得拼接不同Review Run；
- 切换signal时取消无效请求；
- 后端返回partial/stale/unavailable时必须显示具体状态；
- 禁止无限"加载中"；请求超时、404、422、500分别显示明确错误和request_id。

## 16. 与现有页面的边界

### /market

负责：全字段筛选、排序、列设置、导出、自选管理。

Review跳转参数：

- reviewSignalId
- tradeDate
- sourceCoreRunId
- boardId
- firstPyramidFilters
- sort

### /stock/:symbol

负责：K线、第一金字塔完整详情、事件和筹码状态。

Review只传：

- from=review
- signalId
- boardId
- tradeDate

### /boards/analysis

保留为板块原始分析和管理/研究入口；Review阶段三复用其组件，不复制业务。

## 17. 加载、空态和异常态

必须覆盖：

- 当日Review尚未计算；
- 计算中；
- partial未发布；
- 已发布但无信号；
- scope coverage不足；
- 历史不足无法计算分位；
- signal无可归因子范围；
- 个股无第一金字塔；
- 用户无复盘权限；
- API超时或版本不一致。

"无信号"应显示"今日未命中已配置偏差筛选器"，不能显示"暂无数据"。

## 18. 性能与缓存

- 页面首屏只加载overview、一级scope摘要和信号摘要；
- 归因和股票列表按需加载；
- 所有长列表服务端分页；
- 不一次返回几千只成员；
- Review计算读取已发布快照，禁止逐只重新计算第一金字塔；
- 120日分位应批量计算或预聚合，禁止N+1；
- Redis只缓存已发布、不可变的Review响应，cache key包含review_run_id；
- pointer切换后旧缓存自然隔离，不做全局flush。

## 19. 测试要求

### 19.1 后端单元测试

- component registry映射；
- P/Q/U/C/V计算；
- 历史分位不足；
- A/B/C各筛选器正反例；
- signal生命周期；
- attribution排序；
- tracking状态机；
- 模板化解释。

### 19.2 PostgreSQL集成测试

不得skip：

- migration upgrade/downgrade/upgrade；
- run/item并发claim；
- 相同输入幂等；
- signal唯一约束；
- pointer不混run；
- published与partial隔离；
- attribution和instrument分页；
- tracking evaluation逐日唯一；
- 用户权限隔离。

### 19.3 前端目标测试

- URL hydration与前进/后退；
- 五阶段切换；
- MarketScan表排序；
- SignalCard证据和状态；
- 归因下钻；
- 个股跳转参数；
- 加入追踪；
- 无信号、partial、历史不足、API错误；
- EvidenceDrawer字段来源。

### 19.4 生产canary

先固定：

- 全市场
- 2个主要指数
- 2个风格范围
- 5个一级行业

验证：

- P/Q/U/C/V值可复算；
- 至少一条正向和一条风险信号；
- 下钻路径和成员归因一致；
- /market与/stock跳转正确；
- 次日tracking状态可重复计算。

## 20. 验收标准

完整验收必须满足：

- 页面五阶段与后台业务链一一对应；
- 前端没有P/Q/U/C/V或筛选器计算代码；
- 同一页面不混合不同run；
- 三类筛选器均能给出结构化证据；
- 信号可下钻到子范围和股票；
- 个股第一金字塔与板块关系可解释；
- 信号可保存追踪并在下一交易日产生evaluation；
- 过去信号可显示确认/持续/减弱/失效/转化；
- coverage、历史不足和partial不被伪装成完成；
- 真实登录浏览器完成URL、页面、Console和Network验收。

## 21. 文档与记忆系统

必须更新：

- `docs/prd/70-review.md`（本文档）
- `docs/maps/70-review.md`（真实调用链、表、API、组件）
- `docs/prd/30-after-close.md`（Core→Board→Review编排）
- `docs/maps/30-after-close.md`（pointer和run关系）
- `docs/maps/40-market-stock-experience.md`（Review→Market→Stock跳转合同）
- `docs/runbooks/after-close-remote-development-run.md`（review canary/resume/publish）
- `rules/40-testing-quality.md`（TQ-97 页面验收三类证据、TQ-98 成功判定三要素）

保持：

- docs/current只读；
- 不创建reports；
- 不新增重复治理目录；
- AGENTS.md只保留入口，不扩写业务细节。

## 22. 推荐实施顺序

**Phase 0：输入门禁**

先确保第一金字塔、板块分析、行情完整性和发布pointer可靠。

**Phase 1：Review后端骨架**

迁移、模型、scope snapshot、P/Q/U/C/V、run/item、API overview/scopes。

**Phase 2：筛选器与归因**

A/B/C筛选器、signals、attributions、instrument mapping、发布门禁。

**Phase 3：五阶段前端**

ReviewPage、URL状态、市场扫描、筛选发现、板块归因、个股验证。

**Phase 4：追踪闭环**

tracking、daily evaluation、过去发现、自选映射、事件演化。

**Phase 5：历史回放与阈值校准**

使用历史Review Run验证筛选器稳定性；阈值变化升级filter_version，不覆盖旧信号。

## 23. P0 强化条款（review-1.1.0）

> 本章节为 review-1.1.0 算法版本（CHANGE-20260730-014）追加的强制条款，是对 §7（P/Q/U/C/V 指标合同）、§11（任务编排与发布）、§6（范围定义与两级扫描）的补强。本章节条款优先级高于历史 §7/§11 的所有冲突描述。

### 23.1 历史原始组件 bootstrap 合同

每个历史日的聚合组件必须按 point-in-time 语义重建，禁止使用未来成员或未来因子：

- **成员 point-in-time**：每个历史日必须使用当日有效成员关系（行业归属、概念归属、指数成分、风格池定义）；当日已退市或尚未上市的股票不得进入当日 eligible_count 与 ready_count。
- **因子 point-in-time**：每个历史日必须使用当日已发布的 `stock_core` 快照对应的扁平化 99 字段；禁止使用后续版本回填的因子覆盖历史日。
- **rawValue 先行**：每个历史日必须先写入组件 `rawValue`（原始值、方向、分母、字段来源、权重）；`normalizedValue`、`delta1d`、`delta5d`、`historyPercentile120d`、`crossSectionPercentile` 在未达到 60 个有效观测前必须为 `null`。
- **观测计数**：`historyObservationCount` 必须真实反映已保存的 rawValue 数量，不得用 eligible_count 或 ready_count 代替。
- **聚合顺序**：达到 60 个观测后才允许计算 `normalizedValue`、P/Q/U/C/V 的 `value`，以及 1d/5d 变化与历史分位；未达到门槛的组件必须以 `status=insufficient_history` 暴露，不得补 0、不得补均值、不得用前值填充。

### 23.2 至少 60 日才允许生成 P/Q/U/C/V

- 任意 P/Q/U/C/V 聚合变量的 `value` 与 `historyPercentile120d` 必须在累计达到 60 个有效历史观测后才能生成；不足 60 日时 `status=insufficient_history`，且 `value` / `normalizedValue` / `historyPercentile120d` / `delta1d` / `delta5d` 必须为 `null`。
- `status=insufficient_history` 不得伪造分位，不得使用样本外分位、不得使用 cross-section 分位替代历史分位。
- `delta1d` / `delta5d` 必须基于 `normalizedValue` 计算；`normalizedValue` 为 `null` 时 `delta*` 也必须为 `null`。
- 该合同同时适用于 market、major_index、style、industry_l1 第一级范围，以及 industry_l2 / industry_l3 / concept / instrument 第二级范围。

### 23.3 canary 不得切正式 market_review pointer

- canary review run 必须以 `scope=canary` 显式声明，且只能通过 admin 端 provisional 入口查看，不得写入 `factor_publications`（`publication_kind=market_review`）。
- canary run 的 `status` 可以为 `published`（仅表示 run 内部计算完成），但 `factor_publications` 表中不得存在对应 `data_run_id` 指针；普通用户 `/api/v1/review/*` 端点读取的 pointer 不得切到 canary run。
- canary run 的结果可由 admin 通过 `include_partial=true` 或显式 `run_id` 查看，但必须返回 `is_provisional=true` 标记，避免与正式发布结果混淆。
- 上一轮 canary run（`run_id=3e1db415-2266-4cc5-9453-d8561d799b43`，`trade_date=2026-07-29`，`force=True`，`signal_count=0`）保留为审计记录，不修改历史数据；该 run 已写入 `factor_publications`，后续 review-1.1.0 修复后必须通过新 run 切换 pointer，不得复用该 run 重发。

### 23.4 完整第一级范围合同

第一级扫描必须完整覆盖以下四类范围，缺一不可：

```
market
major_index
style
industry_l1
```

合同要求：

- **market**：全市场有效 A 股，必须使用当日 active 股票，`eligible_count` 不得小于 4500（A 股正常交易日）；
- **major_index**：必须覆盖配置的全部主要指数成分（不少于 2 个），每个指数成分来源以版本化服务为准；
- **style**：必须覆盖配置的全部风格池（不少于 2 个），不得只算部分风格；
- **industry_l1**：必须覆盖全部一级行业（不少于 25 个），不得只算 canary 子集。

`scope_key` 命名规范：

- `market`：`scope_key="market"`（固定）；
- `major_index`：`scope_key=<index_code>`（指数代码，不含空格）；
- `style`：`scope_key=<style_code>`（风格代码，不含空格）；
- `industry_l1`：`scope_key=<board_id>`（统一使用 `board_id`，禁止混用 `industry_name`、`industry_code`、`board_name`）。

`industry_l1` 的 `scope_key` 必须与 `board_analysis_snapshots.board_id` 对齐，便于 Review 阶段三归因直接 JOIN 板块分析结果，禁止出现 `scope_key=electronics` 与 `scope_key=<uuid>` 混用的情况。

### 23.5 禁止 force 发布不可用数据

`review_publication_service.publish_review(db, run, force=False)` 必须严格执行以下门禁；`force=True` 仅允许 admin 在内部调试时使用，且不得写入 `factor_publications`：

整套 Review 发布门禁（force=False 时强制校验）：

1. **market P/Q/U/C/V value 非空**：market 范围的 P / Q / U / C / V 五项 `value` 必须全部非 `null` 且 `status=ready`；任一为 `null` 或 `status=insufficient_history` 拒绝发布。
2. **source_board_run_id 一致**：`market_review_runs.source_board_run_id` 必须等于当日已发布的 `market_aggregation`（`publication_kind=market_aggregation`、`scope_type=board`）pointer 的 `data_run_id`；不一致拒绝发布。
3. **source_core_run_id 一致**：`market_review_runs.source_core_run_id` 必须等于当日已发布的 `stock_core` pointer 的 `data_run_id`；不一致拒绝发布。
4. **无 failed signals**：`market_review_signals` 中不得存在 `status=failed` 的记录；存在 failed signal 拒绝发布。
5. **无 failed run_items**：`market_review_run_items` 中不得存在 `status=failed` 的记录（`skipped` 允许，但必须记录原因）。
6. **coverage_ratio >= 0.95**：market 范围 `coverage_ratio >= 0.95`，且 `industry_l1` ready 比例达到配置门槛。

> **[P0 2026-08-04] 无未来数据门（历史基线 point-in-time）**：
> 本 run 正常落库的当日观测（`trade_date == run.trade_date`）是**合法行为**，
> 不得被当作"未来数据"拦截。门禁只拦截严格未来观测（`trade_date > run.trade_date`），
> 它代表乱序计算或历史基线污染（真实数据泄漏）。
> 历史基线读取（`load_metric_history`）已使用 `trade_date < run.trade_date` 过滤，
> 因此合法 run 只有当日观测，必然通过该门。

force=True 时跳过 1-6 门禁，但必须（2026-08-01 安全收口，已按此实现对齐代码）：

- 不得写入 `factor_publications`（仅 admin 内部查看）；
- run.status 不得进入 `published`，`published_at` 不得写入；
- 必须在 run.metadata_json["provisional_publication"] 记录：
  `force_requested=true`、`is_provisional=true`、发布门禁评估结果
  `gate_blockers`、执行时间 `requested_at`、操作者 `operator`、
  幂等键 `idempotency_key`；
- 必须返回 `is_provisional=true` 标记；
- 该 run 永远不得作为普通用户读取入口的正式 pointer；
- 普通用户 API（`/review/dates`、`/review/latest`、`/review/{date}/overview`、
  `/review/{date}/scopes` 等）只能读取正式 pointer；provisional run 仅 admin
  可通过 `include_partial=true` 或显式 `run_id` 查看。

### 23.5A Review publication withdrawal（撤销正式 pointer）

- 撤销唯一正式入口：`review_publication_service.withdraw_review_publication`
  （CLI：`python -m app.scripts.withdraw_review_publication`，默认 dry-run，
  `--apply` 才执行写入）；
- 只删除 `(scope_type=market, scope_key=market, publication_kind=market_review,
  trade_date=指定日)` 的唯一 pointer，不得触碰其他交易日或其他
  publication_kind；
- 保留 review run / scope snapshot / signal / attribution / instrument
  全部数据，禁止删除 Review run，禁止裸 SQL；
- withdrawal 只撤销 pointer；被撤销 pointer 指向的 run 的 `status`、
  `published_at` 和全部子数据是历史审计事实，禁止回退、清空或原地重算；
- after-close 只有在当前正式 market_review pointer 仍指向该 run 时，才可将
  历史 `published` run 作为正式结果复用；pointer 已撤销时必须明确阻断复用，
  后续由升级后的算法版本创建新 run 并重新通过完整发布门禁；
- 撤销审计必须写入 run.metadata_json["publication_withdrawal"]：
  原因、操作者、幂等键、执行时间、被撤销 pointer 详情；
- 幂等：pointer 不存在时不做任何写入，返回 `already_withdrawn`。

### 23.6 history_maps 读取合同

- `metric_engine` 读取历史基线时必须从 `market_review_scope_snapshots` 读取同 `scope_type + scope_key` 的历史记录，禁止从 `board_analysis_snapshots` 或 `factor_publications` 直接拼装。
- 首次运行（无历史数据）时，所有 component `status=insufficient_history`，`historyObservationCount=0`；不得使用 `None` 作为 `status`，也不得用空对象替代。
- `metric_engine` 中 `history is None` 必须显式映射为 `status=insufficient_history`，禁止抛 `AttributeError` 或被 `try/except` 静默吞掉。

## 24. 第二金字塔定义与冷启动（草案补强）

> 本章节为第二金字塔定义的草案补强，明确第二金字塔的维度、聚合口径、P/Q/U/C/V 就绪状态与冷启动 bootstrap 合同。本章节在确认为"已确认"后，优先级高于历史描述中与之冲突的部分（特别是冷启动发布行为）。

### 24.1 第二金字塔维度

第二金字塔（板块级分析层）定义以下六个维度：

| 维度 | 说明 |
|---|---|
| 状态分布（state distribution） | 板块成员第一金字塔状态分布（趋势/结构/动量各状态的占比） |
| 状态迁移（state migration） | 板块状态在时间轴上的迁移轨迹 |
| 事件新鲜度（event freshness） | 板块新鲜结构/动量事件的覆盖与衰减 |
| 宽度（breadth） | 参与成员比例 |
| 集中度（concentration） | 贡献集中度 |
| 相对强度（relative strength） | 板块相对市场/指数的强度 |

第二金字塔不生成综合总分。

### 24.2 行业与概念分别聚合

- 行业（industry）和概念（concept）必须分别聚合（SEPARATELY），不得混合计算；
- 行业聚合结果与概念聚合结果各自独立存储与发布；
- 禁止把概念成员混入行业分母，反之亦然。

### 24.3 P/Q/U/C/V 就绪状态合同

每个 P/Q/U/C/V 聚合变量必须返回以下就绪状态字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `raw_ready` | bool | 原始组件值（rawValue）是否已就绪 |
| `normalized_ready` | bool | 归一化值（value/percentile）是否已就绪（需累计 ≥60 个有效观测） |
| `insufficient_history` | bool | 是否因历史不足无法归一化（与 `normalized_ready` 互斥） |
| `reason` | string | 当 `normalized_ready=false` 或 `insufficient_history=true` 时的具体原因 |

规范：

- `raw_ready=true` 但 `normalized_ready=false` 时，可发布 rawValue，但 `value`/`historyPercentile120d`/`delta1d`/`delta5d` 必须为 `null`；
- 该合同补强 §7.1 的 `status` 字段：`status=ready` 对应 `raw_ready=true && normalized_ready=true`；`status=insufficient_history` 对应 `insufficient_history=true`。

### 24.4 冷启动 bootstrap

- 系统不得强制要求"上线后累计 60 个交易日"才允许发布第二金字塔；
- 必须提供可重复执行的 bootstrap 流程，从已有第一金字塔历史（约 250 个交易日）回填第二金字塔历史观测，生成满足发布门禁的最小历史；
- bootstrap 生成的历史观测必须遵循 point-in-time 语义（使用当日有效成员关系与当日已发布因子），不得使用未来成员或未来因子；
- bootstrap 不得伪造 normalized 值或历史分位；当回填后有效观测仍 < 60 时，对应维度 `insufficient_history=true`；
- bootstrap 流程必须幂等且可重放，相同输入产生相同历史观测；
- bootstrap 完成后必须在发布元数据中记录 `bootstrap=true` 与回填的观测数量。

### 24.5 fp_segment_change_pct 禁止伪造

- `fp_segment_change_pct`（第一金字塔分段变化百分比）在数据为空时必须返回 `null`，不得伪造为 0、均值或前值；
- 该字段为空时必须在 `reason` 中明确记录"无可用分段数据"。

## 最终原则

- 筛选器是发现引擎；
- 第二金字塔是解释引擎；
- 第一金字塔是验证引擎；
- 自选与盘中监控是追踪引擎；
- 历史复核是反馈引擎。

复盘页必须把这五个引擎连接成一个可解释、可下钻、可追踪、可复现的工作台。

## 25. raw与normalized分离 + 冷启动展示 + bootstrap 生效范围（CHANGE-20260801-001）

### RV-25-01 raw 与 normalized 双值分离合同

Review 的每个 P/Q/U/C/V 维度和每个聚合指标必须显式维护两套值：

- `raw_value` / `rawValue`：原始聚合值，只要有足够的当日样本即可计算（无需 60 日历史）；
- `normalized_value` / `normalizedValue`：相对历史分位或归一化值，只有当有效历史观测 ≥ 60 交易日时才允许非 null。

前端展示规则（ScopeMetricsTable / SignalCard / 五阶段）：

| 条件 | rawValue | coverage | normalized / 历史分位 | reason 显示 | 筛选信号 |
|---|---|---|---|---|---|
| `rawReady && normalizedReady`（完整合同） | 展示 | 展示 | 展示 | — | 正常可用 |
| `rawReady && insufficient_history`（冷启动） | **必须展示** | **必须展示** | null / 不展示空值灰态 | `insufficient_history` + "历史不足 N 天，仅展示原值" | 该指标筛选器 disabled，tooltip 显示原因 |
| 上游失败 / 数据不存在 | null / N/A | 0.0 | null | `no_raw_data` / `compute_failed` | disabled |

- 不得在冷启动时把整页 P/Q/U/C/V 统一显示为"不可用"或"加载失败"。
- `SignalCard` 的 0/1/2 结论：当至少 P 与 Q 两个维度存在 raw 值时，可显示 raw 基线结论；当任一维度 normalized 仍为 null（insufficient_history）时，SignalCard 必须显示 "raw baseline only" 标签。

### RV-25-02 pointer 日期同步

Review 正式发布 pointer（`review_publications` 或等价发布记录）的 `trade_date` 必须满足：

```
review.pointer.trade_date == stock_core.pointer.trade_date == board_analysis.pointer.trade_date
```

若上游 pointer 还未发布（盘后刚启动、盘后未完成），review 页面必须显示未发布提示（"盘后未完成，当前展示上次正式发布：YYYY-MM-DD"），不得指向陈旧 pointer 的 7/29 日期却显示盘后已完成的 7/31 状态栏。

### RV-25-03 bootstrap 生效范围

- 优先使用历史正式 `stock_core` / `board_analysis` 当日快照做 point-in-time bootstrap；**禁止**用"今日板块成员"去回补昨天或更早 review 的 `scope_items`。
- 没有历史成员版本的板块（如 2026 年新成立板块且 `board_version_id < N`）：该板块的 historical observations 明确标为 `bootstrap_unavailable`，不得伪造。
- `review_runs.metadata.bootstrap = true` 必须记录 bootstrap 来源 run_id、覆盖的交易日范围、真实观测数量（不等于 60 天花板）、未回填板块清单及原因。

### RV-25-04 五个阶段的冷启动表现

Review 五阶段（Market Scan / Filter Discovery / Board Attribution / Stock Validation / Tracking Review）在 insufficient_history 场景：

1. **Market Scan**：允许展示 raw 的 P/Q/U/C/V 热力或分布卡片，右上角显示 `insufficient_history` chip；
2. **Filter Discovery**：展示 raw 分布 + 原因提示；依赖 normalized 的 percentile 筛选器 disabled；
3. **Board Attribution**：展示板块 raw 排名（覆盖度≥0.95 的板块），原因在 hover 显示；
4. **Stock Validation**：个股第一金字塔必须完整展示（个股不受 review 历史门槛限制），`insufficient_history` 仅影响板块级对比；
5. **Tracking Review**：追踪信号状态以第一金字塔验证结论为准，normalized 不足时 `trackingStatus` 不允许给出 normalized 偏差结论（只写 raw + "历史不足，未计算分位偏差"）。

## 26. Review 计算事实、历史观测与发布终态合同（2026-08-01）

- 当日与历史计算统一使用 `ReviewMemberFact`：instrument identity、日线 OHLC/真实日收益、
  rolling position、volume/amount、第一金字塔 canonical 当前/前日状态、新鲜事件和权重。
- P 使用真实日收益与价格位置；Q 使用 canonical 趋势/结构/新鲜事件；U 至少两个维度较前日改善；
  C 明确 `equal_weight/official_weight/amount_weight`；V 分离 volume 与 amount 并使用同量纲均量比。
- 编排采用两遍：先保存 raw/normalized，再按同日同 scope family 计算横截面分位，最后评估 signal。
- `market_review_metric_observations` 保存 component raw、denominator、field source、weight mode、
  algorithm/input/membership version；唯一键保证 bootstrap 重放幂等。
- Bootstrap 默认 dry-run，按 market/index/style/industry/concept 分开处理；缺 PIT 成员时写
  `bootstrap_unavailable`，禁止回退到当前成员。
- force 永远只产生 provisional，不写正式 pointer；正式发布必须校验 core/board pointer、配置范围、
  coverage、run items、算法版本和非 canary/provisional。旧 run 不原地修改。
- 五阶段 UI 必须区分无信号、无追踪、历史不足、字段缺失和 API 错误；Evidence Drawer 展示来源、
  分母、权重、版本与 readiness。

## 27. Review 依赖矩阵与发布质量硬门（QM-63，2026-08-04）

### RV-27-01 上游依赖矩阵

Review run 创建时必须显式解析上游依赖状态，并把结果固化到 run 记录，
禁止用"没查到就当成功"或"缺失就静默跳过"掩盖降级：

| 上游 | 状态 | Review 行为 | 记录 |
|---|---|---|---|
| stock_core | 失败 / 未发布正式 pointer | **阻断**，不得发布 | publish gate blocker |
| 第一金字塔核心字段 | 不完整 | **阻断** | publish gate blocker |
| chip 共识 | 全缺失 / 全失败 / expected 无法确定 | 降级为 **core-only**，仍可生成 | `degraded_reasons=["CHIP_UNAVAILABLE"]` + `chip_coverage` |
| chip 共识 | 部分覆盖（`succeeded/expected < 1`） | 生成，标记部分降级 | `degraded_reasons=["CHIP_PARTIAL"]` + `chip_coverage` |
| chip 共识 | 全部覆盖（`succeeded == expected`） | 正常生成 | `degraded_reasons=[]` + `chip_coverage` |
| auction 竞价 | 失败 / 不可用 | **默认降级，不阻断**（不参与发布门禁） | 由 auction 自身 readiness 表达 |
| 历史基线 | <60 日 | 保留 raw，normalized 不就绪 | `status=insufficient_history` |

> **[P0 2026-08-04] chip 覆盖率合同**：chip 无独立 run 记录，只通过 `core_run_id` 挂靠 stock_core。
> 因此 `source_chip_run_id` 恒为 `NULL`，**不得把 stock_core 的 run id 写成 source_chip_run_id 冒充 chip run**。
> chip 质量改由真实覆盖率判定（分母 = stock_core run 的 `expected_count`）：
> `succeeded_count / failed_count / skipped_count / missing_count / coverage`，
> 覆盖率写入 `run.metadata_json["chip_coverage"]` 并经 overview/run API 暴露给前端展示。
> 判定规则：`expected_count` 缺失或 `succeeded==0` → `CHIP_UNAVAILABLE`；
> 无降级仅当 `succeeded >= expected` **且 `failed==0` 且 `skipped==0` 且 `missing==0`**；
> 否则 `CHIP_PARTIAL`。不得再用"chip 表已有行"的占位比例冒充覆盖率（那会漏掉缺失股票）。

> **[P0 2026-08-04] chip 覆盖率算法版本隔离**：chip 表唯一键含 `algorithm_version`，
> 同一 (instrument, trade_date, core_run_id) 可同时存在不同 chip 算法版本记录。
> 覆盖统计必须按当前 `CHIP_CONSENSUS_ALGORITHM_VERSION` 过滤，并用
> `COUNT(DISTINCT instrument_id)` 去重，否则会重复计数、coverage 超 100%、
> 旧版本成功行掩盖新版本失败行。实际采用的 chip 算法版本写入
> `metadata_json["chip_coverage"]["algorithm_version"]`。

`market_review_runs` 新增两列承载该合同：

- `source_chip_run_id UUID NULL`：chip 无独立 run 记录，恒为 `NULL`；不得写成 core_run_id 伪装可用；
- `degraded_reasons JSONB NOT NULL DEFAULT '[]'`：降级原因列表，空数组表示无降级。

重新创建同一 (trade_date, algorithm_version) 的 run 时，这两列必须按当次解析结果**刷新**，
不得沿用上一次的陈旧降级状态。

### RV-27-02 发布质量硬门（在既有门禁基础上新增）

`evaluate_publish_gate` 在原有检查（canary/provisional 禁发、market/major_index/style/industry_l1
范围完整、配置范围隔离、run items 终态、core/board pointer 一致、published run 不可原地重发）之外，
新增三条硬门：

1. **无未来数据（point-in-time 硬门）**：本 run 落库的 `market_review_metric_observations`
   不得存在 `trade_date > run.trade_date` 的**严格未来**记录（`>` 而非 `>=`，[P0 2026-08-04]）。
   本 run 正常落库的当日观测（`trade_date == run.trade_date`）是合法行为，不得被拦截；
   门禁只拦截乱序/未来观测（代表历史基线污染）。历史基线读取满足 `observation.trade_date < run.trade_date`。
   检出严格未来即阻断并报告条数。
2. **reason 完整性**：market 范围的 P/Q/U/C/V，凡处于非 ready 状态
   （`raw_ready=false`，或 `raw_ready=true` 且 `normalized_ready=false`）
   必须给出非空 `readiness.reason`。**禁止无原因的不可用**——与第一金字塔 chip 七态合同一致。
3. **all-null 禁止发布空壳**：market 范围 P/Q/U/C/V 的 `value` 全部为 `None` 时禁止正式发布。
   此类 run 可以以 provisional / failed 形式留档审计，但不得成为正式 pointer。

三条硬门均只产出 blocker，不做任何"自动修正"或静默兜底。

### RV-27-03 原子发布与幂等

- publication 记录写入与 current pointer 更新必须在同一事务内完成；
- 失败时保留旧 pointer，新 publication 对普通用户不可见；
- 对**已是当前正式 pointer** 的 published run 重复发布：返回既有 publication，
  **零写入**（不插入新行、不 flush、不 delete、不改写 `run.status` 与 `run.published_at`）；
- 已 published 但**已非**当前正式 pointer 的旧 run：禁止原地重发。
