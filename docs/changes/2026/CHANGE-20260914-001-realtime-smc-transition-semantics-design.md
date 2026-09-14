# CHANGE-20260914-001 — Realtime SMC Transition Semantics（Design Only）

状态：进行中（**DESIGN ONLY**；未写任何生产代码、未接 Monitor、未改 C3 contract）  
日期：2026-09-14  
类型：`contract`  
领域：盘中监控 / SMC realtime 语义 / 事件身份与幂等 / 价格坐标  
负责人：待填写

相关 PRD：

- 无（本阶段不新增、不修改 PRD）

相关 Maps：

- `../../maps/10-market-data.md`（**未修改**；实现落地并验收后再单独授权同步）

相关 Rules：

- `../../../AGENTS.md`（One semantic, one owner / Evidence must match the claim）

相关提交或 PR：

- 设计基线（C3 contract PASS）：`278a52a0135dc92315edc8f62b6aad09aa37b4d8`
- 前版设计（被本修订取代）：`91055608befc676b6896852842023af8b3703ed1`
- 本修订提交：以 Git 记录为准

替代：无  
被替代：无

> **修订记录（REOPEN correction）**：本版关闭三处会导致隐蔽 bug 的语义缺口——
> (1) `event_type` **移出** structure 幂等身份；(2) lane bias 冻结为**动态 realtime transition state**；
> (3) 冻结 **qfq 价格坐标一致性** 与 fail-closed。并据 **Git authoritative SMC core** 关闭原 Q1–Q5。

---

## 0. 本阶段边界（DESIGN ONLY）

本文件**只冻结语义**，不含任何实现承诺。允许写伪代码、状态机、表格；**禁止**在本轮产生生产代码。

`SEMANTICS_STATUS = FROZEN`（本文第 3–15 节为冻结口径，实现阶段不得自行 redesign）

### 0.1 Realtime 层 MAY 做

```text
- 消费 C3 已发布的 SmcMonitorTargetSet（引用 target，不自行发现 pivot / OB）
- 消费 canonical completed 1m stream（已按 qfq 坐标，见 §5）
- 维护 dynamic lane bias（current_swing_bias / current_internal_bias）
- 用冻结规则判定 BOS / CHoCH 的一次性状态转换
- 用冻结规则判定 OB 的 entry episode
- 以稳定 logical key 作为幂等键（配合 DB 唯一约束）
- 维护可重建的 runtime 状态（缓存 / 加速），不承担 correctness authority
- UI preview（未完成 1m / tick 的“提示”）——非 canonical event source
```

### 0.2 Realtime 层 MUST NOT 做

```text
- MUST NOT 修改 monitor_batch_service / monitor_crossing_service / watchlist realtime service
- MUST NOT 新增生产 scheduler / 接 G5 / 接 G6 / 真正发送通知（本阶段）
- MUST NOT 修改 C3 contract（smc_monitor_target_service）
- MUST NOT 自行重新发现 pivot / 重算结构位 / 重算 OB / 重算 mitigation
- MUST NOT 用 standalone 1m candle 冒充 daily candle
- MUST NOT 用 intrabar high/low 产生正式 BOS / CHoCH
- MUST NOT 给 BOS / CHoCH 增加 retest / episode 概念
- MUST NOT 用 arbitrary time cooldown 代替正确性
- MUST NOT 用“价格连线经过 OB”推断 touch
- MUST NOT 在 session 切换时制造假 retest
- MUST NOT 把 C3 target_id 当作跨 rebuild 的 global 通知幂等键
- MUST NOT 把 event_type 放进 structure 幂等身份
- MUST NOT 一直读 C3 初始 lane bias 当作当前 bias
- MUST NOT 用 raw 1m OHLC 直接与 qfq target level / OB zone 比较
- MUST NOT 在 adjustment context 缺失/过期/无法证明时 raw fallback（必须 fail closed）
- MUST NOT 在 realtime 层发明第二套 bullish_bar / bearish_bar / confluence 判据
```

---

## 1. 摘要

把盘中 SMC 的“状态转换”拆成两类**本质不同**的事件：

> **Structure（BOS / CHoCH）＝ 由「已完成 1m 收盘」确认的一次性状态转换，且依赖实时的 lane bias 演化。**
> **OB Touch/Retest ＝ 由「已完成 1m 的实际成交区间」驱动的可重复 entry episode。**

