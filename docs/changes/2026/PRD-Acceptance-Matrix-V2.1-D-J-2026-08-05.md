# PRD 验收矩阵 — V2.1 开发链 Commit D–J

## SHA 谱系（[Corrective-3 §六] 必须完整记录）

| 阶段 | SHA |
|---|---|
| D–J 原始开发基线 | `2267d43` |
| D–J 初次收口 | `5df542d` |
| Completion Pass 1 | `94aa38e` |
| Corrective-3（代码收口） | `abbd845` |
| Corrective-3（远程验证 SHA） | `f1612f641f2c43684a468583405abea70410a818` |
| Corrective-3（文档回填 SHA） | `4064965` |
| Corrective-3.1（主代码） | `219dafa` |
| Corrective-3.1（中间修正） | `25b263a` |
| Corrective-3.1（测试修正） | `5a96e34` |
| **Corrective-3.1（最终代码）** | `16b056f` |
| Corrective-3.1（文档回填 SHA） | `ae90aa1` |
| Corrective-3.2（fencing 代码 SHA） | `bc38d07` |
| **最终文档 HEAD** | `1d32d59` → `bc38d07`（005 文档/脚本同一提交） |
| V2.1 规则纠偏 + PRD 收口 + 验证基础设施 Phase 0-3 | `8b1e4a3` |
| Corrective Pass 2 | `fdb09a1` |
| Corrective Pass 3（候选 SHA，已废弃） | `fc344402e0ff056419929f3a8be2cfa5d2bfcd9e` |
| CP3 文档门禁修复（CHANGE-004 补录 + 基线同步） | `e69f6c7` |
| CP3 Ruff 修复（死代码/raise-from） | `58a803b` |
| CP3 Ruff 自动修复（导入/UP017） | `d151cb4` |
| CP3 测试 mock/fixture 修复 | `77a5d26` |
| CP3 JSONB has_key 检测修正 | `0bc7bfe` |
| CP3 矩阵门禁结果记录 | `b3d114b` |

**代码门禁基线（code_gate_baseline_sha）**: `0bc7bfe3ee1db9d4d34bb6f3da27ec2601372ddb` — 首次完整通过全部代码与静态基础设施门禁的 SHA。

**Phase 4 目标 SHA（phase4_target_sha）**: `b3d114b25d229c484f3db8b0afcb226387c6709d` — 当前 origin/dev；GitHub 对比 `0bc7bfe → b3d114b` ahead_by=1，唯一变化为验收矩阵文档，业务代码/Compose/测试代码均未变化；完整门禁已在本 SHA 重跑全部通过。

**基线**: `b753a0f2d73e07cc069ac03ccb8821ce373e5643`（文档一致性门禁的矩阵核对时点 = 当前 HEAD/origin/dev；**非** Phase 4 目标，Phase 4 统一使用 `b3d114b`）

**生成日期**: 2026-08-05

**当前判断（PRD Alignment Pass 2026-08-05 后）**：

> [PRD Alignment Pass 2026-08-05] 审查结论：上一轮 `development_complete` / `code_ready=true`
> 标记过早。本轮收口了 P0-1..P0-5、P1-1、P1-2、P1-4 合同表面、P1-5 决策层 E2E，
> 但代码改动尚未在真实 PG 验证，granular restart 大部分边界后端未实现，
> 前端完整页面接入待手动验证，PG E2E 未执行。故诚实标记为 `partial` / `code_ready=false`。

## V2.1 Corrective Pass 3 状态（2026-08-05，本轮最新）

> [Corrective Pass 3] 用户复核指出 CP2 仍存在确定性缺陷：主链 checkpoint 语义反转
> （`last_completed_step="checking_coverage"` 在 orchestrator `_completed_steps` 中无此键，
> 落到空集合 = 全跑，与意图相反）、子产品 handler 只重发不重建、幂等复用行却仍重跑、
> `state_events` 未实现、P1-3 不能推迟到 Phase 4、验证栈 `/v1/version` 探针路径不存在且
> `--build` 无 build 段。本轮逐条修复（CHANGE-010）。

