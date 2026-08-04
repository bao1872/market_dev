# CHANGE-20260804-004: 资源与测试治理垂直切片 — 规则收口 / 容器硬预算 / 部署闭环 / 长任务预算 / 治理门禁

- 日期：2026-08-04
- 类型：governance+architecture+ops+contract
- 领域：治理规则 / 部署运维 / 测试合同 / 容器资源 / 长任务资源治理
- 关联 PRD：`docs/prd/80-system-runtime.md`
- 关联 Maps：`docs/maps/80-system-runtime.md`
- 关联 Changes：CHANGE-20260802-003（部署入口收敛）、CHANGE-20260802-005（治理去角色化）
- 数据操作：**零写入、零部署**（本轮仅本地验证、提交并推送 `origin/dev`；未连接远程服务器、未 dry-run、未执行真实部署）

## 1. 为什么改

盘迹治理规则存在三类缺陷：

1. **规则相互矛盾**：`rules/40-testing-quality.md` 既说"已永久删除 CI 临时数据库容器路线"，又写"必须在 CI 临时库运行"、TQ-92 表格写"CI 临时容器"；`rules/80` 第 244 行写 `RUNTIME_SHA`"必须原子替换（temp file + mv）"，与脚本实际原地写入、`maps` 描述、checker 断言三方冲突。
2. **写了未落实**：`docker-compose.prod.yml` 全部 15 个服务零容器级资源限制（无 `mem_limit`/`cpus`/`pids_limit`）；`panji-deploy.sh` 单次 `up -d --force-recreate` 交出全部 Python 服务、无串行/超时/部署后资源复检/旧 SHA 回收；长任务内存预算仅 Review bootstrap 有。
3. **只有禁止项无正向路径**：`rules/90` 禁止通用 prune，但没说"到底该怎么清理"。

## 2. 修改范围（5 个提交）

### Commit 1：规则矛盾清理
- `rules/40-testing-quality.md`：新增 **TQ-100 唯一测试模式合同**（唯二模式 `PURE_UNIT_TEST=1` / `PANJI_SHARED_DEV_DB_TEST=1`，永久禁止独立/临时/CI 测试库）；删除"CI 临时库运行""CI 临时容器"矛盾表述；TQ-92 表格"运行位置"改为共享开发库目标测试。
- `rules/50-git-development-flow.md`：新增 **GF-100 任务产物收尾**（可删前提、应清理清单、不得自动删除清单、最终报告四字段）。
- `rules/80-deployment-data-safety.md`：新增 **DS-100 主机准入时机 / DS-101 容器运行期硬预算 / DS-102 构建重启串行 / DS-103 长命令超时 / DS-104 部署后资源验收 / DS-105 旧 SHA 镜像精确回收 / DS-106 遗留容器定向治理 / DS-107 长任务统一资源合同**；修正 `RUNTIME_SHA` 表述为"原地 truncate+write、保持 inode、禁 mv/rename/rsync"。
- `rules/90-deprecated-forbidden.md`：补充清理边界**正向路径**（DS-105/DS-106/DS-107/TQ-100 落点）。

### Commit 2：文档同步清理
- `docs/maps/80-system-runtime.md`：删除"PostgreSQL 集成测试只在 CI 临时容器运行"；新增「容器资源预算现状」（初始值 + 实测高水位待采集）与「部署后资源证据字段」。
- `docs/runbooks/auction-analysis.md`、`docs/runbooks/review-restore-and-publish.md`：Migration 验证改为共享开发库目标测试模式。
- `docs/runbooks/development-deployment.md`：补充波次重启、超时值、部署后资源复检、旧 SHA 回收、GF-100 收尾。
- `docs/prd/80-system-runtime.md`：无冲突表述，未改。

### Commit 3：Compose 容器资源硬预算（DS-101）
- `docker-compose.prod.yml`：为 postgres/redis/backend/全部 10 worker/frontend/capture/umami 添加 `mem_limit`/`mem_reservation`/`cpus`/`pids_limit`/`stop_grace_period`，全部 `${PANJI_<SERVICE>_<FIELD>:-default}` 可配置；`x-resource-app-light/heavy/data` 三档 anchor；umami 补 `healthcheck`。

