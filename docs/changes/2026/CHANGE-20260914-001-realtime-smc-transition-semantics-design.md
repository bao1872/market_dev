# CHANGE-20260914-001 — Realtime SMC Transition Semantics（Design Only）

状态：进行中（**DESIGN ONLY**；未写任何生产代码、未接 Monitor、未改 C3 contract）  
日期：2026-09-14  
类型：`contract`  
领域：盘中监控 / SMC realtime 语义 / 事件身份与幂等  
负责人：待填写

相关 PRD：

- 无（本阶段不新增、不修改 PRD）

相关 Maps：

- `../../maps/10-market-data.md`（**未修改**；实现落地并验收后再单独授权同步）

相关 Rules：

- `../../../AGENTS.md`（One semantic, one owner / Evidence must match the claim）

相关提交或 PR：

- 设计基线（C3 contract PASS）：`278a52a0135dc92315edc8f62b6aad09aa37b4d8`（分支 `verify/c3-smc-target-contract`）
- 本设计文档提交：以 Git 记录为准（commit 后回填）

替代：无  
被替代：无

---

## 0. 本阶段边界（DESIGN ONLY）

本文件**只冻结语义**，不含任何实现承诺。允许写伪代码、状态机、表格；**禁止**在本轮产生生产代码。

`SEMANTICS_STATUS = FROZEN`（本文第 3–11 节为冻结口径，实现阶段不得自行 redesign）

### 0.1 Realtime 层 MAY 做

```text
- 消费 C3 已发布的 SmcMonitorTargetSet（target 引用，不自行发现 pivot / OB）
- 读取已完成 1m bar（completed 1m）与当前交易日累计 OHLC
- 用冻结规则判定 BOS / CHoCH 的一次性状态转换
- 用冻结规则判定 OB 的 entry episode
- 以稳定 logical event key 作为幂等键（配合 DB 唯一约束）
- 维护可重建的 runtime 状态（缓存 / 加速），不承担 correctness authority
- UI preview（未完成 1m / tick 的“提示”）——非 canonical event source
```

### 0.2 Realtime 层 MUST NOT 做

```text
- MUST NOT 修改 monitor_batch_service / monitor_crossing_service / watchlist realtime service
- MUST NOT 新增生产 scheduler / 接 G5 / 接 G6 / 真正发送通知（本阶段）
- MUST NOT 修改 C3 contract（smc_monitor_target_service）语义
- MUST NOT 自行重新发现 pivot / 重新计算结构位 / 重新计算 OB / 重新计算 mitigation
- MUST NOT 用 standalone 1m candle 冒充 daily candle
- MUST NOT 用 intrabar high/low 产生正式 BOS / CHoCH
- MUST NOT 给 BOS / CHoCH 增加 retest / episode 概念
- MUST NOT 用 arbitrary time cooldown 代替正确性（幂等靠 logical key + DB 唯一约束）
- MUST NOT 用“价格连线经过 OB”推断 touch
- MUST NOT 在 session 切换时制造假 retest
- MUST NOT 把 C3 target_id 当作跨 rebuild 的 global notification 幂等键
- MUST NOT 在 realtime 层发明第二套 bullish_bar / bearish_bar 近似判据
```

---

## 1. 摘要

把盘中 SMC 的“状态转换”拆成两类**本质不同**的事件，并各自给出冻结语义：

> **Structure（BOS / CHoCH）＝ 由「已完成 1m 收盘」确认的一次性状态转换。**
> **OB Touch/Retest ＝ 由「已完成 1m 的实际成交区间」驱动的可重复 entry episode。**

因此**不再存在**一个通用的 “crossing / retest 引擎” 把二者硬塞到一起。C3 `SmcMonitorTargetSet` 是 target 与 mitigation 的 authority；本层只负责“在正确的输入上，按正确的规则，产生正确的一次性事件”。

---

## 2. 背景与问题

变化前的 realtime 路径（`607df2ed` / `74c0668f` / `896d2f0c`）以 one-shot crossing 为主，存在三类结构性风险：

1. **语义混装风险**：structure crossing 与 OB touch 被当成同一种“穿越/retest”，导致 BOS/CHoCH 出现 retest 语义，或 OB 每根 1m 重复通知。
2. **输入粒度错配风险**：daily core 用**完整 candle** 判定 `bullish_bar / bearish_bar`；盘中若用一个 1m candle 直接冒充 daily candle，等于偷偷改变语义。
3. **身份错配风险**：QD/XDXR rebuild 后同一经济意义的 structure 可能得到不同 `target_id`；若把 `target_id` 当通知幂等键，会重复通知或漏通知。