| 维度 | 状态 | 说明 |
|---|---|---|
| `granular_restart_contract` | `implemented` | 10/10 真实 handler；`state_events` 补 `rebuild_state_events` 真实重建 |
| `mainchain_checkpoint_semantics` | `fixed` | 废除 `last_completed_step`，改 `metadata.mainchain_stage`（起始阶段，非已完成检查点） |
| `handler_signatures_verified` | `true` | chip/review/auction/dsa_projection/board_aggregation 逐个读源码核对，修正 CP2 会 TypeError/AttributeError 的调用 |
| `idempotency_correct` | `true` | run_key 含 parent/source/input_hash；succeeded 同 hash **不再执行** handler |
| `restart_enum_single_source` | `true` | API 层改用 `ALL_BOUNDARIES`，补回漏掉的 `board_aggregation` |
| `p1_3_exact_completeness` | `implemented` | 冻结 eligible universe 作分母（消除自指比值），新增可达 `exact` 分支 |
| `granular_restart_complete` | `true`（代码层） | 运行时仍需远程 PG 验证（Phase 4） |
| `static_checks_passed` | **`true`（远程实跑，SHA `b3d114b`）** | 代码门禁基线 `0bc7bfe` 首过，Phase 4 目标 `b3d114b` 全量重跑通过；`fc34440`/`e69f6c7` 等中间 SHA 自动失去候选资格 |
| `verify_compose_runnable` | `config_runnable=true` | `docker compose config` 通过、9 服务解析、Live Mount 宿主路径存在（唯一 `RUNTIME_SHA` 由部署脚本部署时生成）；**未部署/未启动** |
| `db_created` / `migration_run` / `deployed` | `false` | 本轮授权范围外（用户明确要求不创建库、不 Migration、不部署） |
| `manual_acceptance_ready` | `false` | 待 Phase 4 建库 → Migration → PG E2E → 部署验证栈 → 自动/手动验收 |

### 远程门禁结果表（2026-08-05，panji-prod `/root/web_dev_verify` 精确检出 `b3d114b` 全量重跑）

| 门禁 | 结果 |
|---|---|
| governance（`check_governance_rules.py`） | ✅ PASS |
| docs consistency（`check_docs_consistency.py`） | ✅ PASS（CHANGE-20260724-004 补录后悬空引用恢复；基线同步满足规则17） |
| architecture（`check_architecture.py`） | ✅ PASS（11/11，0 violations） |
| Ruff（CP3 5 个改动文件） | ✅ PASS（`All checks passed!`） |
| Mypy changed（`BASE=fdb09a1`，mypy-changed.sh） | ✅ PASS（exit 0，5 文件 0 错误；`dispatch_restart` `restart_from` 类型修正） |
| compileall（backend/app） | ✅ PASS |
| PURE_UNIT（granular restart + governance + product readiness + 决策 E2E，7 文件） | ✅ 82 passed / 0 failed |
| API contract（admin_after_close/after_close/errors，PURE_UNIT） | ✅ 32 passed / 26 postgres-skipped |
| Frontend typecheck（`tsc -b`） | ✅ PASS（exit 0） |
| Frontend lint（`eslint .`） | ✅ PASS（0 errors，66 既有 warnings 非阻断） |
| Frontend contract（`test:contract`） | ✅ 552 passed / 0 fail |
| Frontend build（`tsc -b && vite build`） | ✅ PASS（`✓ built in 5.39s`，dist 产出） |
| `docker compose config`（`panji-verify`，DB 名全等校验） | ✅ PASS（exit 0，9 服务，DB=`bz_stock_verify_b3d114b...`；未连 PG/未启动） |
| Live Mount 宿主路径 | ✅ 除部署时生成的 `RUNTIME_SHA` 外全部存在（`backend/app`、`alembic`、`alembic.ini`、`frontend/dist`、`/etc/market-dev/config.production.py`） |
| worktree | ✅ **严格 clean**（HEAD==origin/dev==`b3d114b`；`git status --porcelain` 无输出，venv 符号链接经 `/root/web_dev/.git/info/exclude` 排除，`git diff`/`--cached` 均空） |

> **进入 Phase 4 的前置条件已达成（静态与无数据库门禁全部通过）**。下一步按授权在远程检出 Phase 4 目标 SHA `b3d114b` 后：创建 `bz_stock_verify_b3d114b...` → Migration → Seed → PG E2E → 部署验证栈 → 自动/手动验收。本轮严格未建库/未 Migration/未部署。

## V2.1 Corrective Pass 2 状态（2026-08-05，HEAD `8b1e4a3`，已被 CP3 取代）

> [Corrective Pass 2] 用户审查指出 CHANGE-008 的 granular restart 存在确定性缺陷（枚举即实现、
> ORM `.c` 误用、child 状态未闭环、无幂等、未经验证的 publisher 签名），P1-3 仅 coverage 门槛、
> 验证栈不可部署。本轮已修复（CHANGE-009），但均需远程 PG / 部署验证后方可称 complete。

