# 93 — E2.1 P1-C: ADMISSION_BOOTSTRAP_CONTRACT

## 0. 本文要回答的问题

Steady-state 设计已经确认：PostgreSQL **singleton admission owner**，worker claim
在**同一个事务**内锁同一行。

但 steady-state 设计有一个自举盲区：

> 新 table + 新 worker **不能保护"部署它们自己的那次部署"**。

因为旧 production worker 既不读取 admission owner，其数据库里也还没有那张表。
在 candidate worker 接管之前，旧 worker 仍能 `queued → running` 领取任务。

本文定义两种运行模式，闭合这个盲区。

---

## 1. 两种运行模式

| | MODE A — BOOTSTRAP | MODE B — STEADY STATE |
|---|---|---|
| 适用 | 第一次把 admission control 部署到 production | 此后所有正常部署 |
| 旧 runtime 是否支持 admission | 否 | 是 |
| 关闭 pickup 的手段 | 复用已有 graceful drain（SIGTERM） | admission owner row |
| 可证明的边界 | `OLD_WORKER_PICKUP_DISABLED=TRUE` | `PAUSE ACTIVE`（机器确认） |
| 是否需要 migration | 是（建表 + 置 PAUSED） | 否 |

两者服务于**同一个** E2.1 安全目标，不是两个 Phase。

---

## 2. MODE A — BOOTSTRAP 顺序（强制）

```
1. 利用当前正式能力关闭 old worker pickup
   - docker compose stop <worker-after-close>   # SIGTERM，非 kill -9
   - 已有 running job：自然完成，不 kill / 不 reset / 不改写 queue

2. 等待并机器验证 drain boundary
   - running(after_close_orchestrator) == 0
   - old worker 进程不再 accepting work
   - 若超时仍有 running：继续等待；绝不以 kill 换取推进

3. formal migration（Alembic，禁止裸 SQL 建表）
   - 创建 admission control table
   - 插入 singleton row
   - 置 PAUSED = true

4. 启动 candidate runtime
   - candidate worker 启动后第一件事就是读 admission row
   - 此时 row 已存在且 PAUSED → 不会 pickup

5. 机器确认
   - OLD_WORKER_PICKUP_DISABLED = TRUE
   - RUNNING_CONFLICT_COUNT = 0
   - PAUSE ACTIVE = TRUE
```

**关键点**：candidate pickup 恢复（步骤 4）必须发生在 admission 安装（步骤 3）**之后**。
两者顺序颠倒则自举失败。

---

## 3. MODE A — BOOTSTRAP RACE

场景：operator 发出 graceful drain 时，worker 恰好准备 claim。

允许：

```
worker 先 claim
  → job 成为 running
  → drain 让该 run 自然完成
  → worker exit
  → deployment 等待
```

禁止：

```
deployment 继续 mutation
而 worker 仍可能 pickup
```

因此 drain 不是"发出 SIGTERM 就认为完成"，而必须**观测**到
`running == 0` 且 worker 不再 accepting work。

最终 bootstrap boundary 必须同时成立：

```
OLD_WORKER_PICKUP_DISABLED = TRUE
RUNNING_CONFLICT_COUNT     = 0
```

---

## 4. MODE B — STEADY STATE 顺序

```
resolve exact rollback owner
  → acquire deployment pause（带 ownership token）
  → 机器确认 PAUSE ACTIVE
  → 观测 running
      running > 0 → BLOCK，等待，不杀
  → secondary pre-mutation gate（紧邻第一次真实 runtime mutation）
      admission paused
      pause ownership valid
      running conflicts = 0
  → runtime mutation
```

PAUSE ACTIVE 且 admission closure 已被机器证明后，
`queued / resume_queued` **允许原样留在 queue** —— 它们已不可能被 claim。
不要求清空，不删除、不重写 queue。

---

## 5. Linearization boundary（两种模式共用同一判据）

admission 状态必须与 claim 共享**同一个 ownership domain**：
worker claim 用 `SELECT ... FOR UPDATE SKIP LOCKED`（PostgreSQL 事务内），
因此 admission 判定必须在**同一事务**内、以 `FOR UPDATE` 锁住 singleton row。

谁先拿到行锁，谁就 linearize 在先：

```
PAUSE 先赢锁  → worker 随后读到 paused → 不能 claim
WORKER 先赢锁 → job commit 为 running → pause 随后 commit
              → secondary gate 看到 running → 部署被阻止
```

禁止：

```
transaction A: read paused
transaction B: claim
```

这留下 check→claim 的 TOCTOU。也禁止仅用 Redis flag + 独立 PostgreSQL claim，
除非能证明跨系统 race 已真正关闭。sleep-based 时序测试不构成证明。

---

## 6. 为什么需要新的 persistence owner

现有 schema 无法满足 atomic admission invariant，原因：

- `scheduler_job_runs` 是**每行一个 job** 的表；admission 需要的是
  一个 **singleton control record**，而不是"给每个 job 加一列"。
  给 job 行加列无法表达"全局暂停"这一状态，也无法在 claim 前被原子锁定。
- pause 必须能与 `SELECT ... FOR UPDATE SKIP LOCKED` 在**同一事务**内互斥，
  唯一可靠方式是一个可被 `FOR UPDATE` 锁住的 singleton 行。
- Redis / 进程内 flag 与 PostgreSQL claim 分属两个系统，
  无法提供单一 linearization point。

因此新增最小控制表，字段只取完成 invariant 所需的最小集合。

---

## 7. PAUSE 业务语义（两种模式共用）

PAUSE ACTIVE 时：

- `running`：继续自然运行，**不 kill / 不 cancel / 不 reset / 不中断**
- `queued / resume_queued`：保持原状态，不被 claim

UNPAUSE：恢复正常 claim eligibility。

绝不：kill worker、cancel running、UPDATE queue 状态、delete queue。

---

## 8. SAFE RELEASE

禁止 finally 无条件 unpause。

只有满足之一才 release，且**只释放本 attempt 自己拥有的 pause**：

- candidate verify PASS
- candidate FAIL → exact rollback → rollback verify PASS

rollback verify FAIL：

```
KEEP PAUSED
MANUAL_INTERVENTION_REQUIRED = TRUE
```

**Ownership 要求**：deploy 必须能区分"我创建的 pause"与"他人/先前设置的 pause"。
若 deploy 进入时已处于 paused，结束时**不得**擅自 unpause。
因此需要正式 ownership token。

---

## 9. 当前实现状态

本文为设计契约。实现与测试进行中；
PRODUCTION_DB_MIGRATION = NO（migration 只在 verification DB 执行）。