C3 `SmcMonitorTargetSet` 是 target 与 mitigation 的 authority；本层只负责“在正确的**价格坐标**与正确的**动态状态**上，按 core 的**同一语义**，产生正确的一次性事件”。

---

## 2. 背景与问题

原 realtime 路径（`607df2ed` / `74c0668f` / `896d2f0c`）以 one-shot crossing 为主，存在三类结构性风险：

1. **身份错配**：QD/XDXR rebuild 后同一经济意义的 structure 可能得到不同 `target_id`；若把 `target_id`（或把会变化的 `event_type`）当幂等键，会**双发**或漏发。
2. **状态漂移**：core 每次 crossing 后会修改 lane bias；若 realtime 一直读 C3 初始 bias，会把 CHoCH 错判成 BOS。
3. **坐标错配**：SMC 用 qfq；分钟数据 raw。若直接把 raw 1m close / high / low 与 qfq level / OB zone 比较，除权日会**算错**。

本设计冻结语义，关闭上述三类缺口。

---

## 3. 术语与事件分类（Event Taxonomy）

| 事件 | canonical 输入 | 触发形态 | 生命周期 | 可重复性 |
|---|---|---|---|---|
| BOS（structure） | completed 1m `close` | 收盘穿越 | PENDING → FIRED → TERMINAL | 一次性 |
| CHoCH（structure） | completed 1m `close` | 收盘穿越 | PENDING → FIRED → TERMINAL | 一次性 |
| Internal gate（bullish/bearish_bar + confluence） | 当日累计 OHLC（partial-daily candle） | 非事件，是 **gate** | 每根 completed 1m 可重算 | 状态量（不是通知） |
| OB Touch / Retest | completed 1m `high/low` | 区间相交 | OUTSIDE ↔ INSIDE_EPISODE | 可重复（每 entry episode 最多一次通知） |

**关键区分**

```text
event_type（BOS | CHoCH） = fire 时由 current lane bias 推出的【事件属性】，不是结构身份
```

- BOS / CHoCH 只来自 **structure**，无 retest、无 episode。
- OB 才有 episode；不属于 structure 家族。
- tick / 未完成 1m **不是** canonical event source。

---

## 4. 时间与输入合同（Completed-bar Timing Contract）

### 4.1 canonical 输入

```text
BOS / CHoCH       : completed 1m bars only（用 close）
Internal bar gate : 截至当前 completed 1m 的当日累计 OHLC
OB touch/retest   : completed 1m 的 high / low（实际成交区间）
```

### 4.2 crossing 定义（冻结）

```text
bullish:
    prev_completed_close <= target.level
    AND current_completed_close > target.level

bearish:
    prev_completed_close >= target.level
    AND current_completed_close < target.level
```

- **禁止**用 `high > level` 或 `low < level` 产生正式 BOS / CHoCH。
- 最多 **1 分钟确认延迟**：有意设计，非缺陷。

### 4.3 触发时点

```text
一根 1m 完成（bar close） → 计算 → 若满足 crossing → 立即产生一次事件
```

未完成 bar / tick：**不产生** canonical event。

---

## 5. 价格坐标与复权合同（qfq Coordinate Contract）— 冻结

### 5.1 单一坐标

SMC registry 明确 `adjustment_mode = qfq`。因此下列**必须处于同一个 qfq coordinate**：

```text
C3 target level（structure）
C3 OB bar_low / bar_high
realtime completed 1m OHLC
partial-daily candle OHLC
```

**禁止**：

```text
raw 1m close      vs  qfq target.level
raw 1m high/low   vs  qfq OB zone
```

### 5.2 转换 owner

realtime 必须使用仓库既有的 canonical intraday 复权路径（以权威**日线** factor 对分钟 bar 应用），**不信任分钟 bar 自带的 `adj_factor`**。SMC realtime **不得**自建第二套复权算法。

### 5.3 fail closed（冻结）

若出现任一情况：

```text
adjustment context missing
OR stale
OR XDXR freshness 无法证明
OR 与产生该 TargetSet 的 generation context 不一致（坐标不匹配）
```

则：

```text
structure events = 0
OB events = 0
fail closed（禁止 raw fallback）
```

物理 adjustment-context DTO 的位置留给 implementation design；**“坐标必须一致 + 无法证明则 fail closed”现在冻结**。

---

## 6. Session ownership 与 partial-daily candle

### 6.1 session owner（不在本层）

SMC realtime **不自己定义交易时钟**。

```text
session identity = canonical completed 1m bar 的 Asia/Shanghai business date
```