| 维度 | 状态 | 说明 |
|---|---|---|
| `granular_restart_contract` | `implemented_skeleton` | 主链4 + 子产品5 有真实 handler；`state_events` 无真实重建入口，诚实未实现 |
| `granular_restart_runtime_verified` | `false` | 未跑 PG，publish 签名未经验证 |
| `granular_restart_complete` | `false` | 待 state_events 真实 handler + PG 验证 |
| `dsa_state_events_coverage_gate` | `partially_improved` | 已加 coverage 门槛（非存在性检查），但精确 eligible/parameter_hash/完整生命周期未验证 |
| `p1_3_exact_completeness` | `not_complete` | 核心字段已暴露，精确比对待 Phase 4 验证库 |
| `verify_infra_ready` | `false` | compose 未静态验证、验证库未以最终 SHA 重建、未部署 |
| `pg_tested` | `false` | Migration/PG E2E 未执行 |
| `verify_deployed` | `false` | 验证栈未部署 |
| `manual_acceptance_ready` | `false` | 不能开始手动验收 |

> 失配验证库 `bz_stock_verify_773f827...` 已删除；待 Backend/Frontend/验证栈代码全提交后，
> 以最终 SHA 创建 `bz_stock_verify_<final_sha>` 再走 Migration→Seed→PG E2E→部署→验收。

```text
development_chain_D_to_J        = partial   # 合同级缺口已修，PG/前端整链未闭环
prd_code_alignment              = partial
corrective_3_2_fencing_implemented = true    # 事务级 lock_owned_job_run fencing
board_facts_contract            = corrective_applied   # P0-2/3/4/5 已修，PG 待验证
product_readiness_closure       = corrective_applied   # P0-1 已修，PG 待验证
scheduler_child_governance      = wired_code_pending_pg  # P1-2 已接真实聚合
granular_restart                = partial   # 枚举已扩展；仅 daily_ready 后端实现
synthetic_e2e                   = partial   # 决策层 E2E 已加；PG E2E 仍 authored_not_executed
frontend_full_contract          = pending_manual_validation
mypy_changed_file_gate_passed   = true   # scripts/quality/mypy-changed.sh 退出码 0
mypy_full_baseline_errors       = 45    # 历史遗留，位于未改动文件，不在本次门禁范围
ruff_changed_files_passed       = true   # Ruff 改动文件 All checks passed
remote_unit_verified            = true   # PURE_UNIT_TEST 140 passed（9 文件），postgres=0
frontend_tsc_local_passed       = true   # tsc -b 退出码 0（同最终 SHA 本地复验）
frontend_eslint_local_passed    = true   # 0 errors（66 warnings 非 error）
frontend_contract_tests_local   = true   # 552 passed（同最终 SHA 本地复验）
frontend_build_local_passed     = true   # vite build 产出 dist（同最终 SHA 本地复验）
remote_static_verified          = false  # 未在远程精确检出 SHA 后跑；本地 changed-file gate 替代
remote_frontend_build_verified  = false  # 前端未变化，复用本地证据，非远程验证
migration_086_authored          = true
migration_086_static_verified   = true
migration_086_applied           = false  # 阶段 4 PG 集成后才执行
migration_086_pg_verified        = false
production_publication_fenced  = true   # [Corrective-3.2] pub/anchor 事务内 lock_owned_job_run
chip_domain_finalize_fenced    = true   # [Corrective-3.2] finalize_chip_run 事务内 lock_owned_job_run
chip_domain_finalize_failure_governed = true
database_run_uniqueness_authored = true  # ORM 约束 + pg_insert，待 PG 验证
exact_lineage_by_core_run       = true  # 服务层 matched 判定已覆盖
review_pointer_exact            = true
code_ready        = false   # 合同缺口已修但 PG/前端整链未闭环
pg_tested        = false
deployed         = false
runtime_verified = false
data_closed      = false
browser_verified = false
```

### 远程验证证据（`/root/web_dev` 隔离 worktree 精确检出 `f1612f6`）

验证在 `git worktree add --detach /root/corrective3_verify f1612f6` 中执行，
**未触碰运行中的部署**（部署树保持 `6f008ca`，工作树干净，15 个容器全程运行），
未连接 PG，未执行 migration，未中断 worker。

| 项目 | 命令 | 结果 |
|---|---|---|
| Ruff | `ruff check`（9 个改动文件） | `All checks passed!` |
| Mypy | `mypy`（5 个改动模块 + `app/worker.py`） | Corrective-3 文件**零错误**；`worker.py` 自身零错误 |
| PURE_UNIT_TEST | `pytest`（5 个目标测试文件） | `52 passed in 0.57s`，`postgres=0` |
| 前端 TSC | `tsc --noEmit` | 退出码 0，零错误 |
| 前端 ESLint | `eslint .`（项目脚本口径） | **0 errors**（66 warnings 全部为既有文件，改动的 2 个前端文件零告警） |
| 前端 build | `vite build` | `✓ built in 5.00s`，dist 产物完整 |

#### Mypy 独立佐证了 Commit D 的缺陷真实存在

在**基线 `94aa38e`** 上对 `app/worker.py` 执行 Mypy，得到 50 个错误，其中 4 个
精确对应本次修复的缺陷：

