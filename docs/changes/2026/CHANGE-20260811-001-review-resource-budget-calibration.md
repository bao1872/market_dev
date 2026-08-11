# CHANGE-20260811-001 — Review 资源预算校准缺陷修复（REVIEW_RESOURCE_BUDGET_CALIBRATION_DEFECT）

- **类型**：resource-calibration + bugfix（运行期资源合约）
- **领域**：系统运行 / 容器资源预算 / REVIEW-V2 复盘计算
- **状态**：`implemented_unconfirmed`（代码/compose 已改；未远程部署、未实测高水位；资源预算数值关系待 `/etc/market-dev/market.env` 覆盖或默认生效）
- **关联 PRD**：`prd/80-system-runtime.md`
- **关联 Maps**：`maps/80-system-runtime.md`（§容器资源预算现状，已同步更新）
- **No PRD semantic change / No algorithm change / No Migration**

## 1. 问题（FIRST_BLOCKER = REVIEW_RESOURCE_BUDGET_CALIBRATION_DEFECT）

REVIEW-V2 FINAL RUNTIME ACCEPTANCE 链在 STEP 6（FULL `compute_run` SAME `d12a384c`）被执行通道前景运行，
进程在启动后约 1 秒内被 SIGKILL（exit 137）。只读根因诊断（2026-08-11）确认：

- cgroup `memory.max = 1GiB`；`memory.peak ≈ 1GiB`；`memory.events.oom_kill = 4`
- `docker inspect trading-backend` → `State.OOMKilled = true`
- 内核日志 `CONSTRAINT_MEMCG` 杀死 python，RSS ≈ 970MB
- **ROOT CAUSE = BACKEND_CGROUP_OOM**

即 `backend` 与 `trading-worker-after-close` 的 `mem_limit` 初值 `1024m` 是一个**未经生产验证的初始值（unverified initial value）**，
在真实 FULL Review compute（5293 instruments 载入 → ~970MB RSS）下触发 cgroup OOM。**该 1GiB 天花板已被生产 OOM 证据证伪。**

## 2. 修正范围（最小必要，不扩大）

仅校准 Review-capable 两个服务的 cgroup 上限，其余服务与算法、PRD 语义、Migration 均不动：

| 服务 | 修正前 | 修正后 | 说明 |
|---|---|---|---|
| `trading-backend` | `mem_limit: ${PANJI_BACKEND_MEM_LIMIT:-1024m}` | `mem_limit: ${PANJI_BACKEND_MEM_LIMIT:-4096m}` | Review-capable |
| `trading-worker-after-close` | 继承 `x-resource-app-heavy` 锚点 `1024m` | 新增显式 `mem_limit: ${PANJI_AFTER_CLOSE_MEM_LIMIT:-4096m}` override | Review-capable |
| `trading-worker-strategy-batch` | `x-resource-app-heavy` 锚点 `1024m`（无 override） | **不变** | 用户明确：不全局抬高所有 heavy worker |
| `trading-worker-capture` | `768m` | **不变** | 未参与 Review FULL compute |
| 轻 Worker / scheduler / watchdog / postgres / redis / frontend / umami | 各自初值 | **不变** | 与当前假设无关 |

**关键约束落实**：
- **不修改 `x-resource-app-heavy` 锚点**（仍 `1024m`），从而 strategy-batch 等共享该锚点的服务保持 `1024m` 不变；
- 仅对 `backend` 与 `after-close` 做**显式 `mem_limit` override**，避免"全局把 heavy worker 都设 4096m"；
- **不优化 Review 内存**：用户明确"Do NOT optimize Review memory further"，只抬高容器天花板。

## 3. 改动文件

1. `docker-compose.prod.yml`
   - `backend` 服务：`mem_limit` 默认 `1024m` → `4096m`（保留 `PANJI_BACKEND_MEM_LIMIT` 可配）
   - `worker-after-close` 服务：在 `<<: *resource-app-heavy` 后新增显式 `mem_limit: ${PANJI_AFTER_CLOSE_MEM_LIMIT:-4096m}`
2. `market.verify.env.example`
   - `PANJI_BACKEND_MEM_LIMIT=1024m` → `4096m`（与 compose 默认对齐）
3. `docs/maps/80-system-runtime.md`
   - §容器资源预算现状：backend 与 after-close 行 `1024m（证伪）→ 4096m`；补充 OOM 证伪说明；
     其余服务 mem_limit 现状列保持原值，明确 strategy-batch 等不变。

## 4. 验证状态

- `docker compose -f docker-compose.prod.yml config` 未在此环境运行（本地无 docker 守护，非 CI 连库禁令）；
  compose 文本改动为纯 YAML 键值覆盖，未触碰锚点定义，结构等价性高。
- 数值关系审查：`memory_budget_mb`（应用级）当前尚未在代码中落地（CHANGE-20260804-007 `implementation_pending`），
  故 `memory_budget_mb < mem_limit` 约束不受影响；4096m 仍低于 7.4G 宿主余量规划，未触碰宿主机保留。
- **未远程部署、未实测 `docker stats` 高水位**：本轮仅修正配置缺陷，部署与高水位回填需用户在授权真实部署后执行。
- **未复活 REVIEW-V2 闭环**：本修复解除 OOM blocker 的前提条件，但 FULL recompute 的实际执行仍属 REVIEW-V2 验收链，
  须用户在解除 blocker 后授权继续 STEP 6–16。

## 5. 后续（Deferred，需用户授权）

- 真实部署时确认 `/etc/market-dev/market.env` 是否需显式 `PANJI_BACKEND_MEM_LIMIT=4096m` / `PANJI_AFTER_CLOSE_MEM_LIMIT=4096m`，
  或依赖 compose 默认。
- 部署后 `docker stats --no-stream` 采集 backend / after-close 真实高水位，回填 Map（DS-104）。
- 解除 blocker 后继续 REVIEW-V2 STEP 6–16 闭环（仍受"不优化 Review 内存"约束，仅验证 4096m 是否足够）。