```text
哪些 bar 属于 canonical trading stream，由 market-data / session owner 决定
```

SMC semantics **不得硬编码**：

```text
09:30 / 11:30 / 13:00 / 15:00
```

### 6.2 partial-daily aggregation（冻结）

以同一 business date 的 canonical completed 1m 聚合：

```text
session_open  = 该 business date 第一根 canonical completed 1m 的 open
running_high  = 该 business date 截至当前的 max(high)
running_low   = 该 business date 截至当前的 min(low)
latest_close  = 当前 completed 1m 的 close
```

午休：

```text
不 reset
```

---

## 7. internal gate 精确语义（冻结，来自 core SSOT）

令 partial-daily candle：

```text
O = session_open
H = running_high
L = running_low
C = latest_completed_close
```

### 7.1 bullish_bar / bearish_bar

```text
若 internal_filter_confluence == False：
    bullish_bar = True
    bearish_bar = True

若 internal_filter_confluence == True：
    bullish_bar = (H - max(C, O)) > min(C, O - L)
    bearish_bar = (H - max(C, O)) < min(C, O - L)
```

> **不要在本 design 阶段“纠正”或美化该表达式。** 这是当前 core 实码 SSOT。
> Implementation 阶段优先提取 **shared pure helper**，让 daily core 与 realtime 共用同一 owner；禁止复制第二套公式。

### 7.2 internal confluence

```text
Internal HIGH candidate:
    internal_high.level != swing_high.level
    AND bullish_bar

Internal LOW candidate:
    internal_low.level != swing_low.level
    AND bearish_bar

Swing structure:
    extra_condition = True（无上述 level inequality gate）
```

### 7.3 unformed counterpart（冻结，极易错）

core 未形成 pivot 时为 `NaN`；`finite_internal_level != NaN` 语义为 **True**。

C3 把未形成 swing level 暴露为 `None`。realtime **必须保留同样效果**：

```text
None 不得被当作 “gate false”
finite internal level != None  → True（与 core 的 != NaN 等价）
```

---

## 8. 动态 lane bias 与 BOS / CHoCH 分类（冻结）

### 8.1 lane bias 是 realtime transition state

C3 `structure_context` 的 `swing_bias / internal_bias` **只作为 TargetSet 激活时的初始值**：

```text
TargetSet 激活
→ current_swing_bias  = C3 swing_bias
→ current_internal_bias = C3 internal_bias
```

之后由 realtime 按 core 规则演化。

### 8.2 分类与 bias 更新（严格复现 core）

```text
high pivot crossing（bullish 方向）：
    event_type = CHoCH  iff current_lane_bias == BEARISH(-1)
                 否则 BOS
    → current_lane_bias = BULLISH(+1)

low pivot crossing（bearish 方向）：
    event_type = CHoCH  iff current_lane_bias == BULLISH(+1)
                 否则 BOS
    → current_lane_bias = BEARISH(-1)
```

因此：

```text
bias == 0：
    high crossing → BOS
    low  crossing → BOS
```

（**不是** unresolved 项）

### 8.3 必须体现“不能一直读 C3 初始 bias”

```text
初始 internal bias = 0

上午：internal high crossing
      → BOS
      → internal bias = +1

下午：internal low crossing
      → 此时 current bias = +1 → CHoCH
      → internal bias = -1
```

### 8.4 持久化要求

`current_swing_bias` / `current_internal_bias` 必须能在 restart 后正确恢复：

```text
persist authoritative transition state
   OR
deterministic replay of persisted fired transitions
```

**不得**只存在进程内 cache。

---

## 9. Structure 状态机（BOS / CHoCH）

### 9.1 状态

```text
PENDING
   │  qualifying completed-1m close crossing
   ▼
FIRED
   ▼
TERMINAL
```

- 同一个 **logical structure（structure_transition_key）** 一生最多产生一次转换。
- pivot 的 `crossed` 一生只能从 `False → True` 一次。
- **不存在** BOS retest / CHoCH retest episode。
- **不使用**时间 cooldown 作为 correctness 手段。

### 9.2 Gap crossing（跨交易日）

允许：

```text
上一交易日最后一根 completed close
→ 当前交易日第一根 completed 1m close
```

直接构成 crossing。但：

```text
盘前 / 未完成 tick 的一瞬间超过 level → 不产生正式 event
```

---

## 10. OB Touch / Retest Episode

### 10.1 touch 判定（冻结）