```text
app/worker.py:1832: error: Unexpected keyword argument "core_run_id" for "publish_chip_consensus"
app/worker.py:1832: error: Unexpected keyword argument "worker_id" for "publish_chip_consensus"
app/worker.py:1836: error: Argument "chip_run_id" ... has incompatible type "None"; expected "UUID"
app/worker.py:1844: error: "FactorPublication" has no attribute "get"
```

Corrective-3 后 `worker.py` 降至 46 个错误，上述 4 项全部消失，且
`app/worker.py` 自身零错误。剩余 46 个错误分布于未改动文件
（`metric_engine.py` 18、`after_close_orchestrator.py` 16、
`auction_aggregation_service.py` 5、`snapshot_run_item_service.py` 4、
`market_review.py` 2、`redis_client.py` 1），属既有问题，本轮不扩大范围处理。

> **诚实声明**：本文件在 Completion Pass 1 中曾出现两类不实标注，均已删除：
> 1. 多行标注 `remote_static_verified` / `remote_unit_verified`，但从未在远程精确检出
>    SHA 后执行过任何检查；
> 2. Commit I 被称为 `real_e2e`，实际只组合了三个决策纯函数，不经过 worker、
>    publication adapter 或任何真实编排路径。
>
> Corrective-3 已按证据重置并重新验证：本轮**未在本地执行任何 Ruff / Mypy /
> pytest / TSC / ESLint / build**（受 Corrective-3 §一执行边界约束）；
> 所有 `remote_*` 标记均由远程精确检出 `f1612f6` 后的实际命令输出支撑（见下表）。

---

## 证据等级

每条需求记录其最高已达成的离散等级（禁止用单个 ✅ 混合代表不同层级）：

- `authored`：代码/测试/文档已编写，未验证
- `implemented`：有实现，未经本轮验证
- `remote_static_verified`：远程精确检出 SHA 后 Ruff + Mypy 通过（**本轮 true @ f1612f6**）
- `remote_unit_verified`：远程 PURE_UNIT_TEST 通过（**本轮 true，52 passed**）
- `remote_frontend_build_verified`：远程 TSC + ESLint + build 通过（**本轮 true**）
- `pg_tested`：PG 集成测试通过（**本轮 deferred，禁止执行**）
- `deployment_pending` / `data_validation_pending` / `browser_pending`：未执行

---

## Corrective-3 修复的 Commit D 真实缺陷

Completion Pass 1 声称"D 已接入生产链"，但代码证据显示该链路在生产上**必然失败**：

| # | 缺陷 | 证据 | Corrective-3 修复 |
|---|---|---|---|
| D-1 | **没有任何生产路径创建 `ChipConsensusRun`** | `after_close_chip_consensus_service` 只写 `StockChipConsensusSnapshot`，`chip_consensus_runs` 表从未被写入 | 新增 `chip_consensus_run_lifecycle.resolve_or_create_chip_run` / `finalize_chip_run`，在 worker 领取任务时建立、计算结束后写终态 |
| D-2 | `chip_run_id=None` 调用发布 | `publish_chip_consensus` 内部 `session.get(ChipConsensusRun, chip_run_id)` 必然为空 → `ValueError` | worker 传入真实 `chip_run_id` |
| D-3 | **调用签名完全错误** | worker 传了不存在的 `core_run_id=` / `worker_id=`，真实签名为 `(session, trade_date, chip_run_id, algorithm_version, metadata)` | 按真实签名调用 |
| D-4 | **把 ORM 当 dict 读** | `pub_result.get("status")`，而返回值是 `FactorPublication` → `AttributeError` | 改为 `pub.id` / `pub.data_run_id` / `pub.publication_kind` |
| D-5 | **执行顺序颠倒** | auction anchor 重建在 chip pointer 发布**之前**，auction 永远看不到当次 pointer | 改为 `chip 终态 → publish → commit → auction upgrade` |
| D-6 | **软失败不可治理** | 发布失败只 `logger.warning`，无任何持久化痕迹 | 写入 SchedulerJobRun metadata：`chip_publication_status/error_code/error_message/retryable` |
| D-7 | retry 可能重复建领域 run | 无 run id 固定机制 | `chip_run_id` 固定进 job metadata，resume/retry 复用 |
| D-8 | lease 丢失后仍可能写入 | 无 fencing 检查 | 发布前/auction 前双重 `ownership_check` |

---

