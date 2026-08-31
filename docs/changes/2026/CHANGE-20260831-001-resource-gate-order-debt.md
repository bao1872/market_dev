# CHANGE-20260831-001 — RESOURCE_GATE_ORDER_DEBT 语义拆分

- 类型: 部署脚本控制流修正（非阈值校准）
- 影响文件: `scripts/deploy/panji-deploy.sh`
- 关联: E3 PRE-DEPLOY BLOCKER = RESOURCE_GATE_SEMANTIC_ORDER_DEFECT
- 不在范围内: 不降低 `MIN_MEM_MB`；不手工释放生产内存；不重启 worker 凑数；不改容器 `mem_limit`

## 背景 / 问题

原 `check_resource_budget()` 在**任何 supervisor-drain fence 之前**用 `MemAvailable >= 4096MB`
硬失败。但 backend runtime 部署的正常临界区是：

```
fence worker-after-close → worker 退出并释放 ~942MB anon → 再检查部署内存 headroom
```

因此当 `MemAvailable≈3441MB`、worker-after-close 可回收 ~942MB 时，脚本在 fence 之前就退出，
永远无法进入"释放内存后约有 4383MB"的临界区。这是真实的控制流错误
（`RESOURCE_GATE_ORDER_DEBT`）。

同时 `post_deploy_resource_check()` 用同一个 `MemAvailable >= 4096MB` 作为失败门槛，
而它在 `verify_deployment` / `cleanup_resources` 两处被调用时，worker-after-close 仍被 fenced/stopped，
于是检查的是"worker 被人为停掉时的临时状态"，不是最终生产稳态。

## 历史依据（source owner）

- 早期 `4096MB host MemAvailable` 仅是 deployment preflight 的保守门槛，authoritative rules
  并未定义"稳态必须空闲 4096MB"。
- `CHANGE-20260811-001` 将 backend / worker-after-close 的 **容器 `mem_limit`** 调到 `4096m`，
  原因是 1GB cgroup 上限被真实 Review OOM 证伪；该 4096 是**单容器允许使用的上限**，
  **不是宿主机必须空闲的内存**。7.4GB 宿主机的各容器预算基于"保留约 1GB 宿主余量"规划，
  并不要求宿主机空闲 4GB。三个数字（宿主总 RAM / 当前 MemAvailable / after-close cgroup limit）
  不能互相推导。

## 改动

1. `check_resource_budget` 拆分为两个语义独立的 owner：
   - `check_static_resource_budget()`：仅做阈值配置健全性 + 主机磁盘余量（合法生产约束），
     **不再**判断当前 `MemAvailable >= 4096`。`main()` 在 fence 之前调用它。
   - `check_deployment_memory_headroom()`：读取 `MemAvailable`（测试 seam
     `PANJI_MOCK_MEM_AVAILABLE_KB`，生产不设），要求 `>= MIN_MEM_MB`，fail-closed。
     不足时 `return 1`（非 `fail`），以便 `deploy()` 走 failure matrix 恢复被本 deploy fenced 的 worker。
2. `deploy()` 中，对 backend-runtime 变更路径：先 `_fence_after_close_worker`，再
   `check_deployment_memory_headroom`；frontend-only / 非 backend 路径不 fence worker，
   但首笔内存密集 mutation 前仍检查 headroom。`MIN_MEM_MB` 保持 4096，不降。
3. `post_deploy_resource_check()` 中 host 内存改为 **observation**（只记录，不再因此判失败）；
   保留磁盘门槛与 DS-104 容器健康（OOMKilled / RestartCount / limits / stats）。
4. `main()` 成功路径重排：在 `save_state` / 宣布成功**之前**先
   `_restore_after_close_pickup_if_owned`（worker 必须回到运行态），再跑最终稳态资源健康
   （`post_deploy_resource_check`），最后才 `save_state`。恢复或最终健康失败则 `fail`，
   不宣布成功（CASE F / H / I）。

## 验证

