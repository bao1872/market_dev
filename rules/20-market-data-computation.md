# 20 行情、计算与因果不变量

本文件中的规则直接决定结果是否可信，属于 Always-On Correctness。

## 1. MDAS 是行情读取 SSOT

`MarketDataAggregationService.get_bars` 是后端业务行情读取的唯一出口。

业务/API/indicator/SMC/strategy_batch/feature_snapshot/structural_factor/temporal_feature/monitor/capture/chart 等正式链路不得绕过 MDAS 直接调用 repository 私有查询或自行复权。

## 2. 原始数据与复权

- 原始 bar 不复权落库；
- qfq 只在 MDAS 出口统一应用一次；
- 不信任 bar 自带 `adj_factor`；
- `adjustment_as_of` 必须 point-in-time；
- as_of 之后的除权事件不得泄漏进历史结果。

目标日期之后的数据、因子、membership 或未来确认信息不得参与目标日期判断；这是严格的 **no future leakage** 合同。

## 3. 数据周期

正式数据周期：

- 1d：DB 日线 + 正式日线来源；
- 15m：DB 15m + 原生 15m；
- 1h：DB 60m + 原生 60m；
- 1w：日线聚合周线；
- 1mo：日线聚合月线。

禁止用 1m 聚合冒充 15m / 60m / 1d 正式数据。

## 4. Node Cluster 固定输入合同

当前 Node Cluster：

- `1d = 250` 根；
- `15m = 4000` 根；
- `1m = 2` 根已完成 bar。

图表展示长度、指标输出长度、Node 内部输入长度必须分离。

90-bar Capture 舞台不得反向改变指标输入。

若未来产品假设需要改变这些固定参数，必须先修改 PRD/业务合同，不得在实现中偷偷调整。

## 5. Count-aware 回补

正式计算需要固定根数时：

- `completed_only=True`
- `include_realtime=False`（盘后/历史）
- `adj=qfq`
- 统一 `adjustment_as_of`

实际历史不足时必须区分：

- 真正历史耗尽；
- 系统没有取满。

不得用静默短数据当完整输入。

## 6. Canonical Compute

相同业务事实不允许存在多个生产 owner。

详情、盘后、盘中、Capture 等链路的共享基础指标必须来自 canonical kernel / canonical computation；外层可以有节奏、TTL、去重、渲染等适配，不得各自重新实现核心算法。

若当前架构已有 `CanonicalComputationService` / registry，则不得从业务模块直接 import kernel 绕过它。

相同 canonical input 必须具有确定性结果。

## 6.1 Canonical Scope Observation

正式逻辑链：

```text
PIT member set + target trade date + canonical member facts → canonical Scope Observation
```

硬规则：

1. **同一 Observation fact 不得存在多个 production owner**（跨 market / industry / concept / style / index 复用同一 owner）；
2. **不得按 Scope Family 重复实现核心 Observation calculator**（Price / Trend / Structure / Momentum / Participation / Concentration 计算不分 Family 复制）；
3. **Family-specific adapter 只负责** membership / metadata / cohort / readiness；
4. **exact canonical T-1、no-future leakage、transition denominator 等核心时间/因果口径对所有 Family 一致**；
5. **确需 Family-specific computation 必须先有正式 PRD 授权**，不得在实现中自行增加 family branch。

架构细节见 `docs/prd/70-review.md` §7.8。

## 7. 第一金字塔 Core

daily Core 的正式链：

`Daily Bars → Trend/DSA → Structure/SMC → Momentum → Core Artifact → canonical persistence`

约束：

- DSA 在 Core 内计算一次；
- Structure/SMC 不允许第二 owner；
- Momentum 使用同一 target trade date；
- daily Core 不依赖 Node/15m；
- Core version/hash 不得混入 Chip-only 参数；
- Core 成功后可独立消费，不等待 Chip。

## 8. DSA

- 全市场 computable universe 先计算特征；
- 不得在计算阶段按方向、强弱、matched、用户筛选提前删股票；
- 历史计算必须 point-in-time；
- Core canonical DSA 与兼容 projection 必须来自同一计算 artifact；
- projection 不得独立重算；
- `partial_failed` 不得伪装完整发布。

## 9. SMC

### 9.1 FVG 完全排除

Fair Value Gap：

- 不计算；
- 不返回；
- 不缓存；
- 不渲染；
- 不暴露开关。

### 9.2 严格 time-key

SMC 事件和坐标必须按真实时间键匹配。

禁止 index fallback。

time 缺失或匹配失败必须显式 skip / unavailable，不得通过数组位置“猜”事件。

### 9.3 事件生命周期

OB 等结构事件应保持 anchor / confirmed / event 分离。

当前 OB 生命周期使用：

- `OB_CREATED`
- `OB_ENTERED`
- `OB_MITIGATED`

不得用“当前活跃 OB”静默派生为历史进入事件。

## 10. 历史计算前缀不变性

`compute_first_pyramid_history` 等历史序列计算不得把未来完整 group 统计回填到过去。

对任意前 N 根数据：

“截断后重算前 N 行”应与“全量计算的前 N 行”一致，除非 PRD 明确某指标是 hindsight label。

## 11. Chip Consensus

Chip：

- 使用目标交易日已收盘 15m；
- 运行前必须验证 15m freshness / completeness / minimum history；
- 数据不满足时标记 unavailable / skipped / failed；
- 禁止用旧 15m 静默计算；
- 禁止阻塞已完成 Core；
- 禁止失败后重算或改写 Core；
- 有独立 version/hash/run lineage。

## 12. Chart Snapshot 与 Quote

个股详情行情唯一正式入口为 Chart Snapshot。

- 单次 snapshot 不得为了 quote 再做第二次行情读取；
- quote 从同一 snapshot 已加载数据派生；
- 前端不得同时使用独立 `/quote` 和 `/chart-snapshot` 作为两个真源；
- 不得恢复 `useRealtimeQuote` / `mergeRealtimeQuoteIntoBars()` 双源逻辑；
- `include_realtime=true` 仅用于真实交易时段 partial bar；
- 收盘后不得伪装实时。

## 13. Frontend 不重算后端业务

前端可以：

- 格式化；
- 映射展示文案；
- 控制图层；
- 做纯 ViewModel 派生。

前端不得重新实现：

- DSA；
- SMC；
- Momentum；
- ProductReadiness；
- canonical business state；
- 业务资格/权限逻辑。

## 14. Board Facts / Aggregation

pywencai（`wencai_board_provider`）是当前板块分类正式来源。

- 用户 API 请求链不得直接访问问财；
- 盘后由 after-close 正式步骤同步；
- 不得增加 akshare、代理/IP绕过、东方财富混用作为静默替代源；
- Industry 与 Concept 是不同 taxonomy，不能在聚合/展示中混为一个维度；
- Board Aggregation 必须追溯到同一 target-date Core。

## 15. After-Close

盘后主链必须尊重依赖：

`market facts → daily/core → board aggregation → review`

增强链可以异步：

`state events / chip / auction`

当前 PRD31 的详细 ownership、九节点定义和 readiness 以 `docs/prd/` 为准；规则只保留“不允许增强链反向污染 Core/Review identity”这一稳定不变量。

## 16. Atomic Facts / 已冻结合同

已有明确版本冻结的 Atomic Fact Core 或 schema contract 不得在普通实现中偷偷改变。

确需改变时必须先修改对应 PRD/契约并显式版本化，旧快照不能被新代码误读为同一 schema。
