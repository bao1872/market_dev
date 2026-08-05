# CHANGE-20260805-010 — Corrective Pass 3：Granular Restart 真实化 / P1-3 精确化 / 验证栈可运行化

- 日期：2026-08-05
- 范围：`granular_restart_service` / `product_readiness_service` / `admin_after_close` / 验证栈 compose 与部署脚本 / 相关单测
- 前序：CHANGE-20260805-009（Corrective Pass 2）
- 本轮**未**创建数据库、**未**执行 Migration、**未**部署（按授权范围）

---

## 0. 为什么需要第三轮

CP2 自述"已修复"，但复核发现以下缺陷会在真实运行时**确定性失败或静默错判**。
本轮逐条修复，并把判据写成可执行断言，而不是文档声明。

---

## 1. 废除 `last_completed_step` 伪造 restart（P0，语义反转）

**缺陷**：CP2 主链 restart 写入 `metadata.last_completed_step="checking_coverage"`。

实证（`after_close_orchestrator.py:2111-2143`）：`_completed_steps` 的键只有
`None / queued / refreshing_daily / syncing_boards / computing_features / publishing /
computing_review / succeeded`（外加三个旧名兼容键）。**根本不存在 `checking_coverage`**。
因此 `_completed_steps.get("checking_coverage", set())` → 空集合 → `skip_* 全为 False`
→ 语义等于「什么都没完成，从头全跑」，与「从 core 链开始、跳过日线刷新」**完全相反**。

此外 `last_completed_step` 的语义是「已完成到哪一步」，而 restart 需要表达的是
「应从哪一步开始」，两者方向相反，复用该字段本身即是设计错误。

**修复**：删除 `_MAINCHAIN_RESUME_STEP`，改为 `_MAINCHAIN_START_STAGE`，写入 child
`metadata.mainchain_stage`（含 `execution_mode="worker_pull"`），**绝不写 last_completed_step**：

| boundary | mainchain_stage（worker 起始阶段） |
|---|---|
| `daily_ready` | `syncing_boards` |
| `board_facts` | `syncing_boards` |
| `core` | `computing_features` |
| `stock_core_published` | `publishing` |

四个 stage 值均取自 orchestrator 真实步骤名。

**断言**：`test_mainchain_writes_stage_never_last_completed_step`（参数化 4 个 boundary），
断言 `"last_completed_step" not in metadata` 且 `child.status == "queued"`。

---

## 2. 10/10 boundary 真实 handler（CP2 为 9/10）

CP2 的 `state_events` 无真实 handler。CP3 新增 `rebuild_state_events()`：

1. **冻结 eligible universe**：该 core run 的 distinct instrument 集合；
2. **从 core artifact 派生**：调用领域级 `state_event_service.generate_events_for_run`
   （读快照 → 取前序兼容快照 → 比较稳定 code → 生成转换事件）；
3. **幂等 upsert**：`ON CONFLICT (idempotency_key) DO NOTHING`，
   幂等键 `symbol:source_run_id:algorithm_version`；
4. 返回 eligible / matched / coverage / algorithm_versions 统计。

`is_implemented_boundary()` 以 `_REAL_HANDLERS` 注册表为唯一权威（禁止「枚举即实现」）。

---

## 3. 子产品 handler：真实"重建 + 发布"，签名逐个核对

CP2 的子产品 handler 只是「重发旧产物」，且多处签名与真实函数不符（会 TypeError/AttributeError）。
本轮**逐个读取源码核对**后修正：

| boundary | CP2 错误 | CP3 真实调用 |
|---|---|---|
| `chip` | `publish_chip_consensus(db, run_id, operator=...)` → **TypeError**（无 operator 形参） | `publish_chip_consensus(db, chip_run.trade_date, chip_run.id, chip_run.algorithm_version, metadata=...)` |
| `review` | 传 run **id** → AttributeError | `db.get(MarketReviewRun, id)` → `publish_review(db, run, operator=..., idempotency_key=...)`（首参为 ORM 对象） |
| `auction` | 只调 publish = 重发旧 snapshot | `generate_auction_anchors(db, date, worker_id=...)` → `publish_auction_anchors(db, snapshot_id)`（先重建再发布） |
| `dsa_projection` | 调 `StrategyBatchService.publish_run`（只改 StrategyRun 状态，不是重建） | 从持久化 `CoreComputationArtifact` 经 `build_dsa_projection_payload(...)` 重建，写回 `summary_payload["dsa_projection"]`，并强制 source_core_run_id / parameter_hash / dsa version 三项对账 |
| `board_aggregation` | — | 取 succeeded `BoardAnalysisSnapshot` **对象** → `publish_board_analysis(db, snapshot)` |

