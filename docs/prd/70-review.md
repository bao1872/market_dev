# 复盘模块 PRD V1.0

状态：已确认
最后确认日期：2026-07-30
对应 Map：`../maps/70-review.md`
需求所有权：复盘模块完整产品与工程合同（目标行为、数据契约、API、筛选器、归因、追踪、编排）

> 本文件是复盘模块的权威产品与工程合同。实现时不得根据页面方便性重新发明业务逻辑；前端不计算聚合变量、筛选器或归因结论。

## 0. 背景与当前基线

现有系统已经具备：

- 个股第一金字塔：趋势、结构、动量为必选维度，筹码共识为可选维度；
- 约250个交易日的第一金字塔历史状态；
- board_analysis_snapshots：行业/概念板块的趋势、结构、动量、量能和事件分布；
- factor_publications：核心和板块结果的发布指针；
- /market 行情列表与 /stock/:symbol 个股详情；
- BoardAnalysisPage.tsx：当前仅是板块分析列表与详情，不是完整复盘页。

本需求不推翻现有板块聚合，而是在其上增加：

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

### 8.4 信号排序

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
- pointer切换失败只重试发布，不重算。

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
- `docs/runbooks/after-close-production-run.md`（review canary/resume/publish）
- `rules/70-trae-cn.md`（一轮闭环、ledger恢复、页面验收要求）

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

force=True 时跳过 1-6 门禁，但必须：

- 不得写入 `factor_publications`（仅 admin 内部查看）；
- 必须在 run.metadata_json 中记录 `force_published=true` 与跳过的具体门禁；
- 必须返回 `is_provisional=true` 标记；
- 该 run 永远不得作为普通用户读取入口的正式 pointer。

### 23.6 history_maps 读取合同

- `metric_engine` 读取历史基线时必须从 `market_review_scope_snapshots` 读取同 `scope_type + scope_key` 的历史记录，禁止从 `board_analysis_snapshots` 或 `factor_publications` 直接拼装。
- 首次运行（无历史数据）时，所有 component `status=insufficient_history`，`historyObservationCount=0`；不得使用 `None` 作为 `status`，也不得用空对象替代。
- `metric_engine` 中 `history is None` 必须显式映射为 `status=insufficient_history`，禁止抛 `AttributeError` 或被 `try/except` 静默吞掉。

## 最终原则

- 筛选器是发现引擎；
- 第二金字塔是解释引擎；
- 第一金字塔是验证引擎；
- 自选与盘中监控是追踪引擎；
- 历史复核是反馈引擎。

复盘页必须把这五个引擎连接成一个可解释、可下钻、可追踪、可复现的工作台。