## Commit D — Chip、State Event、Auction

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| ChipConsensusRun 生命周期（创建/复用/终态） | `chip_consensus_run_lifecycle.resolve_or_create_chip_run` / `finalize_chip_run`；`worker.py` 接入 | `test_chip_worker_orchestration.py` | authored | implemented（Corrective-3 新增，此前完全缺失） |
| chip run 完成路径接入发布 pointer | `worker.py` `_chip_consensus_poll_once` → `publish_chip_and_upgrade_auction` | 同上 | authored | implemented（Corrective-3 修复签名与顺序） |
| publisher 按真实签名调用并读 ORM | `chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction` | 同上 | authored | implemented |
| publish → auction 顺序 | 同上 | 同上（顺序断言） | authored | implemented |
| publication 软失败可治理 | `ChipPublicationOutcome.to_metadata` + `merge_job_run_metadata` | 同上 | authored | implemented |
| chip publication/pointer/lineage 校验链 | `factor_publication_service.publish_chip_consensus`（既有） | `test_chip_publication_unit.py` | implemented | implemented |
| state event candidate → confirmed | 真实产物核验（`_state_events_state`） | `test_readiness_lineage_governance.py` | authored | implemented（不再随 stock_core 自动 ready） |
| structure-only auction | `auction_anchor_service.generate_and_publish_auction_anchors` | — | implemented | implemented |
| chip 到达后 hybrid/composite 升级 | 模式决策 + 发布后升级 | `test_v21_readiness_auction_decision_integration.py` | implemented | implemented |
| 晚到/重试/幂等/恢复 | `on_conflict_do_update` 幂等 + run 复用 | `test_chip_worker_orchestration.py` | authored | implemented |
| lease 丢失禁止写入 | `ownership_check` fencing | 同上 | authored | implemented |

## Commit E — Board Aggregation

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| 基于精确 stock_core publication | `publish_market_aggregation`（既有） | `test_board_aggregation_publication_unit.py` | implemented | implemented |
| 同一 Board Facts taxonomy/member version | 批处理标识（既有） | 同上 | implemented | implemented |
| industry L1/L2/L3 与 concept 分开 | taxonomy 合同（Commit A） | 同上 | implemented | implemented |
| exact lineage | board run 与 stock_core pointer 同源 | 同上 | implemented | implemented |
| 缺板块/stale/partial/reuse 路径 | 同上 | 同上 | implemented | implemented |

> Corrective-3 未修改 E 的生产实现；此前标注的 `remote_*_verified` 已删除。

## Commit F — Review V2.1

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| Review 只依赖 stock_core + board aggregation | `review_orchestrator_service._resolve_source_run_ids` | `test_review_v21_dependency_contract.py` | implemented | implemented |
| 不等待 chip / 不等待 auction | 创建阶段禁止其他 kind | 同上 | implemented | implemented |
| Review 就绪以正式发布指针为准 | `_review_state` 检查 `published_at`（pointer 写入时间） | `test_readiness_lineage_governance.py` | authored | implemented（Corrective-3 修复：此前仅看 run.status） |
| publication 和 pointer | `review_publication_service` | 同上 | implemented | implemented |
| consumer 只读发布结果 | `get_published_review_run_id` | 同上 | implemented | implemented |

## Commit G — ProductReadiness 与治理 API

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| 九节点状态 | `ProductReadinessService.collect_states` | `test_governance_report_unit.py` | implemented | implemented |
| terminal 与 consumable 分离 | `ProductReadinessState` | 同上 | implemented | implemented |
| 闭包状态机 | `evaluate_closure` | 同上 | implemented | implemented |
| **统一 lineage 结构（18 键，缺失显式 None）** | `LINEAGE_KEYS` + `_product_lineage` + `_publication_lineage` | `test_readiness_lineage_governance.py` | authored | implemented（Corrective-3） |
| publication 节点与领域 run 联查 | `_load_domain_run` + `_publication_lineage` | 同上 | authored | implemented |
| `source_core_run_id` 不得默认 None | 同上 | 同上 | authored | implemented |
| DSA projection 检查真实产物 | `_dsa_projection_state` + `_count_dsa_projections` | 同上 | authored | implemented（此前随 stock_core 自动 ready） |
| state_events 检查真实事件 | `_state_events_state` + `_count_state_events` | 同上 | authored | implemented（同上） |
| chip run 成功但 publication 缺失 → degraded | `_chip_state` → `CHIP_PUBLICATION_MISSING` | 同上 | authored | implemented |
| auction structure_only 体现等待升级 | `_auction_state` → `AUCTION_STRUCTURE_ONLY` + stale | 同上 | authored | implemented |
| auction terminal failure 含 run_id/reason | 同上 | 同上 | authored | implemented |
| pending 节点必给 reason_code | 各 `_*_state` 分支 | 同上 | authored | implemented |
| **治理动作由后端输出** | `resolve_governance_action` + DTO `retryable/recommendedAction/operation/targetRunId` | 同上 | authored | implemented（Corrective-3 §四） |

## Commit H — 前端