```text
ob zone    = [bar_low, bar_high]
current 1m = [minute_low, minute_high]

touch ⇔ (minute_high >= bar_low) AND (minute_low <= bar_high)
```

### 10.2 episode 状态机（冻结）

```text
OUTSIDE
   │ first intersect
   ▼
INSIDE_EPISODE        → notify once

INSIDE_EPISODE
   │ 连续仍 intersect
   ▼
INSIDE_EPISODE        → no duplicate

INSIDE_EPISODE
   │ completed bar 不再 intersect
   ▼
OUTSIDE               → episode closed

OUTSIDE
   │ 再次 intersect
   ▼
INSIDE_EPISODE        → retest; notify once
```

> **一次 entry = 一个 episode = 最多一次通知。**

- **禁止**每根位于 OB 内的 1m 重复通知。
- **禁止**用 arbitrary time cooldown 代替 episode state。

### 10.3 session boundary（冻结）

- 交易日切换**不自动关闭** OB episode。
- `上一 session inside → 下一 session 第一根仍 intersect` = **同一 episode**。
- 只有 `inside → outside → inside` 才是新 episode。
- overnight gap 若无任何**实际 completed 1m OHLC** 与 zone 相交 → **不算 touch**。

### 10.4 OB mitigation authority（冻结）

```text
realtime 不重新计算 mitigation
C3 TargetSet 为 authority：
    target 仍存在          → 继续监控
    下一版 TargetSet 中消失 → 该 OB realtime state = TERMINAL
```

---

## 11. 身份分层（Identity Layering）

**核心原则：correctness 幂等身份 ≠ 事件属性 ≠ target instance 引用。**

### 11.1 C3 `target_id`

```text
用途 = 当前 TargetSet 中具体 target instance 的引用
Realtime 目标来源 = C3 TargetSet（必须引用它，不得自行重新发现 pivot / OB）
```

**不得**作为跨 rebuild 的 global notification 幂等键（含 level/params/contract version，rebuild 后会变）。

### 11.2 Structure transition identity（冻结，**不含 event_type**）

```text
structure_transition_key =
    instrument_id
    + lane            (swing | internal)
    + kind            (high | low)
    + anchor_time
```

```text
event_type = BOS | CHoCH
→ fire 时由 current lane bias 推出的【事件属性】
→ 可作为 fired event 的 payload / audit metadata
→ 绝不进入 structure_transition_key
```

理由：`event_type` 会因分类路径/恢复路径不同而变化；若进入 unique key，同一 crossing 会产生两个 DB key → **双发**。

**禁止**在 `structure_transition_key` 中加入：

```text
level / qfq price / anchor_index / target_id / target_set_version / event_type / runtime event minute
```

### 11.3 OB identity（冻结）

```text
logical_ob_key =
    instrument_id
    + internal
    + bias
    + anchor_time
    + confirmed_time

ob_episode_key =
    logical_ob_key
    + entry_completed_bar_time
```

`sequence` 可作 persistence/audit 字段，**不得**成为唯一 correctness dedupe key。

保证：

```text
retry / replay same entry bar  → same ob_episode_key
outside → later inside         → 新 entry_completed_bar_time → 新 ob_episode_key
```

---

## 12. Realtime Evaluation Context 与 params 绑定（冻结）

Realtime 必须知道 `internal_filter_confluence` 的真实有效值，但 TargetSet 只保存 `params_hash`。

因此引入概念合同：

```text
RealtimeSmcEvaluationContext
    - effective SMC params（或规范化语义字段，至少含 internal_filter_confluence）
    - canonical adjustment context（见 §5）
    - TargetSet（target 引用）
    - current dynamic lane bias state（见 §8）
```

进入 evaluation 前必须证明：

```text
canonical_hash(effective_params)
    ==
TargetSet.contract_identity.params_hash
```

不一致：

```text
fail closed
0 SMC events
禁止偷偷使用 DEFAULT_PARAMS
```

物理 DTO / 类名位置留给 implementation design；**语义要求现在冻结**。

---

## 13. 去重所有权与持久化状态要求

### 13.1 去重所有权

```text
dedupe authority = 稳定 logical key + DB 唯一约束
runtime 状态      = 可重建缓存（不承担 correctness）
```

### 13.2 Structure 状态（语义要求）