- `scripts/ops/test-panji-test-deploy-contracts.sh` 新增 CASE A–I：
  - A: backend 部署，worker running，fence 后 MemAvailable=4300 → 允许，fence 早于 mutation。
  - B: 同上，fence 后 MemAvailable=3800 → FAIL，零 runtime mutation，worker 被恢复。
  - C: 源码结构证明 headroom 位于 fence 之后。
  - D: queued 可见不阻塞部署，fence 仍线性化。
  - E: frontend-only，MemAvailable=3800<4096 → FAIL，且不停止 worker-after-close。
  - F/G/H/I: 源码结构证明 restore 先于 save_state、最终健康在 restore 之后、save_state 最后。
- 保留 P1-A / P1-B / P1-C 回归门禁不变。

## 回归门禁

- P1_A_ROLLBACK_OWNER = CLOSED
- P1_B_PENDING_QUEUE_VISIBILITY = CLOSED
- P1_C_SUPERVISOR_DRAIN = CLOSED

## 后续独立审计补充修正（同一 E3 blocker，非新 phase）

独立审计发现 3 个剩余缺口，已全部收口：

1. **FINAL_HEALTH_FAILURE_REFENCE（P1）**：最终稳态资源检查失败时，原路径直接 `rollback()`，
   但 `rollback()` 首动作即 `restore_files_to_previous_sha`，且把 `worker-after-close` 排除在
   recreate 之外——于是 live-mounted 文件被回滚改写时，candidate 的 after-close Python 进程
   仍在运行，形成 P1-C supervisor-drain 要消灭的**混合 runtime**。
   修复：`main()` 最终健康失败分支中，先（backend runtime 路径）`_fence_after_close_worker`
   重新建立 supervisor-drain fence（running==0 才继续），再做 `rollback()`；rollback 成功后在
   OLD runtime 上恢复 worker；rollback 失败则保持 fenced + 人工干预，绝不在 worker 仍 running
   时静默放过。re-fence 仅加在最终失败路径，不污染其它 rollback 调用方。

2. **AFTER_CLOSE_PICKUP_FENCED 状态机缺口**：owned restore 成功后 worker 已 running，但
   `AFTER_CLOSE_PICKUP_FENCED` 仍残留 `true`，与真实容器状态背离。
   修复：`_restore_after_close_pickup_if_owned` 成功时同步置
   `AFTER_CLOSE_PICKUP_FENCED=false`（FENCE_OWNED=false 一并）。仅 owned restore 才清，
   worker 原本 stopped/missing 且非本 deploy 拥有的分支不擅自清。

3. **PRODUCTION_MOCK_MEMORY_BYPASS（P1）**：`check_deployment_memory_headroom` 的测试 seam
   `PANJI_MOCK_MEM_AVAILABLE_KB` 可无条件覆盖真实 `/proc/meminfo`，等于给刚修好的 4096 gate
   开了隐藏 bypass。
   修复：seam 仅在 `DRY_RUN=true`（dry-run / 正式契约测试）下生效；真实部署遇到该变量视为
   环境泄漏，fail-closed 拒绝使用并强制读取真实 `/proc/meminfo`。

### 契约测试补充

- CASE H 由 false-green 结构化断言重写为**行为级**：使用 dry-run-only seam
  `PANJI_MOCK_POST_DEPLOY_FAIL_FINAL`（仅当 after-close worker 已恢复 running，即最终稳态复检
  阶段时触发），强制最终资源复检失败；验证「第一次 restore → 第二次 fence（drain）→ 回滚文件
  mutation」顺序，且回滚成功后 worker 在 OLD runtime 上恢复 running、状态机复位。
  （dry-run 下 `verify_deployment` 直接短路返回 0 且不调 curl，故不能靠 curl mock 触发最终失败；
  该 seam 是复用既有 `PANJI_MOCK_*` dry-run-only 范式的窄接口。）
- CASE J（行为级）：owned restore 后日志含 `AFTER_CLOSE_PICKUP_FENCED=false`。
- FIX F（行为级，隔离 `check_deployment_memory_headroom`）：真实部署 + seam 变量 → 拒绝通过；
  dry-run + seam → 仍可使用 mock 值。
- 保留原有 CASE A-G/I 与 P1-A/B/C 回归门禁。