### Commit 4：部署脚本闭环 + 长任务预算
- `scripts/deploy/panji-deploy.sh`：新增 `run_with_timeout` 统一超时；全局 `COMPOSE_PARALLEL_LIMIT=1`；镜像逐服务串行构建；`restart_services` 改为固定波次重启（backend→health→frontend→Scheduler→workers→after-close/watchdog→capture）；`verify_deployment` 末尾加 `post_deploy_resource_check`（OOMKilled/RestartCount/限制生效/stats 高水位）；`cleanup_resources` 加旧 SHA 完整组精确回收 + 清理前后磁盘证据 + 清理后复检。
- `scripts/ops/panji-test-deploy`：删除 `PANJI_TEST_SKIP_PREFLIGHT` 绕过开关，preflight 成为唯一必经路径。
- `backend/app/utils/long_task_budget.py`（新增）：`LongTaskStopReason`/`LongTaskBudgetState`/`current_rss_mb`，统一 RSS 采样/峰值/预算判定/心跳/进度/stop_reason/checkpoint 序列化与恢复。
- `backend/app/services/review_bootstrap_service.py`：RSS 读取委托共享工具；`stop_reason`/`resume_token`/`progress` 附加字段（消费端 `.get` 兼容）。
- `backend/app/services/review_bootstrap_job_service.py`：透传 `stop_reason`/`resume_token`/`progress`。
- `backend/scripts/feature_snapshot_backfill.py`：接入 `LongTaskBudgetState`，batch 边界采样 RSS，超预算安全停止，返回 `stop_reason`/`peak_rss_mb`/`progress`/`resume_token`；新增 `--memory-budget-mb`。

### Commit 5：治理门禁与测试收口
- `tools/check_governance_rules.py`：新增四类门禁（Compose 资源限制 YAML 解析、部署脚本必备/禁止片段、测试合同 token 扫描扩展、清理合同证据）；测试库 token 扫描改为逐行豁免「明确禁止」语句。
- `tools/tests/test_check_governance_rules.py`：新增 7 个回归用例。
- `tools/tests/test_long_task_budget.py`（新增）：10 个纯单元用例。

## 3. 前后关键差异

| 维度 | 修改前 | 修改后 |
|---|---|---|
| 测试合同 | 自相矛盾（CI 临时库路线） | 唯二模式 TQ-100，唯一权威 |
| 容器资源 | 15 服务零限制 | 全服务 mem/cpu/pids/stop_grace 硬预算，env 可配置 |
| 部署重启 | 单次 `up -d` 全量 | 固定波次串行 + health 门 + Scheduler 单实例 |
| 长命令 | 无超时 | `run_with_timeout` 统一外层超时 |
| 部署后 | 无资源复检 | OOMKilled/RestartCount/限制生效/stats 高水位 |
| 镜像回收 | 仅 builder/image prune | 旧 SHA 完整组精确回收 + 磁盘证据 |
| 长任务内存 | 仅 Review 有预算 | Review/Feature Snapshot 统一 `long_task_budget` |
| preflight | 可被 `PANJI_TEST_SKIP_PREFLIGHT` 绕过 | 唯一必经路径 |

## 4. 验收标准对照

| 验收项 | 结果 |
|---|---|
| `temporary_test_database_terms = 0` | ✅ 活跃文档（rules/maps/prd/runbook/ci.yml）无旧测试库表述 |
| `all_runtime_services_have_memory_limit = true` | ✅ 15 服务均有 mem_limit（YAML 解析断言） |
| `build_parallelism = 1` | ✅ `COMPOSE_PARALLEL_LIMIT=1` 全局 + 逐服务串行构建 |
| `post_deploy_oom_check = true` | ✅ `post_deploy_resource_check` 检查 OOMKilled/RestartCount |
| `current_and_rollback_images_preserved = true` | ✅ 旧 SHA 组回收保留当前/上一成功/rollback |
| `long_task_safe_resume = true` | ✅ Review/Feature Snapshot 返回 resume_token，已有 --resume/checkpoint 幂等 |
| `rules_map_runbook_code_consistent = true` | ✅ RUNTIME_SHA 合同三方一致，checker 断言 |

## 5. 验证结果

- `tools/check_governance_rules.py`：PASS
- `tools/check_docs_consistency.py`：全部通过
- `tools/tests/test_check_governance_rules.py`：30 passed
- `tools/tests/test_long_task_budget.py`：10 passed
- `backend` 修改文件 `py_compile`：全部通过
- `scripts/deploy/panji-deploy.sh`、`scripts/ops/panji-test-deploy`：`bash -n` 通过

## 6. 不在范围 / 待办

- **未部署**：本轮仅本地验证、提交并推送 `origin/dev`；用户检查远端代码通过后再单独授权真实服务器 dry-run 与部署。
- **实测高水位待采集**：容器 mem_limit 为保守宽松初值，部署后需按 `docker stats --no-stream` 实测收紧（禁止只采集不限制）。
- 未连接远程服务器、未 dry-run、未部署、未修改共享开发业务数据库。
- 未创建分支、未 force push、未碰 `main`。