```text
structure_transition_key        （§11.2，不含 event_type）
current state ∈ {PENDING, FIRED, TERMINAL}
current lane bias（该 lane 的 current_*_bias）
event_type after fire ∈ {BOS, CHoCH}     （payload/audit）
confirmed completed-bar time
snapshot 引用：target_id + target_contract_schema_version
input 引用：daily_bars_hash
algorithm/contract identity（registry）
```

### 13.3 OB 状态（语义要求）

```text
ob_episode_key（§11.3）
logical_ob_key
state ∈ {OUTSIDE, INSIDE_EPISODE}
notified_this_episode（bool）
entry_completed_bar_time（当前 episode）
sequence（诊断）
last_seen_bar_time
snapshot 引用：target_id + target_contract_schema_version
```

物理表结构 / migration / 字段命名：**本设计不决定**（`IMPLEMENTATION DESIGN REQUIRED`）。

---

## 14. 重启 / rebuild / XDXR / epoch 语义

### 14.1 重启

```text
runtime 缓存丢弃
→ current lane bias 与 state 从权威持久化状态恢复，或由已持久化的 fired transitions deterministic replay
→ 不重复通知（structure_transition_key / ob_episode_key + DB 唯一约束）
```

### 14.2 TargetSet rebuild

```text
Structure：structure_transition_key 不变 ⇒ 已 FIRED/TERMINAL 不复位
OB：logical_ob_key 不变 ⇒ episode 延续（不因 rebuild 制造假 retest）
OB 在新 TargetSet 中消失 ⇒ 该 OB state = TERMINAL
target_id 因 qfq/XDXR/rebuild 变化 ⇒ 不产生第二条通知（dedupe 用 logical key）
```

### 14.3 epoch（冻结）

```text
同一 daily-state epoch 内 rebuild
    → preserve / replay intraday lane bias evolution

新的 authoritative completed-daily epoch
    → 用新的 C3 structure_context 初始化下一 epoch 的 base bias
```

**禁止**用 `target_set_version` 变化本身判断 epoch 重置（XDXR / rebuild 也会改变 version）。

---

## 15. 失败与 fail-closed 语义

```text
缺少 required completed 1m 数据          → 本轮不产生 event（hold），不得合成/推断
1m 缺失导致 crossing 连续性不可判        → 不产生 event（fail closed），记 unhealthy
adjustment context 缺失/过期/无法证明     → structure=0 & OB=0（§5.3）
effective_params 与 params_hash 不一致    → 全量 fail closed（§12）
TargetSet 不可用 / fail-closed           → 不进入监控（不退回 legacy 近似路径）
输入 malformed                           → 不产生 event
重复处理同一根 completed 1m               → 幂等：不产生第二条通知
session 边界且无实际 1m OHLC              → 不产生 touch（§10.3）
未知 / 不明确                             → 宁可 not-fire，不得 guess-fire
```

原则：**宁可不报，不可错报**（no false-green）。

---

## 16. 示例与边界用例表

| # | 场景 | 期望 |
|---|---|---|
| 1 | 价格 wick 刺破 level 但收盘未过 | **不产生** BOS/CHoCH（close-based） |
| 2 | 上一根 close ≤ level，当前 close > level（bullish） | 产生一次事件，进入 FIRED |
| 3 | 同一 structure 再次收盘穿越 | **不产生**新事件（已 FIRED/TERMINAL） |
| 4 | 进程在 FIRED 后重启，再遇同结构 | 恢复 TERMINAL，**不重复通知** |
| 5 | 高开跳过 level，第一根 1m 收完仍在上方 | 允许 gap crossing → 产生一次事件 |
| 6 | 盘前瞬间报价超过 level（无 completed 1m） | **不产生**事件 |
| 7 | OB 首次 intersect | 进入 INSIDE_EPISODE，通知一次 |
| 8 | OB 连续 10 根 1m 在 zone 内 | 只在首次通知 |
| 9 | OB 离开后再次进入 | 新 episode → 再通知一次 |
| 10 | OB 周一收盘在 zone 内，周二第一根仍在 | **同一 episode**，无假 retest |
| 11 | OB 隔夜 gap 跳过 zone，无实际 1m OHLC 相交 | **不算 touch** |
| 12 | OB：XDXR rebuild 后 target_id 变化但 logical_ob_key 不变 | episode 延续，无假 retest |
| 13 | OB：新 TargetSet 中该 OB 消失 | state → TERMINAL |
| 14 | internal gate：盘中用 partial-daily candle 计算 | 用累计 OHLC，不用单根 1m |
| 15 | 缺一根 1m 导致连续性不可判 | 不产生事件，记 unhealthy |
| **A** | `initial bias = 0` → high fire **BOS** → bias=+1；同日 later low fire → **CHoCH** → bias=-1 | 分类随 dynamic bias 变化；**不得**一直读 C3 初始 bias |
| **B** | `internal_filter_confluence == False` | internal crossing **不受 candle gate 阻挡**（bullish_bar=bearish_bar=True） |
| **C** | internal 对应 swing counterpart **unformed**（C3 为 `None`） | level inequality gate **按 core 语义通过**（`None` 不得当 gate-false） |
| **D** | raw 1m 可用但 qfq adjustment context **unavailable** | **no fire**（structure=0 且 OB=0，禁 raw fallback） |
| **E** | 同一 structure 被 replay，一次算 BOS、一次错误路径算 CHoCH | `structure_transition_key` 相同 → DB correctness 层**不得**产生第二个 event |
| **F** | OB same entry bar replay | `ob_episode_key` 相同 → 不重复通知 |