**诚实边界**：主链四 boundary 在 orchestrator 中没有可按 boundary 名调用的 per-step 入口
（`execute_orchestrator_step` 需调用方传 operation 闭包），故其 handler 只创建 child +
写阶段标记，child 保持 `queued` 交由 worker 执行，**不写 succeeded、不伪造成功**。

---

## 4. 幂等键与真幂等（P0）

**CP2 缺陷**：`run_key` 只含 `trade_date:boundary`（跨 parent/跨输入会误复用），
且命中已有行后**仍然重新执行** handler —— 不是幂等，是重复执行。

**CP3**：

```
run_key = granular_restart:{trade_date}:{boundary}:{parent_job_run_id}:{source_core_run_id|none}:{input_hash}
input_hash = sha256(trade_date, boundary, source_core_run_id, extra)[:16]
```

`_create_or_reuse_child` 返回 `(child, should_execute, attempt)`：

| 既有 child 状态 | 行为 |
|---|---|
| `succeeded` + 同 `input_hash` | **直接返回，不执行 handler**（真幂等） |
| `queued` / `running` / `pending` | 返回既有 active child，不重复调度 |
| `failed` / 其他终态 | `attempt_no + 1`，重新执行 |
| source 或 input 变化 | run_key 不同 → 新 child |

**断言**：`test_succeeded_same_input_does_not_reexecute_handler`（`executed == []`）、
`test_active_child_not_rescheduled`、`test_failed_child_creates_new_attempt`（attempt 2→3）、
`test_run_key_contains_parent_source_and_input_hash`、`test_run_key_differs_across_parent_and_source`。

---

## 5. API 层枚举漂移修复

`admin_after_close._RESTART_FROM_VALID_VALUES` 此前手写 9 项，**漏 `board_aggregation`**：
该 boundary 有真实 handler，却会被 API 判为非法值返回 400。

改为 `set(granular_restart_service.ALL_BOUNDARIES)`，单一真源，消除两处枚举漂移。
同时清理仍描述 `last_completed_step` 与 501 的过期注释/docstring。

---

## 6. P1-3 精确完整性（CP2 永远无法达成 exact）

**缺陷**：CP2 中 `p1_3_exact_completeness` 只能取 `partial` / `not_complete`，
**硬编码不存在 `exact` 分支** —— 等于 P1-3 判定从未实现。

更严重的是分母错误：`eligible_count = total`（当日快照总数），而 `matched` 是同一张表的
子集计数，`coverage = matched/total` 是**自指比值**。只要没有上一轮残留，
1 只股票也能得到 `coverage = 1.0` 并判 ready。

**CP3 修正**：

### 6.1 dsa_projection

- `eligible` = 归属当前 core run 的 **distinct instrument**（冻结 universe，真实分母）；
- `matched` = 其中 `summary_payload` 真正含 `dsa_projection` 键的 distinct instrument
  （真实产物存在性，不是快照存在性）；
- `stale` = 当日不归属该 run 的残留快照数；
- **exact 判据：`matched == eligible`**（全覆盖），否则 partial / not_complete；
- 统计异常一律 fail-closed 归零，不得被解读为全覆盖。

### 6.2 state_events

诚实定义：state_events 只在**状态发生变化**时产生，绝大多数股票当日状态不变，
因此 `matched == eligible` 是**错误判据**（永远达不到，且不应达到）。

真正的精确判据：
1. eligible universe 已冻结（core run 的 distinct instrument 全集，`eligible > 0`）；
2. `stale == 0`（当日事件全部归属当前 core run，无 lineage 残留）；
3. `single_algorithm_version`（多版本混杂 = lineage 断裂）。