本设计**只冻结语义**，为后续实现建立单一 SSOT。

---

## 3. 术语与事件分类（Event Taxonomy）

| 事件 | canonical 输入 | 触发形态 | 生命周期 | 可重复性 |
|---|---|---|---|---|
| BOS（structure） | completed 1m `close` | 收盘穿越 | PENDING → FIRED → TERMINAL | 一次性（每个 logical structure 一生最多一次） |
| CHoCH（structure） | completed 1m `close` | 收盘穿越 | PENDING → FIRED → TERMINAL | 一次性 |
| Internal bullish/bearish gate | 当日累计 OHLC（partial-daily candle） | 非事件，是**gate** | 每根 completed 1m 可重算 | 状态量（不是通知） |
| OB Touch / Retest（order block） | completed 1m `high/low` | 区间相交 | OUTSIDE ↔ INSIDE_EPISODE | 可重复（每个 entry episode 最多一次通知） |

**分类要点**

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
- 理由：wick 瞬间刺破不是结构突破；且 daily core 的 structure crossing 语义本身也是 close-based。
- 后果：最多 **1 分钟确认延迟**。这是**有意设计**，不是缺陷。

### 4.3 触发时点

```text
一根 1m 完成（bar close） → 计算 → 若满足 crossing → 立即产生一次事件
```

未完成 bar / tick：**不产生** canonical event。

---

## 5. Session 累积日线与 internal gate

### 5.1 partial-daily candle（冻结构造）

每根 completed 1m 后，构造“截至当前时刻的 partial daily candle”：

```text
session_open     = 当天第一根 1m 的 open
running_high     = 当天截至当前 completed 1m 的最高 high
running_low      = 当天截至当前 completed 1m 的最低 low
latest_close     = 当前 completed 1m 的 close
```

### 5.2 internal bullish_bar / bearish_bar

- **不得**拿独立 1m candle 替代 daily candle。
- 用上述 partial-daily candle 复现 SMC core **已有的** `bullish_bar / bearish_bar` 语义。
- **single semantic definition**：realtime 层不得发明另一套近似公式。
- 具体判定式以 core 为 SSOT（见第 13 节待确认项）。

---

## 6. Structure 状态机（BOS / CHoCH）

### 6.1 状态

```text
PENDING
   │  qualifying completed-1m close crossing
   ▼
FIRED
   ▼
TERMINAL
```

- 同一个 **logical structure** 一生最多产生一次 BOS / CHoCH。
- **不存在** BOS retest / CHoCH retest episode。
- **不使用**时间 cooldown 作为 correctness 手段。

### 6.2 retry / restart / rebuild

```text
correctness 由 稳定 logical event key + DB 唯一约束 保证
runtime 状态仅是可重建缓存
```

### 6.3 BOS / CHoCH 分类

分类必须与 **daily core 的 BOS/CHoCH 判定语义一致**（SSOT = core），realtime 不另立标准。**工作定义（须与 core byte 对齐）**：

```text
lane ∈ {swing, internal}
lane bias = C3 structure_context 的 swing_bias / internal_bias ∈ {-1, 0, 1}

方向一致突破（up bias 破 high / down bias 破 low） → BOS（延续）
方向相反突破（up bias 破 low / down bias 破 high） → CHoCH（性质改变）
bias == 0 的边界                                  → 见第 13 节（不得由 realtime 自行决定）
```

### 6.4 Gap crossing（跨交易日）

允许：

```text
上一交易日最后一根 completed close
→ 当前交易日第一根 completed 1m close
```

直接构成 crossing（因此高开跳过结构位后，第一根 1m 收完可产生 BOS / CHoCH）。

但：

```text
盘前 / 未完成 tick 的一瞬间超过 level  → 不产生正式 event
```

---

## 7. OB Touch / Retest Episode

### 7.1 touch 判定（冻结）

```text
ob zone        = [bar_low, bar_high]
current 1m     = [minute_low, minute_high]

touch ⇔ (minute_high >= bar_low) AND (minute_low <= bar_high)
```

### 7.2 episode 状态机（冻结）

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

### 7.3 session boundary（冻结）

- 交易日切换**不自动关闭** OB episode。
- `上一 session inside → 下一 session 第一根仍 intersect` = **同一 episode**，不产生假 retest。
- 只有 `inside → outside → inside` 才是新 episode。
- overnight gap 若无任何**实际 completed 1m OHLC** 与 zone 相交 → **不算 touch**（禁止用价格连线推断成交）。

