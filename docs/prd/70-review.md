# 复盘模块 PRD V2.0 — Review Observation Model（Scope Observation Model）

状态：已确认
最后确认日期：2026-08-12
对应 Map：`../maps/70-review.md`
需求所有权：复盘模块完整产品与工程合同（目标行为、数据契约、API、Discovery、信号、Scope Observation Model、Cross-Scope Relation、追踪、编排）
需求变更依据：`ref/需求变更.md`（2026-08-12 Review Observation Model Refactor；Scope Observation Experiment 收口）

> 本文件是 Review 指标、历史、横截面、Discovery、信号、Scope Observation Model、Cross-Scope Relation、归因、发布和页面状态的唯一需求真源。[`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只定义 Review 与上游节点之间的依赖、lineage 和闭环场景，不替代本文件的 Scope Observation Model 合同、Discovery 定义及门禁。

> 本文件是复盘模块的权威产品与工程合同。实现时不得根据页面方便性重新发明业务逻辑；前端不计算聚合变量、筛选器或归因结论。

> **2026-08-12 领域收口（Scope Observation Model）**：Review 的 **first-layer observation model 从 P/Q/U/C/V 评分模型正式替换为 Scope Observation Model**（见 §7）。P/Q/U/C/V 不再作为 scope first-layer fact、discovery prerequisite、State/Change 前置输入、板块综合事实或底层 score。本文件旧 §7 的 P/Q/U/C/V 内容及其在 §6.4/§8/§9/§10/§11/§12/§23/§24/§25/§26/§27 中作为 first-layer observation 的依赖，全部视为 **legacy implementation baseline / IMPLEMENTATION_REDESIGN_REQUIRED**，需在后续 Implementation Design 中映射到新 Observation Model。本文件新增 §7 为 Scope Observation Model 的权威语义合同。

## 0. 领域输入

Review 依赖以下正式领域输入：

- 个股第一金字塔：趋势、结构、动量为必选维度，筹码共识为可选维度；
- 至少满足滚动窗口要求的第一金字塔历史状态和日线事实；
- Board Analysis 提供的行业/概念板块 Scope PIT membership、Price/Amount 事实与第二金字塔数据；
- 个股 core 与板块聚合的正式 publication lineage；
- 行情列表、个股详情和板块详情的稳定导航身份。

Review 新业务链为：

第一金字塔与历史状态
→ Scope PIT membership
→ Price / Amount facts
→ Scope Observation（PRICE / TREND / STRUCTURE / MOMENTUM / PARTICIPATION / CHIP-if-available）
→ State / Transition / Diffusion + Contribution / Concentration facts
→ Evidence
→ Discovery 聚合（Discovery = user-level market finding）
→ Cross-Scope Relation 分析
→ 归因与个股验证
→ 用户追踪
→ 次日状态复核与历史反馈

## 1. 产品目标与边界

### 1.1 产品目标

Review 系统回答以下核心问题：

1. **今天市场最明显的结构变化在哪里？**
2. **它是行业、主题、风格还是多轴共振？**
3. **这是静态强，还是今天正在增强/减弱？**
4. **是少数龙头驱动，还是参与正在扩散？**
5. **哪些股票贡献最大？**
6. **这个现象是今天第一次出现，还是已经持续？**
7. **每一个结论的底层证据是什么？**

### 1.2 不做的内容

- 不预测明日涨跌；
- 不生成买卖建议；
- 不做黑箱"机会分""风险分"或板块综合总分；
- **不恢复 P/Q/U/C/V 作为 underlying observation model**（见 §7；UI 摘要属 presentation layer）；
- 不在前端重算 Scope Observation facts、筛选条件或归因；
- 不把全部99个第一金字塔字段塞入复盘页；
- 不把通用板块排行榜当作复盘主流程；
- 不以自然语言总结替代结构化证据；
- 不建立第二套K线或行情筛选器；
- 不依赖新闻、研报、LLM 推理作为 Discovery 成立的必要条件；
- 不引入 AI 主营业务分类、人工真实概念标签、新闻 NLP 分类、唯一炒作逻辑；
- 不构造唯一 `primary trading category`。

## 2. 权威业务链

```text
A. 已发布 stock_core pointer
B. 已发布 board_analysis / market_aggregation pointer
C. 第一金字塔历史基线（默认120个交易日，最低60日）
D. Scope PIT membership（per-trade-date，point-in-time）
        ↓
所有 Scope Family 独立平行计算：
  market / major_index/* / style/* / industry_l1/* / industry_l2/* / industry_l3/* / concept/*
        ↓
生成每个 Scope 的 Observation facts：
  PRICE（Return Level / Distribution / Breadth / Contribution / Concentration / Amount）
  TREND / STRUCTURE / MOMENTUM（State+Breadth / Transition / Diffusion[PROVISIONAL]）
  PARTICIPATION（Volume / Amount threshold-free distribution）
  CHIP（if available；否则 UNRESOLVED）
        ↓
State / Transition / Diffusion + Contribution / Concentration facts
        ↓
运行 Filter Engine（A/B/C/D 作为内部 Evidence Family；threshold 依赖 Observation Evidence）
        ↓
Signal = atomic evidence 生成
        ↓
Discovery 聚合（多个 Signal → 一个 Discovery）
        ↓
Cross-Scope Relation 分析
        ↓
归因与个股验证
        ↓
发布 Review Run
        ↓
前端 Discovery Workspace
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
       &stage=discoveries
       &scopeType=industry_l1
       &scopeKey=electronics
       &discoveryId=<uuid>
       &signalId=<uuid>
       &boardId=<uuid>
       &symbol=000021
       &trackingTab=history
```

规则：

- URL是页面状态的唯一可分享入口；
- 首次加载先解析URL，再写入组件状态，禁止 hydration 后被默认值覆盖；
- 切换 Discovery、Signal、板块、股票时更新URL；
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

保存每个市场范围的 **Scope Observation facts 与证据**。

> **数据模型语义收口（2026-08-12）**：`p_payload / q_payload / u_payload / c_payload / v_payload`
> 现有 physical persistence shape 属于 **legacy implementation baseline**，与新 Scope Observation
> semantic contract 需要在后续 **Implementation Design** 中完成映射/迁移设计。
> 本轮 **不** 设计新 DB schema（不决定是单个 `observation_payload` JSONB、多个 payload column、
> 还是新表），全部 **DEFER** 到 implementation design。
> 逻辑层明确：Scope Snapshot 必须能够表达新 Observation Model（PRICE / TREND / STRUCTURE /
> MOMENTUM / PARTICIPATION / CHIP-if-available）。

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
p_payload JSONB      # legacy baseline；Observation 映射 DEFER 到 implementation
q_payload JSONB      # legacy baseline
u_payload JSONB      # legacy baseline
c_payload JSONB      # legacy baseline
v_payload JSONB      # legacy baseline
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

保存子范围归因结果（ATTR-1 taxonomy hierarchical attribution）。

```
id UUID PK
signal_id UUID FK
child_scope_type VARCHAR       # 子范围类型（industry_l2 / industry_l3 / concept）
child_scope_key VARCHAR
child_scope_name VARCHAR
relation_type VARCHAR           # contribution（ATTR-1）
contribution_value NUMERIC
contribution_rank INTEGER
metrics_payload JSONB
evidence_payload JSONB
coverage_ratio NUMERIC
created_at
```

> 注：`child_scope_*` 在 ATTR-1 taxonomy attribution 中是合法语义。ATTR-3 Cross-Scope Relation 为 logical/domain requirement，其 physical persistence ownership DEFER 到实现阶段。不要求为此改名或新增表。

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

现有兼容 baseline：

```
id UUID PK
user_id UUID
source_signal_id UUID         # legacy Signal tracking
tracking_type VARCHAR          # signal / scope / instrument（现有）
scope_type/scope_key NULL
instrument_id NULL
status VARCHAR                 # active / confirmed / invalidated / closed
confirmation_conditions JSONB
invalidation_conditions JSONB
note TEXT NULL
created_at / closed_at
```

V2 logical requirement：

Tracking domain 必须能够表达以下 target 类型：
- **Discovery**（使用 Discovery logical identity）
- **Scope**
- **Instrument**

Legacy Signal target 可以兼容保留。

具体如何实现 Discovery target reference（是否新增 `source_discovery_id`、是否改 `tracking_type` 枚举、是否使用 generic `target_type`/`target_id`、FK 结构、nullable 规则、migration shape）全部属于 implementation design。本 PRD 当前不选择其中任何一种。

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

## 6. Scope Discovery 模型

### 6.1 Scope Family 平行发现轴

Review Discovery 层正式定义以下 Scope Family 为**平行观察轴**：

```
Market
Major Index
Style
Industry
Concept
```

五类 Scope Family 在**发现阶段互相独立、平行计算**。

其中 Industry 内部保留 taxonomy hierarchy：

```
Industry L1
Industry L2
Industry L3
```

但必须明确：

> **Discovery topology ≠ Taxonomy topology**

Industry L1 / L2 / L3 都可以独立进入 Discovery。Concept 必须独立进入 Discovery。

**正式废弃以下旧模型：**

- ~~`Level 1 scope → 命中 → Level 2 drilldown → concept`~~ 作为 Discovery gate 的模型。
- ~~L1 命中后才能扫描 L2~~
- ~~L2 命中后才能扫描 L3~~
- ~~Industry 命中后才能扫描 Concept~~

分类父子关系只能用于：

- taxonomy（行业分类学）；
- 导航（Scope Browser 中 L1→L2→L3 浏览）；
- 上下钻（归因时 drill-down）；
- attribution（行业内部 parent/child contribution）；
- relationship explanation（解释为何某 L2 与 L1 共享特征）。

不得作为 signal/discovery eligibility gate。

### 6.2 第一阶段必须独立扫描的 Scope

正式 Review run 应独立生成以下 scope observations：

```
market

major_index/*
style/*

industry_l1/*
industry_l2/*
industry_l3/*

concept/*
```

每个 Scope independently：

```
members
   ↓
第一金字塔成员事实
   ↓
Scope Observation facts（PRICE / TREND / STRUCTURE / MOMENTUM / PARTICIPATION / CHIP-if-available）
   ↓
State / Transition / Diffusion + 历史位置 / 横截面位置
   ↓
Discovery Evidence
```

任何 scope 都不得因为另一个 scope 未命中、无异常、未发布 discovery 而失去自身参与发现的资格。

### 6.3 Membership 与 Discovery 分离

Scope membership 只回答：

> **哪些股票属于这个范围？**

不得回答：

> **这只股票今天主要在炒什么？**

股票允许同时属于多个 Industry、Concept、Style、Index。

系统不得通过 Industry membership 排除 Concept discovery，也不得通过 Concept membership 改写股票的行业归属。

多重归属不是数据污染，而是市场事实。

不得引入唯一 `primary trading category` 作为前置计算条件。

### 6.4 Scope Observation 合同

> **2026-08-12 语义收口**：每一个正式 discovery scope 必须独立产生 **Scope Observation facts**
> （见 §7 Scope Observation Model），**不再以 P/Q/U/C/V 聚合变量作为 first-layer observation**。

每一个正式 discovery scope 都必须独立产生：

- **PRICE** facts：Return Level / Return Distribution / Price Breadth / Signed Return Contribution / Price Concentration / Amount Contribution·Concentration
- **TREND / STRUCTURE / MOMENTUM** facts：State+Breadth（categorical distribution）· Transition（ratio）· Diffusion（PROVISIONAL）
- **PARTICIPATION** facts：Volume / Amount threshold-free distribution
- **CHIP**：if available；否则 UNRESOLVED
- 每个 Observation fact 的 raw value（原始聚合值）、delta1d（1日变化）、delta5d（5日变化）
- self historical percentile（自身历史分位；仅对可历史归一的 facts）
- same-family cross-sectional percentile（同类横截面分位；仅对可比 facts）
- component / member evidence（组件/成员证据）
- coverage（覆盖率）
- readiness / data quality（就绪状态与数据质量）

#### 6.4.1 Comparable Peer Cohort（横截面对比合同）

横截面比较必须 within **comparable peer cohort**。Taxonomy level ≠ comparable statistical cohort。

正式 peer cohort 定义：

| Scope | Peer Cohort | 说明 |
|---|---|---|
| `market` | 无 peer cohort | 全市场是基准范围，不构造一元素横截面 percentile；异常判断主要使用自身历史基线和正式市场事实 |
| `major_index` | 同 `major_index` cohort | 指数之间比较 |
| `style` | 同 `style` cohort | 风格之间比较 |
| `industry_l1` | **仅** `industry_l1` | 一级行业之间比较 |
| `industry_l2` | **仅** `industry_l2` | 二级行业之间比较 |
| `industry_l3` | **仅** `industry_l3` | 三级行业之间比较 |
| `concept` | 同 `concept` cohort | 概念之间比较 |

**禁止**：

- `industry_l1 + industry_l2 + industry_l3` 混合在同一个 cross-sectional percentile pool；
- `concept` 与 `industry` 混合；
- `style` 与 `major_index` 混合。

本 PRD 只定义 comparable cohort，不规定具体统计公式（percentile rank / z-score / winsorization 等由算法版本控制）。

### 6.5 Historical Membership Source Contract（原 §6.3，Phase 4A3）

本小节定义 `major_index` / `style` / `industry_l1` 历史 PIT membership 的数据来源合同。
Phase 4A3（2026-08-09）完成 source selection 与授权分析；当前 schema
（`BoardDefinitionVersion` + `BoardMembershipHistory`，`UniverseDefinition` + `UniverseMembership`，
均由 migration `079_board_hierarchy_batch_identity` 建立）**已完全支持 PIT 半开区间**
`[effective_from, effective_to)`，无需新增表或 migration。缺口仅为**来源数据填充**。

#### 6.5.1 硬规则（禁止项）

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

#### 6.5.2 各 family 来源状态（Phase 4A3 结论）

| family | 当前状态 | 授权来源 | 历史窗口 2026-02-06→2026-08-07 可用性 |
|---|---|---|---|
| industry_l1 | 官方来源 = `pywencai`（wencai），**current-only**；`BoardMembershipHistory` 仅在两次 sync 间发生变更时才产生新版本，首 sync 之前无历史快照 | `wencai`（rules/90 唯一授权） | **SOURCE_NOT_AVAILABLE**（授权来源无法提供历史窗口；见 §6.3.3） |
| csi300 | migration `079` 已建 placeholder `UniverseDefinition`（key=`csi300`，`population_status=blocked_external_population`，`source=authoritative-provider-required`），无已填充 connector | 待授权（候选见 §6.3.4） | **SOURCE_SELECTION_REQUIRED**（候选存在但未授权） |
| csi500 | 同上（key=`csi500`） | 待授权（候选见 §6.3.4） | **SOURCE_SELECTION_REQUIRED** |
| style（large_cap / small_cap） | migration `079` 已建 placeholder（`large_cap_style` / `small_cap_style`，`blocked_external_population`），PRD §6.1 称“使用已有、版本化的风格股票池定义”，但**项目内无任何 origin / 构造规则** | 无 | **STYLE_PRODUCT_DECISION_REQUIRED**（产品定义缺口，非 ingestion bug） |

#### 6.5.3 industry_l1 决策

- `pywencai` 是 rules/90 唯一授权的板块来源，固定查询 `"同花顺概念，行业分类"`，
  返回**单一当前快照**，无 `as_of` / `trade_date` / 历史参数 → 确认 current-only。
- 因此对于 Review MVP 所需的 `2026-02-06 → 2026-08-07` 历史 PIT 窗口，
  授权来源**无法提供** → 结论 **SOURCE_NOT_AVAILABLE**。
- 可选出路（需用户决策，不在此擅自选择）：
  1. MVP 接受 industry_l1 **仅 forward-only**（自 2026-08-09 起每日 snapshot），历史窗口标记为 `bootstrap_unavailable`；
  2. 授权一个具备历史行业分类的替代来源（需先改本 PRD）。

#### 6.5.4 csi300 / csi500 候选矩阵（需授权后选用）

候选按 §10 优先级（现有已授权 > 官方 > 新依赖）排序：

1. **CSIndex 官方 constituent 下载**（www.csindex.com.cn）：提供成分股及 effective date，
   PIT 语义最干净；官方一级来源，优先推荐。
2. **交易所官网（SSE/SZSE）成分公告**：官方但需解析公告，cadence 不规整。
3. **Tushare `index_member` / `index_weight`**（需 token，项目当前未依赖）：
   具备 `start_date`/`end_date` PIT 查询，但属新增 external dependency，需 PRD 授权。

当前均**未授权**，故 csi300/csi500 = **SOURCE_SELECTION_REQUIRED**。

#### 6.5.5 style 产品决策缺口（OPEN DECISION）

- PRD §6.1 仅声明“使用已有、版本化的风格股票池定义”，未定义：
  - large_cap / small_cap 的**来源**（官方风格指数？自定义池？）；
  - 还是**运行时按市值排名构造**（top N / 分位）。
- 禁止自行发明 `top300=large` / `top20%=small` / market-cap threshold（§8）。
- 此为 **PRODUCT_DEFINITION_GAP**，必须由用户决定产品定义后，才能确定 source 或构造规则。
- PRD 此处标记为 **OPEN DECISION**，不补规则。

#### 6.5.6 最小 normalized shape（与现有表对齐）

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

#### 6.5.7 Required historical coverage

第一阶段只要求完整覆盖 `2026-02-06 → 2026-08-07`（当前 120 交易日 baseline）。
不要求 10 年历史，除非来源免费自然提供且不显著增加复杂度。

#### 6.5.8 Review MVP 发布就绪门禁（Phase 4C 校正）

原 §11.1 发布门禁将 `major_index` / `style` / `industry_l1` readiness 作为 whole-Review
硬性阻塞项。Phase 4C（2026-08-09）按产品决策校正为 **渐进式 scope readiness**：

- **MARKET = Review MVP 强制历史基线（HARD GATE）**：
  - 必须存在、状态 `ready`、满足现有 market coverage/quality 要求（含 §7 CORE Observation facts
    就绪；旧 P/Q/U/C/V 五项 `normalized_ready` 为 legacy baseline，映射 DEFER 到 implementation）；
  - 不满足 → 发布门禁 CLOSED。
- **industry_l1 / major_index / style = 渐进式可选 scope（OPTIONAL）**：
  - 真实就绪 → 正常参与产品输出；
  - `bootstrap_unavailable` / `insufficient_history` / `blocked_external_population` /
    PIT unavailable → 保留真实状态与诊断，但**不加入 whole-product blocker**；
  - 禁止把 optional scope 状态伪装成 `ready`；
  - 禁止 current membership × historical date / latest snapshot 回填 / forward-fill。
- **Concept / industry_l2 / industry_l3 = PARALLEL DISCOVERY SCOPE**：
  - 产品 eligibility：Concept 是正式 parallel Discovery scope，与 Industry 平等参与发现；
  - 数据/历史 readiness：若 Concept PIT/history 当前不足，真实输出 `bootstrap_unavailable` / `insufficient_history` / `unavailable`，不阻塞其他 scope；
  - 不得把"当前数据未就绪"写成"Concept 产品能力 deferred"。
- **BJ = DEFERRED**（保持原决定）。
- 此校正**不改变指标公式**：仅调整 scope readiness / publication readiness 语义。
- 实现约束（`review_publication_service.evaluate_publish_gate`）：market 仍为唯一强制 scope；
  optional/parallel scope 不可用仅记为诊断，不阻塞整个 Market Review MVP 发布。

## 7. Scope Observation Model（Review 第一层 Observation 模型）

> **2026-08-12 领域收口（Scope Observation Experiment，S3 = PASS）**：Review 的
> **first-layer observation model 正式从 P/Q/U/C/V 评分模型替换为 Scope Observation Model**。
> 旧的 §7「P/Q/U/C/V 指标合同」内容不再作为 underlying observation truth，被本 §7 取代。

### 7.0 P/Q/U/C/V 正式处置

**P/Q/U/C/V 不再是 Review underlying observation model。** 不得继续作为：

- scope first-layer fact；
- discovery prerequisite；
- State/Change 前置输入；
- 板块综合事实；
- 底层 score。

原因（Scope Observation Experiment 结论）：实验没有发现任何必须通过 P/Q/U/C/V score 才能表达、
而直接 Observation facts 无法表达的重要语义。P/Q/U/C/V 只是对 CORE Observation facts 的聚合，不新增信息。

如果未来 UI 需要 summary：**属于 presentation layer**，不得反向成为 underlying observation truth。
**PRD 不需要设计任何新的 summary score。**

### 7.1 Scope Observation Model 结构

每一个 Scope 直接观察以下对象（**不新增其他顶层维度**）：

```text
SCOPE OBSERVATION

PRICE
  - Return Level
  - Return Distribution
  - Price Breadth
  - Signed Return Contribution
  - Price Concentration
  - Amount Contribution / Concentration

TREND
  - State + Breadth
  - Transition
  - Diffusion [PROVISIONAL]

STRUCTURE
  - State + Breadth
  - Transition
  - Diffusion [PROVISIONAL]

MOMENTUM
  - State + Breadth
  - Transition
  - Diffusion [PROVISIONAL]

PARTICIPATION
  - Volume Participation Distribution
  - Amount Participation Distribution

CHIP
  - UNRESOLVED
  - CHIP / PARTICIPATION RELATION = PENDING DATA
```

> **说明**：Concentration / Contribution 直接属于 PRICE 内部事实，**不** 单列第二个
> `CONCENTRATION_CONTRIBUTION` 顶层模块，避免重复模型入口。

### 7.2 PRICE — 结果事实层

PRICE 是 Review 的 **最上层结果事实层**（result fact layer，不是 Trend score）。

- **Return Level**：`equal_weight_return_mean`（CORE）。`return_median` 与 `return_p50` 视为同一事实，只保留一个产品字段。
- **Return Distribution**：`return_p25 / p50 / p75`（CORE，描述同一个 distribution object；`p10/p90` 为 EXPLANATORY 尾部）。P25/P50/P75 是一个 distribution object 的描述，不是三个独立维度。
- **Price Breadth**：`advance_ratio / decline_ratio / unchanged_ratio`（threshold-free，return>0 / <0 / ==0）。
  **Price Breadth ≠ Trend State/Breadth**（语义不同，禁止合并）。
- **Signed Return Contribution**（EXPLANATORY）：`signed_return_contribution` 回答"谁推动 / 拖累 Scope return"。
  计算基于 **exact canonical T-1** return：`return = close(T) / close(T-1) - 1`，经两次 bars JOIN；exact T-1 缺 bar → return UNAVAILABLE，**禁止**用更早 bar 回退（禁止 instrument-level LAG(close) 充当 1D return）。
- **Price Concentration**（CORE）：`price_contribution_hhi`（raw）与 `price_contribution_hhi_normalized`。
  raw HHI 保留单 Scope 时间变化解释价值；normalized HHI 用于跨不同成员数 Scope 比较。**不得平均二者，不制造 Concentration Score。**
- **Amount Contribution / Concentration**（CORE）：`amount_share` 与 `amount_contribution_hhi`（raw + normalized）。
  Amount universe 独立（仅需 amount 非空，不要求 T-1 return）。

**三者语义必须分开、禁止混为一个指标**：
- signed return contribution（谁推动/拖累收益）；
- abs price share / price HHI（价格变化是否集中）；
- amount share / amount HHI（成交额是否集中）。

### 7.3 TREND / STRUCTURE / MOMENTUM — 各 horizon 轴的 observation grammar

三个轴共享同一 observation grammar：

- **State + Breadth（CORE）**：State categorical distribution **本身即 Breadth**。例如 Trend 的
  Up / Neutral / Down，Scope observation 使用各状态成员占比。**不再定义独立 Breadth Score。**
  同理 Structure、Momentum 使用各自合法 categorical state 的完整分布。`neutral` / `flat` 是合法状态，不是 invalid。
- **Transition（CORE）**：`Transition = member exact canonical T-1 → T state migration`。
  跨 Scope 主表达为 **transition ratio**。raw count 可作为 evidence/explanation，**不是跨 Scope 比较 primitive**。
  新增/删除 membership 不得算成 transition；denominator = T 与 T-1 的 common valid members。
- **Diffusion（PROVISIONAL）**：当前候选 `D1 / D3 / D5`，定义为 State/Breadth distribution 随时间的变化。
  **不删除、不宣布完全验证、不在正式 PRD 选择最佳 horizon。** 原因：历史 PIT membership 数据不足（Q3 INCONCLUSIVE）。

### 7.4 PARTICIPATION

正式定义：**threshold-free distribution**。

- **Volume**：`vol_ratio20_p25 / p50 / p75` 等 distribution descriptors；
- **Amount**：`amt_ratio20_p25 / p50 / p75` 等 distribution descriptors。

**不要定义**：`>1 active`、`>1.5 strong`、`high/low`、Participation Score。
P25/P50/P75 是一个 distribution object 的描述，不是三个独立维度。

### 7.5 CHIP

正式状态：**UNRESOLVED**。

当前不得定义 `Chip == Participation`，也不得定义 `Chip != Participation`。原因：当前实验窗口
chip-like historical field 数据不足（Q7 INCONCLUSIVE）。

在 PRD 保留 slot：`CHIP / PARTICIPATION RELATION — PENDING DATA`。此 slot **不阻塞 Review**。

### 7.6 Scope Observation 就绪状态与诊断

每个 Observation fact 保留真实就绪状态与诊断（含 exact-T1 口径）：

- `price_candidate_count` = PIT ∩ valid FP ∩ close(T) available；
- `price_valid_count` = 其中 exact canonical T-1 close 也 available；
- `missing_exact_t1_count` = price_candidate_count − price_valid_count（审计停牌/缺 bar 影响）；
- `amount_valid_count` = PIT ∩ valid FP ∩ amount(T) non-null（不要求 T-1 return）；
- 历史少于 60 日：该 fact 的 historical percentile 不生成（`insufficient_history`），保留 raw；
- 不得用 cross-section percentile 替代缺失的历史 percentile。

### 7.7 Observation Model 与 Evidence

Scope Observation facts 是 Evidence / Signal / Discovery 的底层事实来源。Filter 与 Discovery 只消费
**structured Observation Evidence**，不依赖 P/Q/U/C/V score。

### 7.8 Scope Architecture Contract（Scope Family 可扩展性与 Canonical Observation ownership）

> **2026-08-12 领域收口（Scope Architecture PRD + Governance Closure）**：本小节正式冻结
> **Scope Family extensibility** 与 **Canonical Scope Observation ownership** 两个长期架构决策。
> 输入为「Review Observation Model — PRD → Code Impact Audit」（2026-08-12）。
> 本小节只收口架构契约，**不** 重新设计 §7 Observation 指标、**不** 决定 persistence/API/filter 实现形状。

#### 7.8.1 Scope logical contract

每个 Scope（无论属于哪个 Family）必须具备以下逻辑属性：

- **scope identity**：稳定、可追溯的身份（scope_type + scope_key + 版本化来源）；
- **PIT membership at T**：目标交易日 `T` 的 point-in-time member set；
- **peer cohort**：用于横截面比较的同类范围集合；
- **metadata / readiness**：taxonomy 元数据与 source / readiness 状态。

#### 7.8.2 Scope Family 是平行、可扩展的观察对象

`market` / `major_index` / `style` / `industry` / `concept` 均为**平行 Scope Family**。

Family 之间的差异**主要**属于：

- membership resolver（如何 resolve PIT member set）；
- metadata / taxonomy（board 分类、层级、命名）；
- peer cohort（横截面比较范围）；
- source / readiness（数据来源与就绪状态）。

Family 差异**不得默认**属于：

- Price calculation；
- Trend calculation；
- Structure calculation；
- Momentum calculation；
- Participation calculation；
- Concentration calculation。

即：不同 Family **不得**各自复制一套核心 Observation calculator。

#### 7.8.3 Canonical Observation ownership

正式逻辑链：

```text
Scope Identity
+ PIT Member Set(T)
+ target trade date
+ canonical Member Atomic Facts
        ↓
Canonical Scope Observation
        ↓
PRICE / TREND / STRUCTURE / MOMENTUM / PARTICIPATION / CHIP-if-available
```

- **同一个 Observation fact 只有一个 canonical production owner。**
- 不得为 `industry / concept / style / index / market` 分别复制核心 Observation 计算。
- **允许 Family-specific adapter**，但 adapter 只处理：
  membership / metadata / peer cohort / readiness（source availability）。
- 若未来某 Scope Family 确实需要特殊 Observation computation：必须有明确业务语义依据，
  **先修改正式 PRD**，不得在实现中自行增加 family branch。

#### 7.8.4 Scope maturity

正式区分三个不同维度，**不得混为一谈**：

- **architecture support**：架构支持一个 Family（≠ 产品假设已 VALIDATED）；
- **product validation**：产品假设已被用户通过真实结果确认（≠ STABLE / RELEASED）；
- **release maturity**：进入正式长期兼容与发布治理。

不同 Scope Family 允许处于不同成熟度。新 Family 可以通过实验逐步接入，
但默认复用同一 Observation Engine。

#### 7.8.5 Peer cohort 属于 Scope contract

- Observation Engine **消费** resolved peer cohort；
- Observation Engine **不得**自己根据 scope_type 猜 comparison universe；
- 例如：`concept → concept cohort`、`industry_l1 → industry_l1 cohort`、
  `major_index → major_index cohort`、`style → style cohort`、`market → no cross-sectional peer`。

#### 7.8.6 Persistence boundary

Scope Observation 的 persistence 形状（单个 `observation_payload` JSONB / 多个 payload column /
新表 / migration shape）**继续 DEFER**（见 §5.3）。本小节只冻结 **logical canonical
Observation ownership**，不决定任何 DB schema 或 migration。

---

## 8. Filter Engine（内部 Evidence Family）

A/B/C/D 继续作为**内部算法 family**，但不再作为用户前端一级信息架构。

筛选器必须由版本化配置驱动：

```
backend/config/review_filters.yaml
```

并使用Pydantic schema校验。不得把阈值散落在多个service。

初始工程默认值仅用于形成可运行基线，上线前必须用历史回放校准；配置变化必须升级filter_version。

**定位变更（2026-08-11）：**

- A/B/C/D 是 Filter Engine 内部的算法分类，不是前端一级产品结构。
- 前端不再按 A/B/C/D 分组展示，转用用户语义（状态/改善/恶化/扩散/收缩/异常/共振）。
- D Family（state migration / freshness / diffusion / concentration / relative strength）定位为 **Discovery Evidence Family**，不是独立 Signal Family。
- `MarketReviewSignal` 保留为 atomic evidence record；新的 `Discovery` domain object 负责 user-level finding 聚合。

**Observation Model 收口（2026-08-12）：**

- Filter / Discovery **只消费 structured Observation Evidence**（§7 Scope Observation Model），
  不得依赖 P/Q/U/C/V score 作为 first-layer observation。
- 以下 **A/B/C 初始阈值**（§8.1–8.3）当前以 `P/Q/U/C/V` 分位/`value` 表达，属于对
  P/Q/U/C/V first-layer 的硬依赖，**标记 `IMPLEMENTATION_REDESIGN_REQUIRED`**：
  必须在 Implementation Design 中把条件改写为对 Observation facts（PRICE Return
  Level/Distribution/Breadth、Concentration、PARTICIPATION distributions 等）的 structured
  条件，**不得现场发明新 P/Q/U/C/V 阈值**。
- D 族（state migration / freshness / diffusion / concentration / relative strength）消费
  **第二金字塔 raw evidence**（非 P/Q/U/C/V score），保持不变。

### 8.1 A类：表面表现与内部质量偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12**：A 类条件原以 `P.value / P.historyPercentile120d / Q.delta1d / U.delta1d`
> 表达，依赖已废弃的 P/Q/U/C/V first-layer。需改写为对 §7 Observation facts 的条件
> （surface strong = PRICE Return Level/Breadth 高；internal weak = State+Breadth 恶化等）。
> 本 PRD **不** 定义新阈值；具体条件在 Implementation Design 中确定。

**A1 surface_strong_internal_weak**

初始条件（legacy P/Q/U/C/V 表达，REDESIGN REQUIRED）：

```
P.historyPercentile120d >= 70
(P.value - Q.value) 的自身历史分位 >= 90
Q.delta1d <= 0 或 U.delta1d <= 0
coverage >= 0.95
```

**A2 surface_weak_internal_improving**

初始条件（legacy P/Q/U/C/V 表达，REDESIGN REQUIRED）：

```
P.historyPercentile120d <= 40
Q.delta1d 的历史分位 >= 70
U.delta1d 的历史分位 >= 60
coverage >= 0.95
```

### 8.2 B类：当前状态与变化速度偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12**：B 类依赖 P/Q/U/C/V 历史分位与 1 日变化分位，需改写为对 Observation facts
> 的状态与 Transition 条件。本 PRD **不** 定义新阈值；REDESIGN REQUIRED。

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

### 8.3 C类：成交、参与与集中度偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12**：C 类依赖 V/U/C 分位，需改写为对 PARTICIPATION distributions 与 Price/Amount
> Concentration facts 的条件。本 PRD **不** 定义新阈值；REDESIGN REQUIRED。

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

### 8.4 D类：第二金字塔 Evidence Family

> D 族只在 industry/concept scope 评估（需 pyramid_v2 数据）；
> market/major_index/style scope 无 board_analysis，D 族不命中。
> D 族输出的是 **Discovery Evidence**，不是独立用户 Finding。

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

> **Concentration 语义校正（2026-08-11）**：`concentration_high` 是 **State**，不是 **Anomaly**。
> 必须区分：
> - `concentration_state_high`：当前集中度高（背景状态）
> - `concentration_rising`：集中度正在上升（Change）
> - `concentration_abnormal`：集中度相对历史异常（Anomaly）
> - `concentration_broadening`：集中度正在扩散（Change，反向）
> - `concentration_narrowing`：集中度正在收缩（Change，反向）
>
> 用户 Discovery 优先消费 Change/Anomaly 变体。仅 `concentration_state_high` 不得单独生成高价值 Discovery。

**D5 relative_strength_strong**

```
vs_market.ratio >= 1.1
equal_weight_diff > 0
```

### 8.5 Discovery 排序

Discovery 必须进行**全量排序后再分页**。

禁止：
```
DB LIMIT 50 → 再在这50条中排序
```

正确逻辑：
```
全部 eligible discovery → 统一 rank → Top N → pagination
```

排名必须可解释，不生成不可追溯黑箱总分。

排序至少考虑：

- 异常程度（anomaly strength）
- 变化强度（change strength）
- 参与宽度（breadth / participation）
- 证据一致性（evidence consistency）
- 持续时间（lifecycle / duration）
- coverage
- cross-scope confirmation

具体算法权重另由算法版本控制。`rank_key` 必须把上述分项保存下来。

**已废弃**：`scope_type` 固定优先级作为排序键（平行发现后 scope family 平等）。

## 9. 归因（Attribution）与 Cross-Scope Relation

筛选器只负责发现 evidence，归因负责解释。

Attribution 正式区分三层业务语义：

### 9.1 ATTR-1：Taxonomy Hierarchical Attribution

用于 Industry taxonomy 内部的层级贡献：

```
L1 ↔ L2
L2 ↔ L3
```

回答：一个行业范围内部，哪些下级 taxonomy scope 对上级状态/变化产生主要贡献。

对每个 Discovery：

- 识别相关的下级 taxonomy scope（industry L2 对 L1、industry L3 对 L2）；
- 计算下级 scope 对上级 **Scope Observation facts** 变化的贡献（如 Return Level/Distribution、
  State+Breadth、Transition、Concentration 等；**不再以 P/Q/U/C/V score 为贡献对象**）；
- 保留正贡献和负贡献；
- 按绝对贡献排序；
- 保存前N项，但API支持分页读取全部。

归因不得仅按涨幅排序。

**注意**：这是 attribution，不是 discovery gate。不得恢复"L1 命中 → 才允许扫描 L2/L3"。

### 9.2 ATTR-2：Member Attribution

回答某个 Scope / Discovery 内：哪些 instrument 是主要贡献成员。

至少可表达：

- **Observation contribution**（对 Return Level / State+Breadth / Concentration / Participation 等
  Observation facts 的贡献；**不再以 P/Q/U/C/V score 为贡献对象**）
- board/scope role（core / second_line / elasticity / follower / laggard）
- relation to scope（synchronized_strengthening / instrument_leads_scope / etc.）
- fresh event evidence
- contributionPayload
- roleEvidence

PRD 定义业务合同，具体 ranking weight 由算法版本控制。

每只成员计算：

- 对 Return Level/Distribution 的表面变化贡献；
- 对 Trend/Structure/Momentum State+Breadth 与 Transition 的贡献；
- 对 Participation 的参与确认；
- 对 Price/Amount Concentration 的集中度贡献；
- 对 Volume/Amount Participation 的成交贡献；
- 新鲜结构/动量事件；
- 与板块状态的关系。

角色分类与因子状态分开保存。角色可使用相对贡献和历史稳定性生成，但必须保留role_evidence。

### 9.3 ATTR-3：Cross-Scope Relation

平行扫描完成后，增加独立的 **Cross-Scope Relation** 阶段。

该阶段不是重新计算第一金字塔，而是比较各 Scope Discovery 的：

- 成员交集（membership overlap）
- Scope Observation facts（PRICE / State+Breadth / Transition / Participation）
- 变化方向
- 异常强度
- 结构事件
- 扩散程度
- 代表股票

目标是识别：**今天不同分类体系是否在描述同一股市场资金行为。**

#### 第一阶段支持的 Relation Type

至少支持：

```
concept ↔ industry
concept ↔ concept
concept ↔ style
industry ↔ style
industry ↔ industry
```

#### Relation 输出语义

禁止输出模糊"相关"。至少区分：

- **THEME_LED**：概念明显强于所属传统行业（Concept ↑↑，Industry →）
- **INDUSTRY_LED**：行业整体出现一致改善，多个相关概念同步
- **BROAD_CONFIRMATION**：行业 + 概念 + 风格出现共同确认
- **ISOLATED_THEME**：单一概念异常，行业和相关概念没有确认，参与宽度有限
- **STYLE_LED**：多个不同行业同时出现相同风格特征（小盘/高弹性/低位修复）
- **CONFLICTING**：不同 Scope 指向相互冲突的状态（Concept 强但 Industry State+Breadth / Participation 恶化），必须保留，不得强行合并

#### Relation 数据来源

第一阶段只使用已有结构化事实：

```
membership overlap
price
first pyramid
Scope Observation facts（PRICE / State+Breadth / Transition / Participation）
pyramid_v2
history
```

> **2026-08-12**：`P/Q/U/C/V` 已从 Relation 数据来源中移除，替换为 Scope Observation facts。
> `CONFLICTING` 示例中的 "Industry Q/U 恶化" 语义改写为 "Industry State+Breadth / Participation 恶化"。

不得依赖新闻、研报、公告语义或 LLM 推理作为 Relation 成立的必要条件。

Relation 是 Discovery 后的关系解释，不是 Scope 之间新的计算 gate。

## 10. State / Change / Anomaly 分离

这是 Review Discovery 的 P0 原则。

> **2026-08-12 Observation Model 收口**：State / Change / Anomaly **不再定义为 P/Q/U/C/V score
> 的变化**，改以结构化 Observation facts 表达（§7）：
> - **State** = 当前 structured Observation（如 Price Breadth、State+Breadth categorical distribution、Concentration、Participation distribution）；
> - **Change** = Transition / Diffusion / observation change facts（member exact T-1 → T 状态迁移、分布随时间变化）；
> - **Anomaly** = 相对于历史 / comparable cohort 的异常 evidence。
> **不设计新的 anomaly score**；具体统计公式（1D/5D change、历史分位、横截面分位）如尚未验证，
> **DEFER 到 algorithm implementation**。

### 10.1 State（状态）

State 描述：**现在是什么样。**

例如：
- 集中度高
- 趋势向上
- 量能活跃
- 价格处于高位
- 内部结构较强

State 可以作为证据。但：**静态 State 不得默认生成用户可见 Discovery。**

### 10.2 Change（变化）

Change 描述：**今天相对昨天发生了什么。**

例如：
- 集中度快速上升
- 参与度扩张
- 结构破坏开始扩散
- 动量增强成员增加
- 龙头与跟随开始同步

Change 可以形成 Discovery Candidate。

### 10.3 Anomaly（异常）

Anomaly 描述：**这个变化相对自身历史或同类范围是否异常。**

最低应允许以下比较维度：
- 1D change
- 5D change
- self historical percentile
- same-day cross-sectional percentile

### 10.4 Discovery 成立条件

一个用户可见 Discovery 原则上应至少包含：
- State + Change
- 或 State + Historical/Cross-sectional Anomaly

而不是只有 State。

## 10A. Signal 与 Discovery 分层

### 10A.1 正式定义

- **Signal = atomic evidence**（原子证据）：Filter Engine 命中的单条技术信号，包含触发条件、metric 值和历史分位。
- **Discovery = user-level market finding**（用户级市场发现）：聚合多个 Signal 形成的一条用户可理解的市场发现。

### 10A.2 关系

一个 Scope 可以同时命中多个内部 Signal：

例如：
```
low_level_repair
breadth_expansion
synchronized_expansion
event_freshness_high
relative_strength_strong
```

用户侧应聚合成一个 Discovery，而不是五条重复 Signal。

例如：
> **玻璃基板：内部参与扩张，结构修复加速，相对市场强度上升。**

下钻后才能查看哪些 filter 命中、哪些 metric 贡献、哪些 component 支持、哪些股票贡献。

### 10A.3 Discovery Domain Object

Review domain 正式业务结构可以表达为：

```
Market Review
├─ Scope Observations
├─ Signals（atomic evidence）
├─ Discoveries（user-level finding）
├─ Cross-Scope Relations
├─ Attribution
└─ Tracking
```

这是逻辑/domain ownership，不是强制物理 storage topology。

建议新增正式 Discovery domain object：

```yaml
discovery:
  discovery_id:        # 稳定 logical identity，在一个正式 Review Run 范围内可唯一定位
  review_run_id:       # 所属 Review Run
  trade_date:
  scope_type:
  scope_key:
  scope_name:

  state:
  change:
  anomaly:

  key_evidence:        # 聚合后的关键证据
  related_scopes:      # Cross-Scope Relation 结果
  representative_instruments:

  lifecycle:
  first_seen:
  duration:
  status:

  data_quality:
```

Discovery 必须有稳定 logical identity。API（`/discoveries/{discovery_id}`）、tracking、evidence drilldown 使用同一 logical identity。

PRD 不要求 Discovery 必须拥有：
- 独立 database table（可以是 view / materialized view / 内存聚合）
- 独立 publication pointer
- 独立 scheduler job
- 独立 ProductReadiness node
- 独立 mandatory product status
- 特定 UUID/hash 生成算法
- source_signal_ids 字段或 foreign key

Discovery 必须可追溯到 supporting evidence。具体 lineage representation DEFER 到实现阶段。

Signal 继续负责算法命中、证据、版本追踪。Discovery 聚合多个 evidence。不要求立即破坏性删除历史 signal schema（additive migration）。

### 10A.4 历史兼容

原有 `MarketReviewSignal` 和 A/B/C/D filter family 允许保留。新的 Discovery 应 consume existing/new signals as evidence，而不是强制把 Signal schema 一次性废弃。迁移优先采用 additive 而不是 destructive。

## 10B. 信号生命周期与追踪状态机

### 10B.1 系统信号

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

### 10B.2 用户追踪

Tracking target 必须支持：

- Discovery（使用 Discovery logical identity）
- Scope
- Instrument

Legacy Signal tracking 可以兼容保留。

每天Review Run完成后自动生成evaluation。用户关闭追踪不删除历史。

实现阶段必须采用 additive-compatible 方式支持 Discovery tracking。具体 schema/migration DEFER 到实现阶段。

## 11. 任务编排与发布

盘后顺序（平行扫描模型）：

```
stock_core published
→ board_analysis published
→ create market_review_run
→ compute ALL scope metrics 并行（market / major_index/* / style/* / industry_l1/* / industry_l2/* / industry_l3/* / concept/*）
→ evaluate filters（A/B/C/D 作为 Evidence Engine）
→ generate Signal records（atomic evidence）
→ aggregate Discovery candidates
→ compute Cross-Scope Relations
→ compute attributions + representative instruments
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

本节与 §6.5.8「Review MVP 发布就绪门禁（Phase 4C 校正）」构成同一份合同。

> **2026-08-12 Observation Model 收口**：发布门禁就绪性改以 **Scope Observation facts**（§7）
> 表达，**不再以 P/Q/U/C/V 五项 `normalized_ready` 作为 first-layer 门禁对象**。旧 P/Q/U/C/V
> gate 引用标记为 **legacy baseline / IMPLEMENTATION_REDESIGN_REQUIRED**（见 §23.5 等 legacy 契约）。

单scope：

- underlying coverage >= 0.95
- **必要 Observation facts 状态可用**（market 至少含 PRICE Return Level/Distribution/Breadth、
  State+Breadth 等 CORE facts；具体字段集在 Implementation Design 确定）

整套Review（渐进式 scope readiness）：

**1. MANDATORY — market（HARD GATE）**

- `market` scope 必须存在且状态 `ready`，并满足 market coverage/quality 要求（含 §7 **CORE Observation
  facts** 就绪；旧 P/Q/U/C/V 五项 `normalized_ready` 为 legacy baseline，映射 DEFER 到 implementation）；
- market missing / not ready / coverage 低于强制门槛 → **whole Review publication CLOSED**。

**2. PROGRESSIVE OPTIONAL — industry_l1 / major_index / style**

- 真实就绪 → 正常参与产品输出；
- PIT unavailable / `insufficient_history` / `blocked_external_population` / `bootstrap_unavailable` / skipped → 记录为 **scope-level diagnostic / unavailable**，保留真实状态；
- 上述 optional unavailable **不得阻塞** whole Review MVP publication；
- 禁止把 optional scope 状态伪装成 `ready`。

**3. PARALLEL SCOPES — industry_l2 / industry_l3 / concept**

- 各自独立 readiness，不阻塞其他 scope；
- 真实就绪 → 正常参与 Discovery；
- 不可用 → 记录诊断，不影响其他 scope 的 Discovery 发布。

**4. UNEXPECTED EXECUTION FAILURE 仍然阻塞**

- 任何 scope（含 optional / parallel）出现非预期执行失败或非终态（`failed` / `pending` / `running`）→ **whole Review publication CLOSED**；
- optional/parallel 语义只豁免「数据源不可用」，不豁免「执行异常」。

**5. 数据来源硬约束**

- 禁止 current membership × historical date 回填；
- 禁止 latest snapshot backfill / forward-fill 冒充 PIT 成员。

**6. 其他整套条件（不变）**

- signal evaluation无系统性异常；
- source_core_run_id和source_board_run_id均指向当前正式pointer。

> 说明：Phase 4C 之前的旧规则「配置的主要指数和风格范围必须ready 
> / 一级行业ready比例达到配置门槛」已按 §6.5.8 废止，不再作为 whole-Review 硬门。

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
  "discoverySummary": {
    "total": 12,
    "new": 4,
    "continuing": 5,
    "confirmed": 2,
    "weakened": 1,
    "invalidated": 0
  },
  "signalSummary": {
    "total": 47,
    "new": 15,
    "continuing": 20,
    "confirmed": 7,
    "weakened": 3,
    "invalidated": 2
  }
}
```

overview 中 `discoverySummary` 是用户一级摘要；`signalSummary` 作为 evidence diagnostics 保留。

### 12.2 市场扫描

```
GET /api/v1/review/{trade_date}/scopes
```

参数：

- scope_type
- scope_family
- sort
- page
- page_size
- include_partial=false

返回每个范围的 **Scope Observation facts**（§7：PRICE / State+Breadth / Transition / Diffusion /
Participation）、变化、历史分位和命中数量。（旧 P/Q/U/C/V 聚合变量不作为 first-layer observation 返回。）

### 12.3 信号（Signal = atomic evidence）

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

Signal endpoint 定位为 **evidence / debug / drilldown**。保留用于兼容和算法调试，不作为用户一级发现入口。

### 12.3A Discovery（用户一级发现入口，新增）

```
GET /api/v1/review/{trade_date}/discoveries
GET /api/v1/review/discoveries/{discovery_id}
```

Discovery 是 primary user-level finding endpoint。筛选参数：

- scope_type
- scope_family
- status（new / continuing / confirmed / weakened / invalidated / transformed）
- sort（按 rank_key 排序）
- page / page_size

Discovery detail 必须返回或可导航到：

- scope（type / key / name / members / coverage）
- state（当前状态摘要）
- change（1D / 5D 变化）
- anomaly（historical / cross-sectional percentile）
- keyEvidence（聚合后的关键证据）
- relatedScopes（Cross-Scope Relation 结果）
- representativeInstruments（含 contributionPayload / roleEvidence）
- lifecycle（firstSeen / duration / status）
- dataQuality（coverage / readiness / reason）

Attribution 和 instrument evidence 以 Discovery 为用户入口。

前端不得从 Signal endpoint 自行聚合 Discovery。

### 12.4 归因与个股

```
GET /api/v1/review/discoveries/{discovery_id}/attributions
GET /api/v1/review/discoveries/{discovery_id}/instruments
GET /api/v1/review/signals/{signal_id}/attributions       # 兼容：signal-level evidence drilldown
GET /api/v1/review/signals/{signal_id}/instruments         # 兼容
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
  ScopeBrowser.tsx           # Scope Family 平行切换浏览器
  DiscoveryList.tsx           # Discovery 列表（替代旧 SignalCard 列表）
  DiscoveryCard.tsx           # 单条 Discovery 卡片
  DiscoveryDetail.tsx         # Discovery 详情页
  ScopeDetailPanel.tsx        # Scope 详情面板
  InternalStructurePanel.tsx  # 内部结构展示
  PyramidV2Panel.tsx          # Pyramid V2 面板
  CrossScopeRelationPanel.tsx # Cross-Scope Relation 面板
  InstrumentEvidencePanel.tsx # 个股证据面板
  TrackingPanel.tsx           # 追踪面板
  EvidenceDrawer.tsx          # 结构化证据抽屉
  ScopeMetricsTable.tsx
  AttributionTable.tsx
  ReviewInstrumentTable.tsx
  ReviewDataQualityBadge.tsx
  review.module.scss

frontend/src/pages/ReviewPage.tsx
```

现有 `BoardAnalysisPage.tsx` 不删除。应抽取可复用的：

- BoardMetricsSummary
- BoardDistributionPanel
- BoardEventDistribution

供板块分析页和复盘归因阶段共同使用，禁止复制两套计算和展示逻辑。

## 14. 页面信息架构（Discovery Workspace）

**已废弃**：旧五阶段 UI（市场扫描 / 筛选发现 / 板块归因 / 个股验证 / 追踪复核）不再作为用户信息架构的强制要求。

新用户主路径调整为**市场结构工作台**语义：

```
市场发现 / 今日结构
    ↓
异常发现 / Discovery Workspace
    ↓
Discovery 详情（Scope + Evidence + Relation + Instruments）
    ↓
我的追踪
```

后台仍然可以保留 `scope / filter / signal / attribution / tracking` 作为内部 domain object，但用户不需要理解系统执行了几个 pipeline phase。

### 14.1 固定顶部

展示：

- 交易日与前后交易日；
- Review发布状态；
- Core/Board Run；
- 覆盖率；
- 算法版本、筛选器版本、历史基线；
- 数据质量入口。

顶部不得显示AI自由生成的市场结论。

### 14.2 Scope 浏览器

Scope Family 必须允许平行切换：

```
全市场
主要指数
风格
行业
概念
```

Industry 内再选择 L1 / L2 / L3（仅浏览维度，不是 discovery gate）。

不得重新引入"先选择 Industry → 才能看 Concept"的隐式 gate。

### 14.3 市场发现首页

首页首要回答：**今天市场发生了什么？**

建议最小结构：

```
今日市场状态

主要发现
────────────────
玻璃基板
主题驱动 · 新增
参与扩张 ↑  结构质量 ↑  量能 ↑  集中度 →
18 / 22 成员确认

机器人
行业+概念共振 · 持续3日
...

军工
上涨但内部质量减弱
...
```

不得首先展示 A/B/C/D 分类。

### 14.4 Discovery 详情必需信息

每一个 Discovery 至少必须能下钻看到：

#### Scope
- family / type / name
- members
- coverage

#### Current State
- **Scope Observation facts**（§7）：PRICE（Return Level / Distribution / Breadth / Concentration）、
  State+Breadth（Trend/Structure/Momentum categorical distribution）、Participation distribution
- （旧 P/Q/U/C/V 不作为 first-layer observation 展示；如需 summary 属 presentation layer）

#### Change
- Transition（member exact T-1 → T 状态迁移）ratio、1D / 5D observation change

#### Position
- historical percentile
- cross-sectional percentile

#### Internal Structure
应尽可能消费现有后端 component：
- trend breadth
- structure breadth
- momentum breadth
- synchronized improvement
- structure breakdown
- non-leader participation
- HHI
- Top5 contribution
- volume expansion

#### Pyramid V2
若 scope 可用：
- migration
- freshness
- diffusion
- concentration
- relative strength

#### Related Scopes
Cross-Scope Relation（THEME_LED / INDUSTRY_LED / BROAD_CONFIRMATION 等）

#### Representative Instruments
- first pyramid
- fresh events
- contribution payload
- role（core / second_line / elasticity / follower / laggard）
- role evidence
- relation to scope

### 14.5 Evidence Drawer（结构化证据解释器）

Evidence Drawer 是**结构化证据解释器**，不是 JSON Debugger。

正式用户页面禁止直接将 `JSON.stringify(payload)` 作为主要展示。

Raw JSON 仅允许 admin/debug mode。

普通用户必须转换为结构化展示：

- metric（指标名称）
- value（当前值）
- change（变化）
- percentile（历史位置）
- denominator（分母）
- coverage
- source / component（来源）
- trigger reason（触发原因）
- member contribution（成员贡献）

### 14.6 个股证据

个股下钻必须展示：

- First Pyramid
- Fresh Events
- **Observation contribution**（contributionPayload：对 Return Level / State+Breadth / Concentration /
  Participation 等 Observation facts 的贡献；旧 P/Q/U/C/V contribution 为 legacy baseline）
- Board Role
- Role Evidence（roleEvidence）
- Relation To Scope

不得只展示单一 `contributionValue`。个股"为什么重要"必须可解释。

### 14.7 追踪

内部子Tab：

- 过去发现
- 自选映射
- 事件演化

"过去发现"字段：

- 首次日期
- Discovery
- 范围
- 当前状态
- 连续天数
- 状态变化
- 后续证据

## 15. 前端数据与状态规则

- 使用React Query；
- query key必须包含reviewRunId/tradeDate/resource/id/filters；
- 已发布历史复盘使用较长staleTime，不每30秒刷新；
- 最新交易日处于computing时仅轮询run status，发布后停止；
- 页面组件不得拼接不同Review Run；
- 切换 Discovery 时取消无效请求；
- 后端返回partial/stale/unavailable时必须显示具体状态；
- 禁止无限"加载中"；请求超时、404、422、500分别显示明确错误和request_id。

## 16. 与现有页面的边界

### /market

负责：全字段筛选、排序、列设置、导出、自选管理。

Review跳转参数：

- reviewDiscoveryId
- tradeDate
- sourceCoreRunId
- boardId
- firstPyramidFilters
- sort

### /stock/:symbol

负责：K线、第一金字塔完整详情、事件和筹码状态。

Review只传：

- from=review
- discoveryId
- boardId
- tradeDate

### /boards/analysis

保留为板块原始分析和管理/研究入口；Review阶段三复用其组件，不复制业务。

## 17. 加载、空态和异常态

必须覆盖：

- 当日Review尚未计算；
- 计算中；
- partial未发布；
- 已发布但无 Discovery（"今日无满足当前 Discovery 条件的市场发现"，可下钻查看 signal/evidence diagnostics）；
- 已发布有 Discovery 但无 Signal（"今日无独立 Signal 命中，所有 Discovery 来自历史持续或结构聚合"）；
- scope coverage不足；
- 历史不足无法计算分位；
- Discovery 无可归因子范围/个股；
- 个股无第一金字塔；
- 用户无复盘权限；
- API超时或版本不一致。

用户主空态："今日无满足当前 Discovery 条件的市场发现"（可下钻查看 signal/evidence diagnostics）。
Signal 空态（evidence 层）："今日未命中已配置偏差筛选器"。

## 18. 性能与缓存

- 页面首屏只加载overview、Discovery 摘要和 scope 摘要；
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
- **Scope Observation facts 计算**（§7：PRICE / State+Breadth / Transition / Diffusion / Participation；
  旧 P/Q/U/C/V 计算为 legacy baseline，映射 DEFER 到 implementation）；
- 历史分位不足；
- State / Change / Anomaly 分离；
- Signal → Discovery 聚合；
- A/B/C/D 各 Evidence 正反例（含 D concentration state/change/anomaly 语义）；
- Signal 生命周期；
- Discovery 生命周期（new / continuing / confirmed / weakened / invalidated / transformed）；
- Cross-Scope Relation 计算与 relation type 分类；
- ATTR-1（taxonomy hierarchical attribution）/ ATTR-2（member attribution）/ ATTR-3（cross-scope relation）；
- global rank before pagination；
- tracking状态机；
- 模板化解释。

### 19.2 PostgreSQL集成测试

不得skip：

- migration upgrade/downgrade/upgrade；
- run/item并发claim；
- 相同输入幂等；
- Signal 唯一约束；
- Discovery identity / idempotency（若实现选择 persistence）；
- Signal ↔ Discovery evidence lineage；
- pointer不混run；
- published与partial隔离；
- Discovery pagination / ranking；
- attribution和instrument分页；
- tracking evaluation逐日唯一；
- 用户权限隔离。

### 19.3 前端目标测试

- URL hydration与前进/后退；
- Scope Browser 平行导航（全市场/指数/风格/行业/概念独立切换，无 Industry→Concept gate）；
- Discovery 列表与详情；
- Discovery evidence drilldown（从 Discovery 下钻到 Signal/metric/component/member）；
- Cross-Scope Relation 展示；
- representative instrument evidence（含 contributionPayload / roleEvidence）；
- 追踪面板；
- 结构化 Evidence Drawer（metric / value / change / percentile / denominator / coverage / source / trigger reason / member contribution）；
- 加载 / 空态 / degraded / 错误状态（Discovery 空态："今日无满足当前 Discovery 条件的市场发现"）；
- 个股跳转参数；
- 加入追踪。

### 19.4 生产canary

先固定：

- 全市场
- 2个主要指数
- 2个风格范围
- 5个一级行业
- 5个概念
- 3个二级行业
- 3个三级行业

验证：

- **Scope Observation facts 值可复算**（§7；旧 P/Q/U/C/V 复算为 legacy baseline）；
- 至少一条正向和一条风险 Discovery；
- Concept 独立产生 Discovery（不依赖 Industry 命中）；
- Cross-Scope Relation 可生成；
- 下钻路径和成员归因一致；
- /market与/stock跳转正确；
- 次日tracking状态可重复计算。

## 20. 验收标准与场景

### 20.1 验收场景

#### Case 1 — Concept 独立发现（京东方 / 玻璃基板）

假设：
- 京东方属于显示面板行业；
- 同时属于玻璃基板 Concept；
- 显示面板行业无明显异常；
- 玻璃基板 Concept 出现明显 P/Q/U/V + migration + volume 改善。

PRD 必须保证：玻璃基板可以独立被发现。不得因为显示面板没命中而漏掉。

#### Case 2 — 小行业局部强

假设：
- Industry L1 整体普通；
- 某 Industry L2/L3 显著改善。

PRD 必须保证：L2/L3 可以独立产生 Discovery。

#### Case 3 — 高集中但长期如此

假设：
- HHI 高；
- Top5 contribution 高；
- 但与历史相比无明显变化。

不得仅因为 `concentration_high` 生成高价值 Discovery。

#### Case 4 — 集中度快速恶化

假设：
- U下降；
- C快速上升；
- leader-median gap扩大。

应能形成"行情向少数龙头收缩"类 Discovery。

#### Case 5 — 多轴共振

Industry + Concept + Style 同时改善。

不得生成三个互不相关的重复 Finding。应允许形成 BROAD_CONFIRMATION。

#### Case 6 — Theme Led

Concept 强、Industry 普通。

应允许输出 THEME_LED。

#### Case 7 — Conflict

Concept 表面很强，但 Industry Q/U 恶化。

不得强行合并成 bullish conclusion。应保留 CONFLICTING relation。

### 20.2 完整验收标准

完整验收必须满足：

- 所有 Scope Family 独立平行参与 Discovery；
- Concept / L2 / L3 不受 Industry L1 的 discovery gate；
- 前端没有 Scope Observation facts 或筛选器计算代码（旧 P/Q/U/C/V 亦不计算）；
- 同一页面不混合不同run；
- Filter Engine 均能给出结构化证据；
- Discovery 可下钻到子范围、Cross-Scope Relation 和股票；
- 个股第一金字塔与板块关系可解释（含 contributionPayload 和 roleEvidence）；
- Discovery 可保存追踪并在下一交易日产生evaluation；
- 过去发现可显示确认/持续/减弱/失效/转化；
- coverage、历史不足和partial不被伪装成完成；
- Evidence Drawer 展示结构化证据，非 Raw JSON；
- 全量 rank → paginate，非 paginate → rank；
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

## 22. 推荐实施顺序（Discovery Model Refactor）

**P0-A：Scope 平行化 + A/B/C Corrective + 排序修复**

- 扩展 scope scanning 到所有 scope family 平行计算（industry_l2/l3/concept 独立）
- A/B/C history context 闭环（CR-01）
- Global ranking before pagination（CR-02）
- Frontend/API contract alignment（CR-03/CR-04）

**P0-B：Discovery Domain + State/Change/Anomaly**

- 新增 Discovery domain object（Signal → Discovery 聚合）
- State/Change/Anomaly 分离
- Concentration 语义校正（state vs change vs anomaly）

**P0-C：Cross-Scope Relation**

- Cross-Scope Relation 计算阶段
- THEME_LED / INDUSTRY_LED / BROAD_CONFIRMATION / ISOLATED_THEME / STYLE_LED / CONFLICTING

**P1：前端 Discovery Workspace 重构**

- Scope Browser 平行切换
- Discovery 列表替代旧 SignalCard 分组
- Discovery 详情页（Scope + Evidence + Relation + Instruments）
- Evidence Drawer 结构化展示
- 追踪面板重构

**Phase 5：历史回放与阈值校准**

使用历史Review Run验证筛选器稳定性；阈值变化升级filter_version，不覆盖旧信号。

## 23. P0 强化条款（review-1.1.0）

> 本章节为 review-1.1.0 算法版本（CHANGE-20260730-014）追加的强制条款，是对 §7（P/Q/U/C/V 指标合同）、§11（任务编排与发布）、§6（Scope Discovery 模型）的补强。本章节条款优先级高于历史 §7/§11 的所有冲突描述。

> **2026-08-12 Observation Model 收口**：本章节及其后的 §24/§25/§26/§27 中所有 `P/Q/U/C/V`
> 引用（含 §23.5 发布门禁 market P/Q/U/C/V value 非空、§24.3 P/Q/U/C/V 就绪状态合同、§25 raw/
> normalized 双值、§26/§27 相关）都属于 **legacy implementation baseline**：它们是既有实现/历史的
> persistence 与 gate 契约，**不** 复活 P/Q/U/C/V 作为 first-layer observation model（§7.0）。
> 这些 legacy 契约与 §7 Scope Observation Model 的映射（gate 就绪对象、就绪字段、schema shape）
> 全部 **DEFER 到 Implementation Design**。本节不现场重写这些 legacy 契约。

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
- 该合同同时适用于所有 scope family：market、major_index、style、industry_l1、industry_l2、industry_l3、concept。

### 23.3 canary 不得切正式 market_review pointer

- canary review run 必须以 `scope=canary` 显式声明，且只能通过 admin 端 provisional 入口查看，不得写入 `factor_publications`（`publication_kind=market_review`）。
- canary run 的 `status` 可以为 `published`（仅表示 run 内部计算完成），但 `factor_publications` 表中不得存在对应 `data_run_id` 指针；普通用户 `/api/v1/review/*` 端点读取的 pointer 不得切到 canary run。
- canary run 的结果可由 admin 通过 `include_partial=true` 或显式 `run_id` 查看，但必须返回 `is_provisional=true` 标记，避免与正式发布结果混淆。
- 上一轮 canary run（`run_id=3e1db415-2266-4cc5-9453-d8561d799b43`，`trade_date=2026-07-29`，`force=True`，`signal_count=0`）保留为审计记录，不修改历史数据；该 run 已写入 `factor_publications`，后续 review-1.1.0 修复后必须通过新 run 切换 pointer，不得复用该 run 重发。

### 23.4 完整 Scope 合同（平行扫描）

Discovery 阶段必须独立覆盖以下全部 Scope Family，缺一不可：

```
market

major_index/*
style/*

industry_l1/*
industry_l2/*
industry_l3/*

concept/*
```

合同要求：

- **market**：全市场有效 A 股，必须使用当日 active 股票，`eligible_count` 不得小于 4500（A 股正常交易日）；
- **major_index**：必须覆盖配置的全部主要指数成分（不少于 2 个），每个指数成分来源以版本化服务为准；
- **style**：必须覆盖配置的全部风格池（不少于 2 个），不得只算部分风格；
- **industry_l1**：必须覆盖全部一级行业（不少于 25 个），不得只算 canary 子集；
- **industry_l2**：全部二级行业独立参与 Discovery，不受 L1 命中 gate；
- **industry_l3**：全部三级行业独立参与 Discovery，不受 L1/L2 命中 gate；
- **concept**：全部概念独立参与 Discovery，不受 Industry 命中 gate。

`scope_key` 命名规范：

- `market`：`scope_key="market"`（固定）；
- `major_index`：`scope_key=<index_code>`（指数代码，不含空格）；
- `style`：`scope_key=<style_code>`（风格代码，不含空格）；
- `industry_l1/l2/l3`：`scope_key=<board_id>`（统一使用 `board_id`，禁止混用 `industry_name`、`industry_code`、`board_name`）；
- `concept`：`scope_key=<board_id>`。

`industry_*` 和 `concept` 的 `scope_key` 必须与 `board_analysis_snapshots.board_id` 对齐，便于 Review 归因直接 JOIN 板块分析结果。

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

- Filter Engine（A/B/C/D）是 **Evidence Engine**（证据引擎）；
- 第二金字塔是 **Explanation Engine**（解释引擎）；
- 第一金字塔是 **Verification Engine**（验证引擎）；
- Discovery 聚合是 **User Finding Engine**（用户发现引擎）；
- Cross-Scope Relation 是 **Market Understanding Engine**（市场理解引擎）；
- 自选与盘中监控是 **Tracking Engine**（追踪引擎）；
- 历史复核是 **Feedback Engine**（反馈引擎）。

复盘页必须把这七个引擎连接成一个可解释、可下钻、可追踪、可复现的市场结构工作台。

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

## 28. Corrective Requirements（实现修正，非新产品功能）

> **2026-08-12**：本节的 Corrective Requirements 属于 **legacy A/B/C filter 实现** 的修正
> （依赖 P/Q/U/C/V history context）。因 A/B/C filters 已标记 `IMPLEMENTATION_REDESIGN_REQUIRED`
> （§8），这些 CR 在 Observation Model 改写后需一并纳入 redesign；不构成对 P/Q/U/C/V first-layer
> 的复活。

以下项目不定义为新产品功能，而是现有设计的实现修正。

### CR-01 A/B/C History Context 闭环

必须正式生成并注入：

```
P-Q historical percentile
Q delta1d historical percentile
U delta1d historical percentile
V delta1d historical percentile

structure breakdown change
C rising
C anomaly state
```

否则任何依赖这些字段的 filter 不得被声明为 production-ready。

### CR-02 Global Ranking Before Pagination

所有 Signal / Discovery：

```
全部 eligible → 统一 rank → Top N → pagination
```

禁止：
```
DB LIMIT 50 → 再在这50条中排序
```

### CR-03 Frontend/API Contract Alignment

前端必须完整接收后端 DTO。特别包括：

```
contributionPayload
roleEvidence
```

当前后端 API 已返回，前端 `ReviewInstrument` contract 必须完整承载。

### CR-04 Signal Payload Rendering

前端读取的数据结构必须与后端真实合同一致。不得假定 `triggerPayload.metrics = Array` 当后端实际合同为 `triggerPayload.metrics = {P,Q,U,C,V}`。

## 29. 不在本次范围

明确不做：

- LLM自动判断股票炒作逻辑
- 新闻NLP自动分类
- 概念可信度模型
- 主营业务真实性评分
- 自动预测涨跌
- 买卖建议 / 组合推荐
- 实时盘中Review
- 自动交易
- AI 主营业务分类
- 人工真实概念标签
- 新闻 NLP 分类
- 唯一炒作逻辑
- Economic Exposure 数据库

本次只解决：**如何更准确地从已有市场结构数据里发现"哪里正在发生变化"。**