`coverage_ratio = matched / comparable`（`comparable` = universe 中有前序兼容快照、
因而可比对的股票数）仅作观测指标，不作门禁分母。

### 6.3 治理测试同步收紧

`test_readiness_lineage_governance.py` 原断言 `'"total"' in src`，与新契约冲突。
未降低标准，而是改为断言更强的契约，并新增两条：

- `test_p1_3_exact_completeness_can_reach_exact`：两个 state 函数源码必须包含 `"exact"`，
  防止再次退化为「永远 partial」；
- `test_dsa_exact_requires_full_eligible_coverage`：必须出现 `matched == eligible`。

---

## 7. 验证栈可运行化

| 缺陷 | 修复 |
|---|---|
| `docker compose up -d --build`，但 `docker-compose.verify.yml` **无任何 `build:` 段**，`--build` 是空操作 | 改用 **Live Mount**：复用既有正式镜像作依赖底座，只读挂载 `/root/web_dev_verify` 已 checkout 的代码 + `RUNTIME_SHA`（沿用 CHANGE-20260724-004 既有约定），移除 `--build` |
| 探针 `curl /v1/version.runtime_git_sha` —— **该端点不存在** | 真实合同（`app/api/health.py:109`）：`GET /v1/version` 返回 JSON，取字段 `runtime_git_sha`；并新增 `deployment_mode == "live"` 校验（确认 RUNTIME_SHA 确实被挂载） |
| DB 校验只用 `grep -q bz_stock_verify`（**包含**判断，`bz_stock_verify_xxx_backup` 也能过） | 解析 `DATABASE_URL` 的 database 段做**全等**比较；启动后再由应用侧执行 `SELECT current_database()` 二次确认（DS-110 fail-closed） |
| 未校验代码目录 SHA | `git rev-parse HEAD` 必须与目标 SHA 前缀一致，且 `git status --porcelain` 为空；`RUNTIME_SHA` 由脚本按 HEAD 生成 |
| 起栈后立即探针，必然抖动失败 | 新增 `READY_TIMEOUT`（默认 180s）轮询等待 `/v1/health` |
| 网络名硬编码 `market-dev-default` | 实测 `trading-postgres` 所在网络后经 `VERIFY_PG_NETWORK` 注入 |
| 本地控制脚本用相对路径 `scripts/ops/panji-prod-ssh` | 改为 `$SCRIPT_DIR/panji-prod-ssh`，不依赖调用方 CWD |
| 失败即 down，无诊断 | 失败**保留容器与日志**并打印诊断命令（DS-112） |

另新增前置校验：验证库必须已存在、镜像底座必须已存在、`frontend/dist/index.html` 必须已构建。

---

## 8. 本轮验证结果（诚实记录）

本地环境**不具备**运行条件（无 `docker`、无 `fastapi`、无 `ruff`），因此：

| 检查项 | 本地结果 |
|---|---|
| `py_compile`（5 个改动 Python 文件） | ✅ 通过 |
| `bash -n`（2 个脚本） | ✅ 通过 |
| compose YAML 解析 | ✅ 通过（`yaml.safe_load`） |
| 治理断言离线复现（13 项） | ✅ 全部 PASS |
| `pytest`（PURE_UNIT_TEST） | ❌ **未运行** — `ModuleNotFoundError: fastapi` |
| `ruff` / `mypy` | ❌ **未运行** — 本地未安装 |
| `docker compose config` | ❌ **未运行** — 本地无 docker |

> **未运行 ≠ 通过。** 上述 ❌ 项必须在远程精确检出最终 SHA 后实际执行并全部通过，
> 才可进入 Phase 4。本文件不得被引用为「测试已通过」的证据。

---

## 9. 状态

```text
granular_restart_complete   = true（10/10 真实 handler，签名已逐个核对）
p1_3_exact_completeness     = implemented（可达 exact，判据见 §6）
verify_compose_runnable     = pending_remote_verification（无 build 段问题已解，待远程实跑）
static_checks_passed        = false（本地不具备条件，须远程执行）
db_created / migration_run / deployed = false（本轮授权范围外）
```