---

## 17. 仅剩的 implementation-design 物理问题（非业务语义）

以下不影响语义正确性，允许留到 implementation 阶段决定：

```text
- persistence 表 / 字段布局 / migration
- RealtimeSmcEvaluationContext 的物理 DTO / 类位置
- adjustment context 如何传递与校验
- shared pure helper（bullish_bar/bearish_bar/confluence）放在哪个模块与提取方式
- runtime state 的缓存实现形态
```

> 原 Q1–Q5 已由 **Git authoritative SMC core / registry** 关闭，不再列为 unresolved：
> Q1 bullish/bearish_bar 公式 → §7.1；Q2 bias==0 → §8.2；Q3 internal confluence → §7.2；
> Q4 session 时钟 → §6（继承 market-data/session owner）；Q5 价格坐标 → §5（qfq + fail closed）。

---

## 18. Change 常规字段

### 18.1 影响范围

- 用户行为：无（本阶段不产生任何通知 / 不接线）。
- API 或契约：新增 **realtime 语义契约**（本文）；不修改 C3 contract。
- 数据：无变更（不建表、不 migration、不写 DB）。
- 前端：无。
- 后端：**本轮零生产代码**。
- Worker 与任务：无新增 scheduler。
- 部署与运行：无。

### 18.2 迁移与兼容

无（docs-only；无 Migration、无回填、无重算、无兼容路径切换）。

### 18.3 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 文档落位与命名符合 `docs/changes` 约定 | docs | PASS | 本文件 + INDEX 登记 |
| 仅新增/修改设计文档，零生产代码改动 | repo | 待审 | Git commit diff（审查方直查） |
| event_type 已移出 structure_transition_key | docs | PASS | §11.2 / §13.2 / 用例 E |
| dynamic lane bias 已冻结为 realtime state | docs | PASS | §8 / 用例 A |
| qfq 坐标一致性 + fail closed 已冻结 | docs | PASS | §5 / 用例 D |
| 原 Q1–Q5 已据 core 关闭 | docs | PASS | §7 / §8 / §6 / §5 / §17 |
| C3 contract 未被触碰 | repo | PASS | C3 分支 tip `278a52a0` 未变 |

### 18.4 回滚方案

删除本 Change 文档 + 撤销 INDEX 登记即可（docs-only，无运行影响）。

### 18.5 遗留问题与风险

- 三个隐蔽点（dynamic bias / structure identity / qfq coordinate）若不冻结，即使测试很多也易出现“能跑但**类型错、除权日错、重启重复发**”。本版已冻结。
- 1 分钟确认延迟：设计取舍，验收口径中应明确为“非缺陷”。
- shared pure helper 若不提取，存在 daily core 与 realtime **公式漂移**风险（§17）。

### 18.6 后续变化

- 得到 `REALTIME SMC SEMANTICS DESIGN PASS` 后，才允许创建 **implementation** Change（接线 monitor / crossing / 持久化 / 通知 / 复权上下文传递）。
- 本 Change 不覆盖后续生产接线（`607df2ed` / `74c0668f` / `896d2f0c`）的独立门禁。

---

## 19. 本轮 Git 纪律

```text
只新增/修改设计文档 → commit → push 到 verify/realtime-smc-semantics-design → 报告 exact 40-char SHA → 停止
不实现任何生产代码
```

审查通过（`REALTIME SMC SEMANTICS DESIGN PASS`）后，才允许进入 implementation。