### 7.4 OB mitigation authority（冻结）

```text
realtime 不重新计算 mitigation
C3 TargetSet 为 authority：
    target 仍存在          → 继续监控 touch/retest
    下一版 TargetSet 中消失 → 该 OB realtime state = TERMINAL
```

---

## 8. 身份分层（Identity Layering）

**核心原则：target identity 与 notification identity 是两个不同职责，必须分开。**

### 8.1 C3 `target_id`

用途：**当前 TargetSet 中具体 target instance 的引用**。

```text
Realtime 目标来源 = C3 TargetSet（必须引用 target_id，不得自行重新发现 pivot / OB）
```

但：

```text
target_id 包含 level / params / contract version 等
QD/XDXR rebuild 后同一经济意义的 structure 可能拥有不同 target_id
⇒ target_id 不可作为跨 rebuild 的 global notification 幂等键
```

### 8.2 Structure logical event identity

```text
instrument_id
+ lane            (swing | internal)
+ kind            (high | low)
+ event_type      (BOS | CHoCH)
+ anchor_time
```

**禁止**加入：

```text
level / qfq price / anchor_index / target_id / target_set_version / runtime event minute
```

### 8.3 OB logical identity

```text
instrument_id
+ internal
+ bias
+ anchor_time
+ confirmed_time
```

OB **episode identity** = OB logical identity + **entry episode 边界**（第 7 节 inside→outside→inside 的新 entry 边界）。

当前 snapshot 仍同时保存 `target_id`（引用用途），二者并存。

---

## 9. 去重所有权与持久化状态要求

### 9.1 去重所有权

```text
dedupe authority = 稳定 logical event key + DB 唯一约束
runtime 状态      = 可重建缓存（进程重启后从权威状态恢复，不承担 correctness）
```

### 9.2 必须持久化的状态（语义要求）

Structure event：

```text
logical event key（第 8.2 节）
state ∈ {PENDING, FIRED, TERMINAL}
fired_at_bar_time（触发那根 completed 1m 的 bar_time）
snapshot 引用：target_id + target_contract_schema_version
input 引用：daily_bars_hash（与产生该 TargetSet 的输入一致）
algorithm/contract identity（registry 提供）
```

OB episode：

```text
logical OB identity（第 8.3 节）
state ∈ {OUTSIDE, INSIDE_EPISODE}
current/closed episode 边界（entry 的 bar_time + sequence）
notified_this_episode（bool）
last_seen_bar_time
snapshot 引用：target_id + target_contract_schema_version
```

物理表结构 / migration / 字段命名：**本设计不决定**（`IMPLEMENTATION DESIGN REQUIRED`，须单独门禁）。

---

## 10. 重启 / rebuild / XDXR 行为

```text
进程重启
  → runtime 缓存丢弃
  → 从权威持久化状态 + 当前 TargetSet 重建
  → 不重复通知（logical key + DB 唯一约束）

TargetSet 因新 bar / XDXR / rebuild 更新
  → Structure：logical key 不变 ⇒ 已 FIRED/TERMINAL 的不复位（同一 logical structure 一生一次）
  → OB：logical OB identity 不变 ⇒ episode 状态延续（不因 rebuild 制造假 retest）
  → 某 OB 在新 TargetSet 中消失 ⇒ 该 OB realtime state = TERMINAL

同一次经济事件在 rebuild 前后 target_id 变化
  → 不产生第二条通知（因为 dedupe 用 logical key，而非 target_id）
```

---

## 11. 失败与 fail-closed 语义

```text
缺少 required completed 1m 数据        → 本轮不产生 event（hold），不得合成/推断
某根 1m 缺失导致无法判定连续 crossing   → 不产生 event（fail closed），记录 unhealthy
TargetSet 不可用 / fail-closed         → 不进入监控（不退回 legacy 近似路径）
输入 malformed（价格非有限等）          → 不产生 event
重复处理同一根 completed 1m             → 幂等：不产生第二条通知
session 边界且无实际 1m OHLC            → 不产生 touch（第 7.3 节）
未知 / 不明确情形                        → 宁可 not-fire，不得 guess-fire
```

原则：**宁可不报，不可错报**（no false-green）。

---

## 12. 示例与边界用例表