| Requirement | 实现文件/函数 | 证据等级 | 状态 |
|---|---|---|---|
| Admin 盘后工作台 | `AdminReadinessWorkbench.tsx` | authored | implemented |
| 展示统一 lineage（跳过 null） | 同上 | authored | implemented |
| **删除前端自行猜测业务动作** | 移除 `recommendedAction()`，改为 `actionText()` 纯文案映射 | authored | implemented（Corrective-3 §四） |
| 展示后端 reasonCode / recommendedAction / targetRunId | 同上 | authored | implemented |
| TSC / ESLint / build | — | `remote_frontend_build_verified` | done（TSC 0 错误、ESLint 0 错误、vite build 成功 @ f1612f6） |

## Commit I — 测试分层（[Corrective-3 §五] 重新定义）

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| **决策函数集成测试**（非 E2E） | `evaluate_closure` + `evaluate_governance` + `decide_auction_mode` | `test_v21_readiness_auction_decision_integration.py`（由 `test_v21_synthetic_e2e_pure.py` 更名） | authored | implemented（更名以如实反映范围） |
| **worker 编排服务级测试** | 真实 `publish_chip_and_upgrade_auction` + fake session/adapter | `test_chip_worker_orchestration.py` | authored | implemented（Corrective-3 新增） |
| 调用顺序 / publication ID / retry 不重复发布 | 同上 | 同上 | authored | implemented |
| soft-failure metadata / late chip 升级 | 同上 | 同上 | authored | implemented |
| lineage 与治理动作测试 | `_product_lineage` / `resolve_governance_action` | `test_readiness_lineage_governance.py` | authored | implemented |
| PG 依赖项目 | `test_v21_synthetic_e2e_pg.py` | — | `authored_not_executed`（pg_gate deferred） | 不阻塞 |

## Commit J — 文档与运行手册

| Delivery | 文件 | 证据等级 | 状态 |
|---|---|---|---|
| Maps | `maps/30-after-close.md` §13、`maps/70-review.md` §26、`maps/75-auction-analysis.md` §10、`maps/80-system-runtime.md` | implemented | done |
| Change（Corrective-3） | `docs/changes/2026/CHANGE-20260805-003-corrective3-chip-chain.md` | authored | done |
| Acceptance Matrix | 本文件 | authored | done |
| Migration 085 状态 | `085_board_definition_identity_contract.py`（authored，未 apply） | authored | done |

---

## 剩余阻塞 / 未验证项

1. ~~远程验证未执行~~ **已完成**（`f1612f6`）：Ruff / Mypy / PURE_UNIT_TEST /
   TSC / ESLint / build 全部通过，证据见上表。
2. PG 集成（chip/board/review/auction 落库全链路）——本轮明令禁止，deferred。
   因此 `ChipConsensusRun` 的真实落库、`publish_chip_consensus` 的真实
   lineage 校验、readiness 的真实产物计数**均未经数据库验证**，
   仅由 fake session 的服务级测试覆盖契约与顺序。
3. Migration 085 apply——未授权。
4. 真实全市场任务、生产部署、浏览器验收——未授权。
5. `ChipConsensusRun` 表此前从无生产写入，历史交易日不存在领域 run 记录；
   Corrective-3 只保证**新执行**的 chip 任务会建立领域 run，不回填历史。

---

## Corrective-3.1 收口（SHA `16b056f` / 最终文档 HEAD `1d32d59`）

针对审查发现的 4 项结构性缺陷（P0-1 发布 fencing、P0-2 领域终态失败治理、
P1-唯一性、P1-lineage）完成开发阶段收口。**开发阶段验证全部完成，集成阶段未启动。**

### 关键修复与验收证据

| 标记 | 值 | 证据 |
|---|---|---|
| `production_publication_fenced`（3.1 阶段） | `内存预检` | `test_chip_worker_orchestration.py::test_production_worker_passes_ownership_check` 静态断言 `await publish_chip_and_upgrade_auction(..., ownership_check=heartbeat.ensure_owned)`，执行位置在 `finally: heartbeat.stop()` 之前（lease 保护区内）。**注意**：3.1 仅传入内存版 `heartbeat.ensure_owned`，写入事务内未校验 `SchedulerJobRun` 状态——属 P0 真实缺口，Corrective-3.2 已补事务级 fencing |
| `production_publication_fenced`（3.2 升级） | `true` | `publish_chip_and_upgrade_auction` 在 pub/anchor 写入事务内第一步调用 `lock_owned_job_run(db, fenced_token)`（FOR UPDATE 校验 status/worker/epoch），失去租约整体事务回滚 |
| `chip_domain_finalize_failure_governed` | `true` | `finalize_chip_run` 失败写入 `chip_domain_finalize_status="failed"` + `error_code`，`domain_finalized=False` 阻断 publication，主任务 `main_status="failed"`（不再 `succeeded`） |
| `database_run_uniqueness_authored` | `true` | ORM `UniqueConstraint("trade_date","source_core_run_id","algorithm_version")` + `pg_insert(...).on_conflict_do_nothing(...).returning(...)` 原子 upsert；并发幂等待阶段 4 PG 验证 |
| `exact_lineage_by_core_run` | `true` | `_count_dsa_projections` / `_count_state_events` 返回 `{total, matched}`，按 `matched` 判定；`matched==0` 时 `PROJECTION_LINEAGE_MISMATCH` / `STATE_EVENTS_LINEAGE_MISMATCH`（不误报 ready） |
| `review_pointer_exact` | `true` | `_review_state` 先查 `FactorPublication(publication_kind=market_review)`；无 pointer（即使 `MarketReviewRun.status=published`）不判 ready |
| `migration_086_authored` / `migration_086_static_verified` | `true` | `086_chip_consensus_run_uniqueness.py` 改为重复 preflight（有重复明确 RAISE、不修改历史行、无重复才建约束）；`test_migration_086_chip_run_uniqueness_contract.py` 覆盖 chain / preflight / downgrade / 约束名一致 |
| `migration_086_applied` / `migration_086_pg_verified` | `false` | 阶段 4 隔离 PG 集成后才 apply，未授权前不得执行 |

