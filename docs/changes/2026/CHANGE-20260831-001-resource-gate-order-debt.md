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

- P1_A_ROLLBACK_OWNER = CLOSED（**已更正为过早收口**，见文末第二轮审计 P1_A）
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

## 第二轮独立审计：3 个 P1 收口（同一 E3 blocker，非新 phase）

> 状态：`implemented_unconfirmed`。本轮**未连接远程**、**未部署**、**未执行远程 dry-run**，
> 仅本地源码 + 测试 + 治理文档。上一轮标记的 `P1_A_ROLLBACK_OWNER = CLOSED` 属**过早收口**，
> 本节予以更正。

### P1_A — ROLLBACK_OWNER_SERVICE_NAME_DEFECT（假绿修正）

pre-deploy 捕获与 rollback 后核验都用 `docker inspect "${service}"`（bare Compose 逻辑服务名）。
真实拓扑中容器名是 `trading-<service>`，该调用必然 `No such object` → image ID 为空 →
在非 first-live 部署中 rollback owner 缺失/核验失效。此前契约测试的 docker mock 对任意
inspect 目标都返回非空占位，因此**掩盖**了该缺陷（false-green）。

修复：新增两个 helper，capture 与 verify **共用同一解析链**，不硬编码容器名前缀
（Compose 是容器命名 SSOT）：

```
_compose_container_id_for_service  : compose ps -q <service> -> 容器 ID
_container_image_id_for_service    : docker inspect <容器 ID> --format '{{.Image}}'
_container_image_ref_for_service   : docker inspect <容器 ID> --format '{{.Config.Image}}'
```

### P1_B — DRY_RUN_PRODUCTION_MUTATION（P1，dry-run 修改了生产容器）

`--dry-run` 在建立部署临界区时执行了真实 `compose stop -t -1 worker-after-close`
（失败路径再 `up --force-recreate`），违反 dry-run 零 mutation 合同。

修复：dry-run 下 fence 变为**模拟态**——只做只读探测（容器状态 + 活跃盘后任务计数），
置 `AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true`，**不**置真实 `AFTER_CLOSE_PICKUP_FENCED`；
restore 在 dry-run 下不 `up`；内存 headroom 在 dry-run 且无测试 seam 时 **deferred**
（不读真实 `MemAvailable`，避免用不可达门槛否决纯计划）。三处临界区门禁统一改用
`_backend_pickup_boundary_ready`（真实部署要求真实 FENCED，dry-run 接受 SIMULATED），
避免"某处判 FENCED、某处判模拟"的分叉。

### P1_C — 治理未与源码对齐（本轮按已批准架构对齐治理，不回退源码）

对齐结果（无新增数值阈值，`PANJI_MIN_MEM_MB` 维持 4096 且不下调）：

- `rules/80-deployment-migration.md` 新增 §11.1（资源门禁顺序：静态预算无 `MemAvailable`；
  headroom 位于 fence 之后、首笔 mutation 之前；部署后主机内存 observation-only；
  禁止把 4096 解释为宿主机稳态空闲要求）与 §11.2（dry-run 零 mutation，含禁止真实 fence）。
- `docs/runbooks/development-deployment.md`：Dry Run 段落补充"只模拟 fence / headroom deferred /
  只允许出现模拟态字段"；新增「部署资源门禁顺序」；DS-104 复检中主机内存由**门禁**改为
  **observation-only**（磁盘与容器级 OOMKilled / RestartCount / limits 生效性仍 fail-closed）。
- `docs/maps/80-system-runtime.md`：新增部署门禁顺序调用图与 rollback owner 容器解析拓扑。

**诚实说明**：审计输入中"现行规则要求在任何状态修改前 `MemAvailable >= 4096`"这一表述，
在 authoritative `rules/80-deployment-migration.md`（`rules/80-deployment-data-safety.md`
仅为兼容别名 stub）中**并不存在**——该文件此前完全没有内存条款。真实冲突只有一处：
runbook DS-104 把**部署后**主机内存写成失败门槛，而源码已改为 observation-only。
本轮只修正这一真实冲突并补齐缺失条款，未虚构"前置状态阈值"规则。

### 验证（本地，无远程）

- `scripts/deploy/panji-deploy.test.sh`：112 PASS / 6 FAIL。6 个 FAIL 在 BASE
  `b06c7d7b506890eb9945d86ca5485c8f474c5cfa` 上逐条一致（既有问题，delta=0）。
  另记录既有缺陷：该套件在 `11/11 DEPLOY ACTIVE-JOB GATE` 处因被测源码 `fail()` 内部 `exit`
  而中断，BASE 同样中断；因此本轮 P1-A/P1-B 行为断言被放在该小节**之前**并置于子 shell 隔离，
  否则永不执行（假覆盖）。断言条数自校验（8/8）防子 shell 早退。
- `scripts/ops/test-panji-test-deploy-contracts.sh`：新增/改写断言——
  P1-A 拓扑感知 mock（bare service inspect 必须失败，作为反向证明）+ capture/verify 对称；
  dry-run 零 mutation（无 `stop -t -1` / `up --force-recreate` / `RESTORED=true`，
  只允许 `AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true`）；headroom 有 seam 才驱动、无 seam 必 deferred；
  P1-C A' 结构断言证明**真实**部署路径仍保留 `stop -t -1` + `up -d --force-recreate`。
  修正了两处 mock 建模缺陷：`docker inspect` 目标必须按参数扫描（真实调用形如
  `inspect -f '{{...}}' trading-backend`，`$2` 是 flag）；行为级 verify lib 必须连同两个
  resolver helper 一起抽取，否则只是 command-not-found 假红。
- 未执行：远程 dry-run、真实部署。`E3_RESOURCE_GATE_BLOCKER` 仍待独立审计后再授权。