| # | 场景 | 期望 |
|---|---|---|
| 1 | 价格 wick 刺破 level 但收盘未过 | **不产生** BOS/CHoCH（close-based） |
| 2 | 上一根 close ≤ level，当前 close > level（bullish） | 产生一次 BOS/CHoCH，进入 FIRED |
| 3 | 同一 structure 再次收盘穿越 | **不产生**新事件（已 FIRED/TERMINAL） |
| 4 | 进程在 FIRED 后重启，再遇到同结构 | 从 DB 恢复 TERMINAL，**不重复通知** |
| 5 | 高开直接跳过 level，第一根 1m 收完仍在上方 | 允许构成 gap crossing → 产生一次事件 |
| 6 | 盘前瞬间报价超过 level（无 completed 1m） | **不产生**事件 |
| 7 | OB：某 1m `[minute_low, minute_high]` 与 zone 相交（首次） | 进入 INSIDE_EPISODE，通知一次 |
| 8 | OB：连续 10 根 1m 都在 zone 内 | 只在首次通知（episode 内不重复） |
| 9 | OB：离开 zone 后再次进入 | 新 episode → 再通知一次（retest） |
| 10 | OB：周一收盘在 zone 内，周二第一根仍在 zone 内 | **同一 episode**，不产生假 retest |
| 11 | OB：隔夜 gap 从 zone 一侧跳到另一侧，期间无任何 1m OHLC 相交 | **不算 touch** |
| 12 | OB：XDXR rebuild 后 target_id 变化但 OB logical identity 不变 | episode 延续，不产生假 retest |
| 13 | OB：新 TargetSet 中该 OB 消失 | 该 OB state → TERMINAL |
| 14 | internal gate：盘中 10:31 用 partial-daily candle 计算 bullish/bearish | 用累计 OHLC，不用单根 1m |
| 15 | 缺一根 1m 导致 crossing 连续性不可判 | 不产生事件，记 unhealthy |

---

## 13. 待 core 权威确认项（realtime 层不得自行决定）

以下必须与 SMC core / C3 对齐后才能进入实现，本设计**不擅自冻结**：

```text
Q1  bullish_bar / bearish_bar 的 exact 判定式（以 core 为 SSOT）
Q2  BOS / CHoCH 分类在 lane bias == 0 时的归属（不得由 realtime 决定）
Q3  internal confluence 的 exact 条件（内部 gate 与 crossing 的联合语义，以 core 为准）
Q4  session 起止定义（交易日边界来源，须与行情/盘后既有 owner 一致）
Q5  level 比较使用的价格基准（须与 C3 TargetSet 的 level 语义一致，不引入第二套复权口径）
```

> 上述任一未定，实现阶段必须**先回到 core/契约确认**，不得在 realtime 层“猜一个合理公式”。

---

## 14. Change 常规字段

### 14.1 影响范围

- 用户行为：无（本阶段不产生任何通知 / 不接线）。
- API 或契约：新增 **realtime 语义契约**（本文）；不修改 C3 contract。
- 数据：无变更（不建表、不 migration、不写 DB）。
- 前端：无。
- 后端：**本轮零生产代码**。
- Worker 与任务：无新增 scheduler。
- 部署与运行：无。

### 14.2 迁移与兼容

无（docs-only；无 Migration、无回填、无重算、无兼容路径切换）。

### 14.3 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 文档落位与命名符合 `docs/changes` 约定 | docs | PASS | 本文件 + INDEX 登记 |
| 仅新增/修改设计文档，零生产代码改动 | repo | 待审 | Git commit diff（审查方直查） |
| 与冻结口径逐条一致（无自行 redesign） | docs | 待审 | 审查方比对聊天冻结口径 |
| C3 contract 未被触碰 | repo | PASS | C3 分支 tip `278a52a0` 未变 |

### 14.4 回滚方案

删除本 Change 文档 + 撤销 INDEX 登记即可（docs-only，无运行影响）。

### 14.5 遗留问题与风险

- 第 13 节待确认项：若不先冻结，实现阶段有再次“语义漂移”的风险。
- 1 分钟确认延迟：是设计取舍，需在验收口径中明确为“非缺陷”。

### 14.6 后续变化

- 得到 `REALTIME SMC SEMANTICS DESIGN PASS` 后，才允许创建 **implementation** Change（接线 monitor / crossing / 持久化 / 通知）。
- 本 Change 不覆盖后续生产接线（`607df2ed` / `74c0668f` / `896d2f0c`）的独立门禁。

---

## 15. 本轮 Git 纪律

```text
只新增/修改设计文档 → commit → push 到独立 verification branch → 报告 exact 40-char SHA → 停止
不实现任何生产代码
```

审查通过（`REALTIME SMC SEMANTICS DESIGN PASS`）后，才允许进入 implementation。