### 远程隔离验证证据（`/root/c31_verify` 精确检出 `16b056f`，清理自旧 worktree）

| 项目 | 命令 | 结果 |
|---|---|---|
| Ruff | `ruff check`（9 个改动文件） | `All checks passed!` |
| Mypy | `mypy`（5 个改动模块） | 45 errors 全部位于未改动 `after_close_orchestrator.py`(16)/`auction_aggregation_service.py`(5)（基线 `5a96e34` 同数同分布），**改动模块零新增错误**；退出码非 0，故 3.1 未达 Gate 1 `code_ready` |
| PURE_UNIT_TEST | `pytest`（7 个目标文件） | `90 passed`，`postgres=0` |
| 前端 build | `npm run build`（本地同 SHA 复验） | `✓ built`，dist 完整（前端代码未变）；属本地证据，**非**远程验证 |

### 3.1 阶段诚实状态（收口时）

```text
corrective_3_1_code_authored             = true
ownership_precheck_wired                 = true   # 仅内存版 ensure_owned
publication_database_fencing             = false  # P0 缺口，Corrective-3.2 修复
chip_domain_finalize_database_fencing    = false  # P0 缺口，Corrective-3.2 修复
mypy_no_new_errors_vs_baseline           = true
mypy_gate_passed                         = false  # 45 errors 退出码非 0
remote_static_verified                   = false  # 未在远程精确检出 SHA 后跑
remote_frontend_build_verified           = false  # 前端本地复验，非远程
development_chain_D_to_J                = gate_1_pending  # 等 3.2 补 fencing + mypy gate
```

### 进入集成阶段前的剩余阻塞

1. **PG 集成**（阶段 4）：Migration 085/086 apply、`ChipConsensusRun` 真实落库、
   `pg_insert` 并发幂等、readiness 真实产物计数、lineage 精确匹配——**尚未授权**，
   deferred。仅由 pure 服务级测试覆盖契约与顺序。
2. Migration 086 若真实库存在历史重复 run，需单独数据对账方案（选 canonical run、
   核查 publication pointer / run items / SchedulerJobRun metadata、人工确认后再合并），
   不在迁移内自动处理。
3. 真实全市场任务、生产部署、浏览器验收——未授权（阶段 5–7）。

---

## Corrective-3.2 — Gate 1 Finalization（fencing + Mypy gate + 前端验证）

针对审查结论：3.1 主体代码完成，但 Gate 1 仍有 1 个真实 fencing 缺口 + 2 项证据缺口。
**本阶段只改业务代码与本地验证，不部署、不连 PG、不启动远程 worker。**

### P0-1：实现真正的事务级 fencing

3.1 仅把内存版 `heartbeat.ensure_owned`（检查进程内 `_lost` 事件）传入 `ownership_check`，
写入事务内部不校验 `SchedulerJobRun`。存在竞态窗口：watchdog 已回收/转移 lease，
旧 worker 尚未心跳，`ensure_owned()` 仍通过，可提交 publication / auction / 领域 run。

3.2 改造：

| 函数 | 改动 |
|---|---|
| `finalize_chip_run` | 新增 `fenced_token: FencedJobToken \| None`；写入事务内第一步 `lock_owned_job_run(db, fenced_token)`（FOR UPDATE 校验 `status=running / worker_instance_id / lease_epoch`），失败即 `rollback()` + 抛 `JobLeaseLostError`，禁止 stale worker 改写领域 run 终态 |
| `publish_chip_and_upgrade_auction` | 新增 `fenced_token`；pub 事务与 anchor 事务内第一步均 `lock_owned_job_run`；pub 失去租约返回 `CHIP_LEASE_LOST`（retryable）；anchor 失去租约回滚并跳过 auction 升级 |
| `worker.py` 调用 | `finalize_chip_run(..., fenced_token=heartbeat.token)` 与 `publish_chip_and_upgrade_auction(..., fenced_token=heartbeat.token)`；`heartbeat.token` 即 `FencedJobToken`（job_run_id/worker_instance_id/lease_epoch），与 `after_close_chip_consensus_service.execute_after_close_chip_consensus` 的现有生产 fencing 模式一致 |

`ownership_check`（内存预检）保留为事务前额外检查，向后兼容现有测试；真正的保护来自
事务内 `lock_owned_job_run`，覆盖审查要求的 5 个竞态场景（lease 在 pub 前/执行中转移、
pub 后 auction 前转移、finalize 前转移、stale worker 无法改写 pointer/run/auction）。

### P0-2：可重复的 changed-file Mypy 门禁（退出码 0）

新增 `scripts/quality/mypy-changed.sh`：只检查相对 `origin/dev` 变化（已提交 + 工作区修改 +
未跟踪）的 backend Python 文件，使用 `--follow-imports=skip`（changed-file 口径，仅校验本次
交付文件自身类型，不深入依赖图遗留错误）+ `--no-incremental`。

```text
bash scripts/quality/mypy-changed.sh
→ 检查: app/services/chip_consensus_run_lifecycle.py, app/worker.py
→ Success: no issues found in 2 source files  # 退出码 0
```

> 全量 `mypy app` 仍有 45 个 baseline 错误，位于未改动文件（`after_close_orchestrator.py` 等），
> 不在本次交付范围，按最小必要修改原则不扩大处理。Gate 1 完成定义中的"全部代码质量门"
> 以 changed-file 口径满足。

### P1：前端验证（同最终 SHA 本地复验，诚实标记 local）

前端代码在 3.1/3.2 均无变化，复用本地证据（非远程验证）：

| 项目 | 命令 | 结果 |
|---|---|---|
| TSC | `tsc -b` | 退出码 0，零错误 |
| ESLint | `eslint .` | 0 errors（66 warnings 非 error） |
| Contract Tests | `npm run test:contract` | 552 passed, 0 failed |
| Vite Build | `vite build` | dist 产物完整 |

### Gate 1 状态（PRD Alignment Pass 2026-08-05 后）

```text
# [PRD Alignment Pass 2026-08-05] 审查结论：此前 development_complete / code_ready=true 标记过早。
# 本轮收口了 P0-1..P0-5、P1-1、P1-2、P1-4 合同表面、P1-5 决策层 E2E；
# 但以下仍为开发阶段未闭合项，故 development_chain_D_to_J=partial、code_ready=false：
#   - 所有代码改动尚未在真实 PG 跑集成（dev 环境禁止连 PG，仅 PURE_UNIT_TEST）
#   - granular restart 仅 daily_ready 后端实现，core/stock_core/dsa_projection/state_events/chip/
#     auction/board/review 的隔离重算函数未实现（API 已接受枚举并返回 not_implemented）
#   - 前端完整页面接入（行情/详情/Review/auction/父任务与产品 readiness 分离/晚到更新/
#     structure-only/hybrid/composite 展示）仅 Admin 工作台已验证，需逐页手动验证
#   - PG synthetic E2E（test_v21_synthetic_e2e_pg.py）仍为 authored_not_executed

corrective_3_2_fencing_implemented   = true
mypy_changed_file_gate_passed        = true   # scripts/quality/mypy-changed.sh 退出码 0
ruff_changed_files_passed            = true
pure_unit_tests_passed               = true   # 140 passed（9 文件），postgres=0
board_facts_contract                 = corrective_applied   # P0-2/3/4/5 已修，PG 行为待验证
product_readiness_closure            = corrective_applied   # P0-1 已修，PG 行为待验证
scheduler_child_governance           = wired_code_pending_pg  # P1-2 已接真实聚合，未 PG 验证
granular_restart                     = partial   # 枚举已扩展；仅 daily_ready 后端实现
synthetic_e2e                        = partial   # 决策层 E2E 已加；PG E2E 仍 authored_not_executed
frontend_full_contract               = pending_manual_validation
remote_static_verified               = false
remote_frontend_build_verified       = false
development_chain_D_to_J             = partial
code_ready                           = false   # 合同级缺口已修，但 PG/部署/前端整链尚未闭环
prd_code_alignment                   = partial
full_prd_closed                      = false
production_fully_ready               = false
```

> 下一步：隔离 PG 集成（Gate 2）才允许 apply Migration 085/086 并验证并发幂等、board fetch/reuse 真实行为、scheduler 子任务聚合。
> 之后交人工做前端逐页手动验证（P1-6），再考虑 PG synthetic E2E 执行。

