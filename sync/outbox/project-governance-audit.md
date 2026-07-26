# 盘迹项目治理第一阶段审计报告

> 报告日期：2026-07-26
> 阶段：治理第一阶段（只读审计，不动代码）
> 工作分支：dev（固定）
> 草案来源：`sync/panji_agents_rules_maps_autodeploy_v2/`
> 当前事实源：仓库真实代码 + 现有正式 docs
> 本报告不修改任何正式文件，不启用 Actions，不修改 Compose/脚本/服务器配置。

---

## 1. 当前分支、HEAD、工作区状态

| 项 | 值 |
|---|---|
| 当前分支 | `dev` |
| HEAD SHA | `06bf5109b07a966207e7203e2b2ba12c7e12388d` |
| HEAD commit message | `files upoad` |
| 与 origin/dev 关系 | `up to date with 'origin/dev'` |
| 工作区 | `clean`（nothing to commit） |
| 仓库根 | `/workspace` |

⚠️ 当前 HEAD commit message 为 `files upoad`，与盘迹常规 CHANGE-YYYYMMDD-NNN 模式不一致，提示 dev 上存在一次性大文件落库的提交。本阶段不修改 git 历史。

---

## 2. 当前文档体系清单

### 2.1 仓库根

| 文件 | 用途 |
|---|---|
| `AGENTS.md` | 项目开发与文档一致性规则 v3（909 行级单文件，含 §一-§十一与 §七 23 条硬规则） |
| `Makefile` | docker-build / dev / migrate / test / lint 等基础命令 |
| `docker-compose.prod.yml` | 14 服务生产编排（postgres/redis/backend/9 worker/frontend） |
| `docker-compose.live.yml` | Live Mount 叠加配置（CHANGE-20260724-004） |
| `docker-compose.yml` | 基础开发环境（仅 Postgres + Redis） |
| `.github/workflows/ci.yml` | 当前唯一 CI：架构/docs/allowlist/ruff/mypy/alembic/pytest/frontend tsc/lint/build/contract/E2E |
| `.gitignore` / `.dockerignore` | 仓库级忽略 |

### 2.2 `docs/` 顶级目录

AGENTS §二白名单允许：`current/` `maps/` `changes/` `archive/` `contracts/` `decisions/` `runbooks/` `acceptance/` `evidence/` `work/`（docs/ 根 .md 不受限）。

实际目录：

| 子目录 | 文件数 | 说明 |
|---|---|---|
| `docs/current/` | 9 + MANIFEST + code-doc-alignment + open-decisions = 12 | 当前设计事实 |
| `docs/maps/` | 10 | 代码位置与调用链地图 |
| `docs/changes/` | CHANGELOG + CHANGE-TEMPLATE + 113 条 records | 历史变更 |
| `docs/archive/` | current-legacy-20260703 共 19 文件 + README | 归档历史 |
| `docs/contracts/` | 7 | 机器可执行合同（schema/yaml/md） |
| `docs/decisions/` | ADR-0001/0002/0003 + README | 架构决策记录 |
| `docs/runbooks/` | 4 + README | 操作手册 |
| `docs/acceptance/` | ci-gates.md | 验收矩阵 |
| `docs/evidence/` | 1 + README | 生产验收证据 |
| `docs/work/` | 4 | PRD/checkpoint |

`docs/` 根 .md：`AI-ONBOARDING.md` `INDEX.md` `MAINTENANCE.md` `MIGRATION-MAP.md` `README.md` `RESTORE-CHECKLIST.md` `SOURCE-SNAPSHOT.md` `TRAE-APPLY-INSTRUCTION.md`。

### 2.3 `docs/current/` 清单

| 文件 | 用途 |
|---|---|
| `MANIFEST.md` | 实现核对基线 `086ebce593ac19ea49f5a4ce2f21e8c77af5ec80`（HEAD 祖先，≤50 commit 内满足规则 16） |
| `00-product-business.md` | 产品定位、用户、业务边界 |
| `01-system-architecture.md` | 系统拓扑、模块边界、依赖方向 |
| `02-data-api-contracts.md` | 数据实体、API、权限、安全 |
| `03-jobs-integrations-operations.md` | Worker、任务、飞书、Capture、部署 |
| `04-frontend-ux.md` | 前端路由、页面、UI 状态 |
| `05-testing-acceptance.md` | 测试、CI、验收（163KB，仓库最大文档） |
| `06-research-feature-matrix.md` | 研究特征矩阵与因果口径 |
| `07-atomic-fact-contract-v1.md` | AFC V1 个股状态观察 |
| `08-indicator-calculation-contracts.md` | 指标计算合同 |
| `code-doc-alignment.md` | 当前差异登记 |
| `open-decisions.md` | 未决问题（OPEN-PRODUCT-001 等 8 项） |

### 2.4 `docs/maps/` 清单

`api-route-map.md` `backend-module-map.md` `database-model-map.md` `deployment-runtime-map.md` `frontend-route-map.md` `indicator-computation-map.md` `notification-flow-map.md` `smc-pine-parity-map.md` `test-coverage-map.md` `worker-job-map.md`。

### 2.5 `docs/runbooks/` 清单

`after-close-recovery.md` `branch-deployment-rollback.md` `feishu-image-issues.md` `live-bind-mount-deployment.md` + README。

### 2.6 `docs/changes/`

- `CHANGELOG.md`：2026-07-02 → 2026-07-25，索引式摘要
- `records/`：CHANGE-20260702-001 ~ CHANGE-20260725-003 共 113 条
- `CHANGE-TEMPLATE.md`：14 必填字段模板
- 最近 CHANGE：
  - CHANGE-20260725-003：左栏来源列表可见性修复（missing_origin invalid）
  - CHANGE-20260725-002：左栏来源列表切股闪烁修复
  - CHANGE-20260725-001：freshness_state 日级周期日期比较修正
  - CHANGE-20260724-004：个股详情行情唯一真源 + Live Mount 部署
  - CHANGE-20260724-003：Phase 8A 端到端链路正确性收口
  - CHANGE-20260724-002：指标原理页替换 + 二维码入口
  - CHANGE-20260724-001：门户二维码更新

### 2.7 检查工具

| 工具 | 用途 |
|---|---|
| `tools/check_docs_consistency.py` | 16 条规则（MANIFEST baseline / docs 目录白名单 / CHANGE 引用 / ref/ 隔离 / Webhook 回归 / baseline 新鲜度 ≤50 commit） |
| `tools/check_architecture.py` | 架构规则静态检查（不连 DB、不导入 conftest） |
| `tools/check_test_allowlist.py` | 测试白名单 |
| `tools/compare_ruff_baseline.py` / `tools/compare_mypy_baseline.py` | 历史债务 baseline 比对 |
| `tools/quality_baselines/{ruff,mypy}.json` | 历史债务基线 |
| `tools/check_mypy_new_files.py` | 新文件 mypy 检查 |
| `tools/tests/test_frontend_runtime_contract.py` | 前端 Dockerfile/compose 部署合同静态测试 |

---

## 3. 当前代码和部署事实

### 3.1 服务编排（docker-compose.prod.yml）

14 个服务，与 `docs/maps/deployment-runtime-map.md` 一致：

```
postgres | redis | backend | frontend
worker-bars-scheduler | worker-strategy-scheduler | worker-calendar
worker-monitor | worker-strategy-batch | worker-outbox | worker-delivery
worker-after-close | worker-watchdog | worker-capture
```

- backend 镜像：`market-dev-backend:${GIT_SHA:-unknown}`
- frontend 镜像：`market-dev-frontend:${GIT_SHA:-dev}`
- capture 镜像：`market-dev-capture:${GIT_SHA:-unknown}`（独立 Dockerfile.capture）
- 配置注入：`/etc/market-dev/config.production.py` + `/etc/market-dev/market.env`
- 端口：backend 8000、frontend 80、capture 8001
- 日志：`x-logging` json-file 50m×5
- `BOARD_SYNC_ENABLED` 默认 `false`，注入 worker-after-close

### 3.2 Live Mount（docker-compose.live.yml + scripts/）

已存在并生效（CHANGE-20260724-004）：

- `docker-compose.live.yml`：`!override` 重新声明 volumes；backend + 9 Python worker + capture 共享 `x-live-python` anchor；挂载 `/opt/panji-live/{backend/app,backend/alembic,backend/alembic.ini,RUNTIME_SHA}` + `/etc/market-dev/config.production.py`
- `scripts/sync_live_runtime.sh`：`rsync --delete` 同步运行必需文件，排除 __pycache__/.pytest_cache 等；写 `RUNTIME_SHA` 经 `/tmp` 中转；`--skip-stop` 选项
- `scripts/deploy_live_runtime.sh`：完整编排（前端构建→compose config 校验→sync→alembic→canary backend→force-recreate 全部应用容器）；支持 `--skip-frontend-build`/`--skip-alembic`/`--skip-canary`
- `scripts/deploy.sh`：旧式完整镜像构建部署（build backend/frontend/worker-capture → up postgres/redis → alembic → up 全部），支持 `CORE_ONLY=1`
- `scripts/cleanup-docker.sh`：`KEEP_VERSIONS=2`（当前 + 1 rollback），保护基础镜像

### 3.3 `/version` 端点合同（backend/app/api/health.py）

```json
{
  "git_sha": "<runtime_git_sha>",
  "build_time": "<BUILD_TIME env>",
  "app_version": "1.1.0",
  "alembic_revision": "<DB head>",
  "runtime_git_sha": "<优先 /app/RUNTIME_SHA，回退 image_git_sha>",
  "image_git_sha": "<GIT_SHA env>",
  "deployment_mode": "live | image"
}
```

- 无需认证
- 兼容旧镜像：RUNTIME_SHA 不存在时 `runtime_git_sha = image_git_sha`，`deployment_mode = "image"`
- 部署验证：`runtime_git_sha` 必须等于 main HEAD（live 模式）

### 3.4 健康检查路径

- `GET /health`：200 + `{"status":"ok","service":"trading-platform","version":"1.1.0"}`
- `GET /health/ready`：200/503，策略资产 + 种子就绪检查
- `GET /version`：见 3.3
- capture worker：`http://localhost:8001/health`（compose healthcheck）
- frontend：80 端口

### 3.5 Worker 服务名与 WORKER_TYPE

| Compose 服务 | WORKER_TYPE | 容器名 |
|---|---|---|
| worker-bars-scheduler | bars_scheduler | trading-worker-bars-scheduler |
| worker-strategy-scheduler | strategy_scheduler | trading-worker-strategy-scheduler |
| worker-calendar | calendar_scheduler | trading-worker-calendar |
| worker-monitor | monitor_scheduler | trading-worker-monitor |
| worker-strategy-batch | strategy_batch | trading-worker-strategy-batch |
| worker-outbox | outbox | trading-worker-outbox |
| worker-delivery | delivery | trading-worker-delivery |
| worker-after-close | after_close_orchestrator | trading-worker-after-close |
| worker-watchdog | watchdog | trading-worker-watchdog |
| worker-capture | capture service（独立 image） | trading-worker-capture |

### 3.6 当前 CI（.github/workflows/ci.yml）

触发：`push`（任意分支）/ `pull_request`（→ main）/ `workflow_dispatch`。
并发：`group: ${{ github.workflow }}-${{ github.ref }}`，`cancel-in-progress: true`。

Jobs：architecture-rules / docs-consistency / test-allowlist / ruff-new-files / ruff-baseline-regression / ruff-full-repository-report（continue-on-error）/ mypy-new-files / mypy-baseline-regression / mypy-full-report（continue-on-error）/ alembic-cycle / postgres-integration-tests / frontend-tsc / frontend-lint / frontend-build / frontend-contract-tests / frontend-e2e。

**当前无 dev push 自动部署 workflow**。CI 全部为质量门禁，未触发任何 SSH 部署。

### 3.7 Makefile

`dev` / `backend` / `frontend` / `migrate` / `migrate-new` / `test` / `lint` / `up` / `down` / `docker-build`（带 GIT_SHA/BUILD_TIME/PYPROJECT_LOCK_HASH）/ `docker-up` / `docker-down` / `worker`。

无 `deploy`、`deploy-live`、`sync-live` target。

### 3.8 Migration 当前事实

- `backend/alembic/versions/` 最新为 `067_scheduler_job_runs_lease_epoch_attempt_no.py`
- MANIFEST 标注实现核对基线 `086ebce`（HEAD 祖先）
- migration 由 `scripts/deploy.sh` / `scripts/deploy_live_runtime.sh` 在容器内执行 `alembic upgrade head`，未自动化门禁

---

## 4. sync 草案审计结果

### 4.1 草案包结构（sync/panji_agents_rules_maps_autodeploy_v2/）

```
AGENTS.md                         # PanJi Agent Entry V5（角色识别 + 最高原则 + 必读顺序）
README.md                         # 包说明
IMPLEMENTATION-CHECKLIST.md       # 7 类 30 项落地清单
TREE.txt                          # 完整目录树
rules/                            # 11 份规则文件（00/10/20/30/40/50/60/70/80/85/90 + README）
maps/                             # 完整 maps 系统（current/code/changes/decisions/evidence/migration/restore/runbooks/work + MANIFEST/README）
.github/workflows/deploy-dev.yml  # dev push 自动部署 workflow（SSH forced command）
scripts/deploy/                   # panji-deploy-dev.sh / panji-deploy-gateway.sh / panji-verify-runtime.sh / classify_deployment.py + authorized_keys.example / sudoers.example
tools/check_knowledge_system.py   # 知识系统完整性检查（必填文件 + 链接有效性）
```

### 4.2 直接可采用的内容

| 草案内容 | 评价 | 备注 |
|---|---|---|
| `rules/README.md` 索引格式 | ✅ 直接采用 | 11 文件分类清晰，"同一规则只能有一个正式位置"原则合理 |
| `rules/00-core-governance.md` 事实源优先级 | ✅ 直接采用 | 10 级优先级与 AGENTS §三基本一致，但将 `rules/` 提到第 3 位（高于 maps/MANIFEST） |
| `rules/10-product-domain-invariants.md` 产品边界 | ✅ 直接采用 | 与 AGENTS §七.1-4 一致 |
| `rules/20-market-data-indicators.md` MDAS/复权/Node/SMC/AFC | ✅ 直接采用 | 与 AGENTS §七.5/12/13/14/15/16/17 一致，浓缩为规则形式 |
| `rules/30-access-security.md` 权限与秘密 | ✅ 直接采用 | 当前 main 资格模型 + Capture Token 隔离 + 自动部署 SSH key 边界，与现状不冲突 |
| `rules/40-testing-quality.md` 测试原则 | ✅ 直接采用 | "按风险运行足够测试"比当前 CI 全量门禁更灵活，但需要 CN/Work 区分 |
| `rules/50-git-development-flow.md` dev/main + 提交 | ✅ 直接采用 | 与 AGENTS §九一致，明确禁止 `git add -A/.` 等 |
| `rules/60-trae-work.md` Work 边界 | ✅ 直接采用 | 明确 Work 不接触服务器/DB |
| `rules/70-trae-cn.md` CN 多模式（A-F） | ✅ 直接采用 | 开发/测试/观察/手动部署/排障/紧急修复六模式合理 |
| `rules/85-server-directory-boundaries.md` 三目录 | ✅ 直接采用 | `/root/web_dev` / `/opt/panji-deploy` / `/opt/panji-live` 职责清晰 |
| `rules/90-deprecated-forbidden.md` 禁止恢复项 | ✅ 直接采用 | 16 条与 AGENTS §七一致 |
| `maps/MANIFEST.md` | ⚠️ 替换为简洁版 | 现有 `docs/current/MANIFEST.md` 含实现核对基线 SHA，sync 版本不含基线字段 → 需合并 |
| `maps/current/INDEX.md` | ✅ 直接采用 | 12 文件索引 |
| `maps/current/09-development-deployment-workflow.md` | ✅ 直接采用 | 新增"开发与部署工作流"章节，当前 docs/current 缺此文件 |
| `maps/migration/DOCS-TO-MAPS.md` | ✅ 直接采用 | 8 类迁移原则（rules/current/code/changes/work/runbooks/evidence） |
| `maps/decisions/ADR-0003-DEV-PUSH-AUTO-DEPLOY.md` | ✅ 直接采用 | dev push 自动部署决策记录 |
| `maps/decisions/ADR-0004-THREE-SERVER-DIRECTORIES.md` | ✅ 直接采用 | 三目录决策 |
| `maps/restore/RESTORE-CHECKLIST.md` | ⚠️ 简化版 | 现有 `docs/RESTORE-CHECKLIST.md` 7 节 50+ 检查项更详细，sync 版 9 项过简 |
| `tools/check_knowledge_system.py` | ✅ 直接采用 | 12 必填文件 + 链接检查，可作 CI 补充 |
| `IMPLEMENTATION-CHECKLIST.md` | ✅ 直接采用 | 7 类 30 项落地清单 |
| `maps/runbooks/AUTO-DEPLOY-DEV.md` | ✅ 直接采用 | dev push 自动部署流程图 |
| `maps/runbooks/SERVER-INITIALIZATION.md` | ✅ 直接采用 | 一次性服务器初始化（panji-deploy 用户 + restrict SSH） |
| `maps/runbooks/MANUAL-DEPLOY.md` / `ROLLBACK.md` / `MIGRATION.md` / `INCIDENT.md` / `TRAE-CN-MODES.md` / `TRAE-WORK.md` | ✅ 直接采用 | 操作手册骨架 |
| `maps/work/IMPLEMENTATION-PLAN-AUTO-DEPLOY.md` | ✅ 直接采用 | 7 阶段实施计划 |
| `maps/work/TRAE-CN-IMPLEMENTATION-INSTRUCTION.md` | ✅ 直接采用 | CN 落地指令 15 条 + 6 条不做 |
| `scripts/deploy/classify_deployment.py` | ✅ 直接采用 | 变更分类（blocked/frontend_live/python_live/combined_live/none）逻辑清晰 |

### 4.3 需要根据真实代码修改的内容

| 草案内容 | 现状差异 | 修改方向 |
|---|---|---|
| `rules/80-auto-deployment-data-safety.md` "backend app: Python live" | 当前 `deploy_live_runtime.sh` 已实现 Python live mount，但走 `force-recreate`，不是文件级热替换 | 草案"不重启"措辞需调整为"Python 代码 live mount + 选择性 force-recreate" |
| `.github/workflows/deploy-dev.yml` 前端 build gate `npm ci` | 当前 `frontend/package-lock.json` 存在；`npm run build` 在 ubuntu-latest runner 上耗时 ~30s | 可直接用，但需要确认 `frontend/Dockerfile` 多阶段构建已优化（CHANGE-20260718-003 已做） |
| `scripts/deploy/panji-deploy-dev.sh` 调用 `scripts/deploy_live_runtime.sh` | 当前 `deploy_live_runtime.sh` 假设 `REPO_ROOT` 是 git 仓库根，会执行前端构建 + alembic | 草案脚本在 `/opt/panji-deploy` detached checkout 后调用，需要确认 `frontend/node_modules` 不存在时回退到 `npm run build`（当前脚本已处理） |
| `scripts/deploy/panji-verify-runtime.sh` `http://127.0.0.1:8000/version` + `/api/v1/health` | 当前 `/health` 路径是 `/health`（不是 `/api/v1/health`） | **必须修改**：sync 草案 health URL 错误 |
| `scripts/deploy/panji-verify-runtime.sh` 检查 `welcome to nginx` | 当前 frontend 80 端口已修复为 SPA（CHANGE-20260718-007），不会出现 nginx 默认页 | 检查保留无害，但语义已变化 |
| `scripts/deploy/panji-deploy-dev.sh` `REPO_URL=git@github.com:bao1872/market_dev.git` | 当前仓库 origin 实际地址需 CN 确认 | CN 落地时确认 |
| `scripts/deploy/panji-deploy-dev.sh` `flock -n 9` 锁文件 `/var/lock/panji-deploy.lock` | 当前无并发部署锁机制 | 新增能力，需要 root 创建锁文件 |
| `scripts/deploy/panji-deploy-dev.sh` `python3 "$DEPLOY_DIR/scripts/deploy/classify_deployment.py"` | 当前 `classify_deployment.py` 在 `sync/panji_agents_rules_maps_autodeploy_v2/scripts/deploy/`，不在仓库 `scripts/` | 必须先迁移到仓库 `scripts/deploy/` |
| `classify_deployment.py` `blocked_markers` 包含 `Dockerfile` `docker-compose` `pyproject.toml` `package.json` `nginx.conf` | 当前 `frontend/nginx.conf` 是真实运行配置；`pyproject.toml` 是后端依赖 | 分类正确，但需要补 `backend/alembic/versions/` 已有（草案已含） |
| `classify_deployment.py` runtime 排除 `docs/` `rules/` `maps/` | 当前仓库无 `rules/` `maps/` 顶级目录（在 `docs/` 下） | **必须修改**：迁移完成前应排除 `docs/`；迁移完成后改为排除 `rules/` + `maps/` |
| `maps/MANIFEST.md` "自动部署：dev push" | 当前未实现 | PLANNED 标记 |
| `rules/30-access-security.md` "Capability V2 ... 只能标记 WIP" | 当前 docs/current/open-decisions.md 无 Capability V2 项 | 草案引入新概念，需要 docs/current/02 补充 WIP 章节 |
| `rules/40-testing-quality.md` "自动部署前快速检查" | 当前 CI 已有 architecture/docs/allowlist/ruff/mypy，但 dev push 不触发部署 | 草案 quick-check job 可复用现有 CI 步骤 |
| `AGENTS.md` 角色识别 `echo "${PANJI_EXECUTION_ROLE:-UNSET}"` | 当前无此环境变量约定 | 新增能力，需要 CN/Work 分别设置 |

### 4.4 与当前项目冲突的内容

| 冲突项 | 草案 | 现状 | 处理 |
|---|---|---|---|
| 文档体系结构 | `AGENTS.md + rules/ + maps/`（rules/maps 在仓库根） | `AGENTS.md + docs/`（docs 下 current/maps/changes/...） | **核心冲突**：需要分阶段迁移，禁止 `mv docs maps`，必须按 `maps/migration/DOCS-TO-MAPS.md` 8 类逐文件迁移 |
| `maps/current/02-data-api-access.md` 文件名 | sync 用 `02-data-api-access.md` | 现有 `docs/current/02-data-api-contracts.md` | 命名差异，需统一（建议保留现有 `contracts` 命名） |
| `maps/current/08-indicator-contracts.md` 文件名 | sync 用 `08-indicator-contracts.md` | 现有 `docs/current/08-indicator-calculation-contracts.md` | 同上 |
| `maps/current/ALIGNMENT.md` | sync 用 `ALIGNMENT.md` | 现有 `docs/current/code-doc-alignment.md` | 同上 |
| 事实源优先级 | sync `rules/` 第 3 位（高于 maps/MANIFEST） | AGENTS §三 `docs/current/MANIFEST.md` 第 3 位 | 迁移期保留现有优先级；迁移完成后切换 |
| `rules/40-testing-quality.md` "按风险运行足够测试" | 默认不每次全量 | CI 当前每次 push 全量 | 草案更灵活，但 CI 全量是质量护栏；建议保留 CI 全量 + 草案 quick-check 用于部署前 |
| `AGENTS.md` "完成闭环 ... push dev 自动部署" | 默认 dev push 自动部署 | 当前 dev push 只触发 CI | PLANNED，未实现前不得描述为已生效 |
| `scripts/deploy/panji-deploy-dev.sh` `git@github.com:bao1872/market_dev.git` | 草案 hardcode 仓库 URL | 当前 origin 需确认 | CN 落地确认 |
| `classify_deployment.py` 排除 `rules/` `maps/` | 草案假设 rules/maps 在仓库根 | 当前在 docs/ 下 | 迁移完成前需调整排除规则 |

### 4.5 当前尚未实现、只能标记 PLANNED/WIP 的内容

| 项 | 状态 | 说明 |
|---|---|---|
| dev push 自动部署 | PLANNED | 当前无 deploy-dev.yml workflow，无 SSH forced command，无 panji-deploy 用户 |
| `/opt/panji-deploy` 仓库 | PLANNED | 当前生产部署直接在 `/root/web_dev` 执行 `scripts/deploy_live_runtime.sh` |
| `panji-deploy` 服务器用户 | PLANNED | 当前部署使用 root 或现有用户 |
| SSH forced command（restrict + command） | PLANNED | 当前 SSH 配置未审计 |
| GitHub Actions `tencent-dev` environment + secrets | PLANNED | 当前无 environment 配置 |
| `PANJI_EXECUTION_ROLE` 环境变量 | PLANNED | 当前 Work/CN 无角色识别约定 |
| `rules/` 顶级目录 | PLANNED | 当前在 AGENTS.md §七内联 |
| `maps/` 顶级目录 | PLANNED | 当前在 `docs/maps/` |
| `tools/check_knowledge_system.py` 进入 CI | PLANNED | 当前 CI 用 `tools/check_docs_consistency.py` |
| 部署锁 `/var/lock/panji-deploy.lock` | PLANNED | 当前无并发锁 |
| `/api/v1/health` 路径 | ❌ 错误 | 实际是 `/health`，sync 草案 verify 脚本写错 |
| Capability V2 | WIP | sync 草案 `rules/30` 引入，docs/current/open-decisions 未列 |
| 自动变更分类（unknown → BLOCKED） | PLANNED | 草案 `classify_deployment.py` 逻辑可用，但未接入 CI |
| 部署失败自动回滚 previous SHA | PLANNED | 当前 `deploy_live_runtime.sh` 无自动回滚，仅 canary 验证 |
| main/dev 同时运行两套服务 | 禁止 | sync `rules/90` 明确禁止，与现状一致 |

---

## 5. 规则迁移表（AGENTS.md §七 硬规则 → rules/）

| AGENTS.md 章节 | 内容 | 迁移目标 | 备注 |
|---|---|---|---|
| §一 最高原则 | 闭环 + 六者对齐 | `rules/00-core-governance.md` | 草案已含"事实源"+"分层"，需补"六者对齐" |
| §二 必读入口 | AI-ONBOARDING/MANIFEST/RESTORE-CHECKLIST/AGENTS | 保留在 `AGENTS.md` | 入口必须在 AGENTS，不迁 |
| §三 事实源优先级 | 10 级 | `rules/00-core-governance.md` | 草案已含，但优先级需调整（迁移期保留 docs/current/MANIFEST 第 3 位） |
| §四 修改流程 | 动手前输出 10 项 | `AGENTS.md` §6 修改前最小报告 | 草案已简化为 10 项，保留 |
| §五 CHANGE 规则 | 必填 14 字段 + check_docs_consistency 规则 12 | `rules/40-testing-quality.md` 或 `rules/00` | 草案 40 未明确，需要补 |
| §六 禁止行为 | 12 条 | 分散到 `rules/90-deprecated-forbidden.md` + 各专项 rules | 草案 90 已含部分 |
| §七.1 产品边界 | 不做自动交易等 | `rules/10-product-domain-invariants.md` | 草案已含 |
| §七.2 策略规则 | dsa_selector + watchlist_monitor | `rules/10` | 草案已含 |
| §七.3 DSA 规则 | computable universe + partial_failed 不发布 | `rules/10` | 草案已含 |
| §七.4 自选和监控 | 自动进入监控 + 到期保留 | `rules/10` | 草案已含 |
| §七.5 Node Cluster 固定契约 | 250/4000/2 | `rules/20-market-data-indicators.md` | 草案已含 |
| §七.6 飞书 | Platform App only | `rules/10` + `rules/90` | 草案已含 |
| §七.7 Capture Token | 仅 Capture API | `rules/30-access-security.md` | 草案已含 |
| §七.8 ref/ 隔离 | 禁止运行时 import | `rules/90` + `rules/20` | 草案 90 已含，需补测试规则到 40 |
| §七.9 Migration | 不修改已发布 + upgrade/downgrade/upgrade | `rules/40` | 草案 40 未明确，需补 |
| §七.10 测试期不备份数据库 | 禁止 pg_dump | `rules/80-auto-deployment-data-safety.md` | 草案 80 已含 |
| §七.11 Docker 镜像保护 | node:20-alpine 受保护 | `rules/80` + `rules/90` | 草案 80 已含 |
| §七.12 MDAS SSOT | MDAS 唯一行情出口 | `rules/20` | 草案已含 |
| §七.13 Atomic Chart Snapshot | 单 MDAS 读取 + quote 唯一真源 | `rules/20` | 草案已含 |
| §七.14 SMC FVG 排除 + 严格 time-key | FVG 完全排除 | `rules/20` + `rules/90` | 草案已含 |
| §七.15 Canonical 四链统一调度 | 禁止绕过 Registry | `rules/20` + `rules/90` | 草案已含 |
| §七.16 AFC Core 14 不可改 | 14 项不可修改 | `rules/20` | 草案已含 |
| §七.17 三链五周期一致性 | profile_hash 一致 | `rules/20` | 草案未明确，需补 |
| §七.18 个股详情行情唯一真源 | ChartSnapshot 唯一 | `rules/20` + `rules/90` | 草案已含 |
| §七.19 板块同步降级保护 | pywencai 唯一源 | `rules/20` | 草案未明确，需补 |
| §七.20 文档目录与 CI 门禁 | check_docs_consistency.py | `rules/40` | 草案 40 未明确，需补 |
| §七.21 提交安全与执行模式 | 精确 git add + 前台串行 | `rules/50-git-development-flow.md` | 草案已含 git add，需补前台串行 |
| §七.22 Live Mount 部署规则 | 固定运行目录 + 只读挂载 | `rules/85-server-directory-boundaries.md` + `rules/80` | 草案已含目录边界，需补 Live Mount 细节 |
| §七.23 因子版本追踪与 auto-resume | stamp_factor_reconciliation_version | `rules/20` | 草案未含，需补 |
| §八 质量门禁 | Ruff/Mypy/Docs/Arch/Allow/Sync | `rules/40` | 草案 40 已含 |
| §九 分支与 PR | 独立分支 + PR 说明 | `rules/50` + `rules/90` | 草案已含 dev/main，但 PR 流程简化 |
| §十 完成报告格式 | 5 节 | `AGENTS.md` §9 | 草案已简化为 10 项 |
| §十一 变更历史索引 | CHANGELOG 指向 | `maps/changes/CHANGELOG.md` | 草案已含 |

---

## 6. docs → maps 迁移表

> 原则（来自 `maps/migration/DOCS-TO-MAPS.md`）：禁止机械 `mv docs maps`。强制规范 → rules；当前事实 → maps/current；代码位置 → maps/code；历史 → maps/changes；WIP → maps/work；操作 → maps/runbooks；证据 → maps/evidence。

| 现有路径 | 迁移目标 | 类型 | 备注 |
|---|---|---|---|
| `AGENTS.md`（909 行） | 仓库根 `AGENTS.md`（精简到 ~250 行）+ `rules/` | 强制规则 | §七 23 条硬规则拆到 rules/ |
| `docs/AI-ONBOARDING.md` | `maps/current/AI-ONBOARDING.md` 或保留 `docs/` | 当前事实 | 草案未明确，建议保留 docs/ 入口 |
| `docs/INDEX.md` | `maps/README.md` + `maps/current/INDEX.md` | 当前事实 | 草案已含 |
| `docs/MANIFEST.md`（根级） | 删除或合并到 `maps/MANIFEST.md` | 重复 | 现有 `docs/current/MANIFEST.md` 是真源 |
| `docs/README.md` | `maps/README.md` | 当前事实 | 草案已含 |
| `docs/RESTORE-CHECKLIST.md` | `maps/restore/RESTORE-CHECKLIST.md` | 操作 | 草案简化版，需保留现有 7 节详细内容 |
| `docs/MAINTENANCE.md` | `maps/MAINTENANCE.md` | 当前事实 | 草案未含 |
| `docs/MIGRATION-MAP.md` | `maps/migration/DOCS-TO-MAPS.md` | 操作 | 草案已含简化版 |
| `docs/SOURCE-SNAPSHOT.md` | `maps/archive/SOURCE-SNAPSHOT.md` | 历史 | 草案未含 |
| `docs/TRAE-APPLY-INSTRUCTION.md` | `maps/work/TRAE-APPLY-INSTRUCTION.md` | WIP | 草案未含 |
| `docs/current/MANIFEST.md` | `maps/MANIFEST.md` | 当前事实 | **关键**：必须保留实现核对基线 SHA 字段，sync 版本不含 |
| `docs/current/00-product-business.md` | `maps/current/00-product-business.md` | 当前事实 | 直接迁移 |
| `docs/current/01-system-architecture.md` | `maps/current/01-system-architecture.md` | 当前事实 | 直接迁移 |
| `docs/current/02-data-api-contracts.md` | `maps/current/02-data-api-contracts.md` | 当前事实 | 保留 `contracts` 命名（不用 sync 的 `access`） |
| `docs/current/03-jobs-integrations-operations.md` | `maps/current/03-jobs-integrations-operations.md` | 当前事实 | 直接迁移 |
| `docs/current/04-frontend-ux.md` | `maps/current/04-frontend-ux.md` | 当前事实 | 直接迁移 |
| `docs/current/05-testing-acceptance.md` | `maps/current/05-testing-acceptance.md` | 当前事实 | 163KB，直接迁移 |
| `docs/current/06-research-feature-matrix.md` | `maps/current/06-research-feature-matrix.md` | 当前事实 | 直接迁移 |
| `docs/current/07-atomic-fact-contract-v1.md` | `maps/current/07-atomic-fact-contract-v1.md` | 当前事实 | 直接迁移 |
| `docs/current/08-indicator-calculation-contracts.md` | `maps/current/08-indicator-calculation-contracts.md` | 当前事实 | 保留 `calculation` 命名（不用 sync 的 `contracts`） |
| `docs/current/code-doc-alignment.md` | `maps/current/ALIGNMENT.md` | 当前事实 | 草案用 ALIGNMENT.md，建议保留 `code-doc-alignment` 命名 |
| `docs/current/open-decisions.md` | `maps/current/OPEN-DECISIONS.md` | 当前事实 | 草案用大写，建议保留小写 |
| `docs/maps/api-route-map.md` | `maps/code/api-route-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/backend-module-map.md` | `maps/code/backend-module-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/database-model-map.md` | `maps/code/database-model-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/deployment-runtime-map.md` | `maps/code/deployment-runtime-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/frontend-route-map.md` | `maps/code/frontend-route-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/indicator-computation-map.md` | `maps/code/indicator-computation-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/notification-flow-map.md` | `maps/code/notification-flow-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/smc-pine-parity-map.md` | `maps/code/smc-pine-parity-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/test-coverage-map.md` | `maps/code/test-coverage-map.md` | 代码位置 | 直接迁移 |
| `docs/maps/worker-job-map.md` | `maps/code/worker-job-map.md` | 代码位置 | 直接迁移 |
| `docs/changes/CHANGELOG.md` | `maps/changes/CHANGELOG.md` | 历史 | 直接迁移 |
| `docs/changes/records/*.md`（113 条） | `maps/changes/records/*.md` | 历史 | 直接迁移 |
| `docs/changes/CHANGE-TEMPLATE.md` | `maps/changes/records/CHANGE-TEMPLATE.md` | 历史 | 直接迁移 |
| `docs/archive/current-legacy-20260703/` | `maps/archive/current-legacy-20260703/` | 历史 | 直接迁移 |
| `docs/contracts/*.json/yaml/md`（7 份） | `maps/contracts/` 或保留 `docs/contracts/` | 机器可执行合同 | 草案未明确，建议保留 `docs/contracts/` 或迁到 `maps/contracts/` |
| `docs/decisions/ADR-*.md`（3 条） | `maps/decisions/ADR-*.md` | 架构决策 | 直接迁移，需补 ADR-0003（sync 已用 ADR-0003 编号给 dev push 自动部署，**冲突**：现有 ADR-0003 是 smc-strict-time-key） |
| `docs/runbooks/*.md`（4 份） | `maps/runbooks/*.md` | 操作 | 直接迁移 |
| `docs/acceptance/ci-gates.md` | `maps/acceptance/ci-gates.md` | 验收 | 直接迁移 |
| `docs/evidence/*.md` | `maps/evidence/*.md` | 证据 | 直接迁移 |
| `docs/work/*.md`（4 份 PRD/checkpoint） | `maps/work/*.md` | WIP | 直接迁移 |

**ADR 编号冲突**：现有 `docs/decisions/ADR-0003-smc-strict-time-key.md` 已占用 0003；sync 草案 `maps/decisions/ADR-0003-DEV-PUSH-AUTO-DEPLOY.md` 重复使用 0003。**必须重新编号**：sync 的 dev push ADR 应改为 ADR-0005 或更高。

---

## 7. AGENTS 重构表

### 7.1 必须保留在根 AGENTS.md

| 章节 | 原因 |
|---|---|
| 最高原则（六者对齐） | 项目核心护栏 |
| 必读入口（AI-ONBOARDING/MANIFEST/RESTORE-CHECKLIST/AGENTS） | Agent 入口 |
| 角色识别（PANJI_EXECUTION_ROLE） | 新增，Work/CN 区分 |
| 修改前最小报告（10 项） | 任务前置 |
| 绝对禁止（最高安全门禁） | 不可分散 |
| 完成闭环 + 完成报告 | 流程闭环 |
| 服务器目录职责（三目录） | 边界 |

### 7.2 应迁移到 rules/

| AGENTS.md 章节 | rules/ 目标 |
|---|---|
| §三 事实源优先级 | `rules/00-core-governance.md` |
| §五 CHANGE 规则 | `rules/40-testing-quality.md` |
| §六 禁止行为 12 条 | 分散到 `rules/90` + 各专项 |
| §七.1-4 产品/策略/DSA/自选 | `rules/10-product-domain-invariants.md` |
| §七.5 Node Cluster | `rules/20-market-data-indicators.md` |
| §七.6 飞书 | `rules/10` + `rules/90` |
| §七.7 Capture Token | `rules/30-access-security.md` |
| §七.8 ref/ 隔离 | `rules/90` + `rules/40`（测试） |
| §七.9 Migration | `rules/40` |
| §七.10 测试期不备份 | `rules/80-auto-deployment-data-safety.md` |
| §七.11 Docker 镜像保护 | `rules/80` + `rules/90` |
| §七.12 MDAS SSOT | `rules/20` |
| §七.13 Atomic Chart Snapshot | `rules/20` |
| §七.14 SMC FVG + time-key | `rules/20` + `rules/90` |
| §七.15 Canonical 四链 | `rules/20` + `rules/90` |
| §七.16 AFC Core 14 | `rules/20` |
| §七.17 三链五周期一致性 | `rules/20` |
| §七.18 个股详情行情唯一真源 | `rules/20` + `rules/90` |
| §七.19 板块同步降级保护 | `rules/20` |
| §七.20 文档目录与 CI 门禁 | `rules/40` |
| §七.21 提交安全与执行模式 | `rules/50-git-development-flow.md` |
| §七.22 Live Mount 部署规则 | `rules/85-server-directory-boundaries.md` + `rules/80` |
| §七.23 因子版本追踪 | `rules/20` |
| §八 质量门禁 | `rules/40` |
| §九 分支与 PR | `rules/50` + `rules/90` |

### 7.3 应迁移到 maps/

| AGENTS.md 章节 | maps/ 目标 |
|---|---|
| §四 修改流程示例 | `maps/work/`（任务模板） |
| §十一 变更历史索引 | `maps/changes/CHANGELOG.md` 指向 |
| 角色识别详细（Work/CN 必读列表） | `maps/runbooks/TRAE-WORK.md` + `maps/runbooks/TRAE-CN-MODES.md` |

### 7.4 已过时，应删除

| 内容 | 原因 |
|---|---|
| 无 | AGENTS.md v3（CHANGE-20260722-001 收口后 290 行）已精简，无过时内容 |

注：当前 AGENTS.md 实际 909 行（system-reminder 显示），但 CHANGE-20260722-001 称已压缩到 290 行。需核对——可能是 system-reminder 显示的是含 §七 23 条详细规则的完整版，而 290 行是收口后的精简版。本审计以 system-reminder 显示的 909 行版本为准。

---

## 8. 自动部署差异表

| 项 | sync 草案 | 当前现状 | 差异处理 |
|---|---|---|---|
| `docker-compose.prod.yml` | 不修改，作为 base | 14 服务，`market-dev-backend:${GIT_SHA}` 镜像 | 一致，无需修改 |
| `docker-compose.live.yml` | 不修改，叠加 prod | `!override` 重新声明 volumes，`x-live-python` anchor | 一致，无需修改 |
| `scripts/deploy_live_runtime.sh` | 草案 `panji-deploy-dev.sh` 调用它 | 当前脚本：前端构建→compose config→sync→alembic→canary→force-recreate 全部 | **草案假设它在 `/opt/panji-deploy/scripts/`**；当前在仓库 `scripts/`。CN 落地时 `/opt/panji-deploy` 是 detached checkout，`scripts/deploy_live_runtime.sh` 路径正确 |
| `scripts/sync_live_runtime.sh` | 草案未直接调用 | 当前脚本：rsync --delete 同步 app/alembic/alembic.ini/RUNTIME_SHA/frontend dist | 由 `deploy_live_runtime.sh` 间接调用，无需修改 |
| `/version` 合同 | 草案 `panji-verify-runtime.sh` 检查 `runtime_git_sha` 字段 | 当前 `/version` 返回 `runtime_git_sha`/`image_git_sha`/`deployment_mode` | 一致 |
| health 路径 | 草案 `http://127.0.0.1:8000/api/v1/health` | 当前 `http://127.0.0.1:8000/health`（`backend/app/api/health.py` 路由 `/health`） | **❌ 草案错误**：必须改为 `/health` |
| Worker 服务名 | 草案未明确列出 | 14 服务（postgres/redis/backend/frontend + 10 worker） | 草案 `panji-deploy-dev.sh` 通过 `deploy_live_runtime.sh` 间接 force-recreate 全部，无需列出 |
| GitHub Actions | 草案 `.github/workflows/deploy-dev.yml` | 当前 `.github/workflows/ci.yml`（仅质量门禁） | **新增 workflow**，不修改现有 ci.yml；需要 GitHub Environment `tencent-dev` + secrets |
| SSH forced command | 草案 `panji-deploy-gateway.sh` | 当前无 | **新增**：`/usr/local/sbin/panji-deploy-gateway` + `authorized_keys` `restrict,command=...` |
| 部署用户 | 草案 `panji-deploy` 用户 | 当前 root 或现有用户 | **新增**：专用 deploy 用户，不用于人工登录 |
| 部署锁 | 草案 `flock -n 9 /var/lock/panji-deploy.lock` | 当前无 | **新增**：并发部署互斥 |
| 变更分类 | 草案 `classify_deployment.py` | 当前无 | **新增**：blocked/frontend_live/python_live/combined_live/none |
| 自动回滚 | 草案 `panji-deploy-dev.sh` 记录 PREVIOUS_SHA | 当前 `deploy_live_runtime.sh` 无自动回滚 | **新增能力**：部署失败回滚 previous SHA |
| `frontend/package-lock.json` | 草案 quick-check `npm ci` | 当前存在 | 一致 |
| `pyproject.toml` | 草案 blocked_markers | 当前存在 | 一致，依赖变更阻塞 |
| `BOARD_SYNC_ENABLED` | 草案未涉及 | 当前 `docker-compose.prod.yml` worker-after-close 注入 | 不冲突 |
| Capture worker | 草案 `x-live-python` 包含 worker-capture | 当前 `docker-compose.live.yml` 已含 | 一致 |
| `/opt/panji-live/RUNTIME_SHA` | 草案 verify 检查 | 当前 `sync_live_runtime.sh` 写入 | 一致 |
| Makefile | 草案未涉及 | 当前无 `deploy` target | 不冲突，部署走 `scripts/` |

---

## 9. 风险和冲突

### 9.1 高风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 文档体系迁移（docs → maps）破坏 CI `check_docs_consistency.py` 规则 11（docs 顶层目录白名单） | CI 阻断 | 分阶段迁移，每阶段同步更新 `check_docs_consistency.py` 与 `check_knowledge_system.py` |
| ADR 编号冲突（0003 已用于 smc-strict-time-key） | 历史引用断裂 | sync 的 dev push ADR 改为 ADR-0005 |
| `classify_deployment.py` 排除 `rules/` `maps/` 但当前在 `docs/` 下 | 迁移期分类错误 | 迁移完成前调整排除规则为 `docs/` |
| dev push 自动部署在 migration 未审核时继续执行 | 数据库损坏 | `classify_deployment.py` 已含 `backend/alembic/versions/` blocked_markers，但需要 CN 真实验证 |
| SSH forced command 配置错误 | 服务器被入侵 | 严格按 `maps/runbooks/SERVER-INITIALIZATION.md` 配置 `restrict,command=...` |
| `/opt/panji-deploy` dirty 检查失败 | 部署卡住 | `panji-deploy-dev.sh` 已含 `git status --porcelain` 检查 |
| 部署失败自动回滚 previous SHA 但 migration 已执行 | 数据库与应用版本不匹配 | `rules/80` 明确"migration 不自动回滚"，需要 CN 人工介入 |

### 9.2 中风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| AGENTS.md 909 行 vs 290 行版本不一致 | Agent 读取混乱 | 核对仓库实际文件行数，统一为收口版 |
| sync `rules/30` 引入 Capability V2 概念 | 与 docs/current 不一致 | 迁移时在 `maps/current/02` 补充 WIP 章节 |
| sync `rules/40` "按风险运行足够测试" | CI 全量门禁可能被弱化 | 保留 CI 全量 + 草案 quick-check 用于部署前 |
| `PANJI_EXECUTION_ROLE` 未设置 | 角色未知，只读检查 | `AGENTS.md` §2 明确"角色未设置时只允许只读检查" |
| `frontend/node_modules` 在 `/opt/panji-deploy` 不存在 | `npm run build` 失败 | `deploy_live_runtime.sh` 已回退到 `npm run build`（需 `npm ci` 先） |
| GitHub Actions secrets 泄露 | 服务器被入侵 | `rules/30` 明确"部署 SSH Key 专用 + forced command + 不读取数据库秘密" |

### 9.3 低风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 文件命名差异（02-data-api-access vs 02-data-api-contracts） | 迁移期引用断裂 | 保留现有 `contracts` 命名 |
| `maps/MANIFEST.md` 不含实现核对基线 SHA | check_docs_consistency 规则 1 失败 | 合并现有 MANIFEST 基线字段 |
| `docs/RESTORE-CHECKLIST.md` 7 节 vs sync 9 项 | 恢复检查不完整 | 保留现有详细版 |
| `/api/v1/health` vs `/health` | verify 脚本失败 | 修正 sync 脚本 |

### 9.4 冲突项

| 冲突 | 草案 | 现状 | 处理 |
|---|---|---|---|
| 文档体系 | `AGENTS.md + rules/ + maps/` | `AGENTS.md + docs/` | 分阶段迁移 |
| ADR-0003 编号 | dev push 自动部署 | smc-strict-time-key | sync 改为 ADR-0005 |
| 事实源优先级第 3 位 | `rules/` | `docs/current/MANIFEST.md` | 迁移期保留现状 |
| `02-data-api-access.md` 命名 | sync | `02-data-api-contracts.md` 现有 | 保留现有 |
| `08-indicator-contracts.md` 命名 | sync | `08-indicator-calculation-contracts.md` 现有 | 保留现有 |
| `ALIGNMENT.md` 命名 | sync 大写 | `code-doc-alignment.md` 现有小写 | 保留现有 |
| `/api/v1/health` 路径 | sync | `/health` 现有 | 修正 sync |

---

## 10. 分阶段实施计划

每阶段独立可检查、可停止。本阶段（Phase 0）只读审计，不动代码。

### Phase 0：审计与设计（本阶段，已完成）

- ✅ 清点现有 AGENTS、docs、检查工具、部署文件
- ✅ 分类现有内容（强制规则/当前事实/代码位置/历史变更/进行中工作/操作手册/验证证据/已废弃）
- ✅ 对比 sync 草案与当前代码
- ✅ 设计 docs → rules/maps 迁移表
- ✅ AGENTS 重构表
- ✅ 自动部署差异表
- ✅ 输出本审计报告

**检查点**：本报告完整 + 用户确认。
**停止条件**：用户未确认前不进入 Phase 1。

### Phase 1：rules/ 顶级目录建立（独立分支）

- 新建 `docs/governance/rules/`（不直接放仓库根，避免与 docs/ 冲突）
- 从 AGENTS §七 23 条硬规则逐条迁移到 `docs/governance/rules/00-core-governance.md` 等 11 文件
- 不修改 AGENTS.md（保留现有内容）
- 不删除 docs/
- 新增 `docs/governance/rules/README.md` 索引
- 更新 `tools/check_docs_consistency.py` 规则 11 白名单允许 `docs/governance/`
- 新增 `tools/check_knowledge_system.py`（从 sync 迁移）
- 新增 CHANGE 记录

**检查点**：`python tools/check_docs_consistency.py` 通过 + `python tools/check_knowledge_system.py` 通过 + 现有 CI 全绿。
**停止条件**：任一检查失败则停止。

### Phase 2：AGENTS.md 精简（独立分支）

- 依赖 Phase 1 完成
- AGENTS.md 从 909 行精简到 ~250 行
- 保留：最高原则、必读入口、角色识别、修改前最小报告、绝对禁止、完成闭环、完成报告、服务器目录职责
- 迁移：§七 23 条硬规则指向 `docs/governance/rules/`
- 不删除任何规则内容
- 更新 `docs/AI-ONBOARDING.md` 指向新 rules/
- 新增 CHANGE 记录

**检查点**：AGENTS.md 行数 ≤ 300 + 规则内容 100% 在 rules/ 可查 + CI 全绿。
**停止条件**：规则内容丢失则停止。

### Phase 3：maps/ 顶级目录建立（独立分支）

- 依赖 Phase 2 完成
- 新建 `docs/governance/maps/`（不直接放仓库根）
- 从 `docs/maps/` 迁移 10 份代码地图到 `docs/governance/maps/code/`
- 从 `docs/current/` 迁移 9 份 + MANIFEST + code-doc-alignment + open-decisions 到 `docs/governance/maps/current/`
- 从 `docs/changes/` 迁移到 `docs/governance/maps/changes/`
- 从 `docs/runbooks/` 迁移到 `docs/governance/maps/runbooks/`
- 从 `docs/decisions/` 迁移到 `docs/governance/maps/decisions/`（ADR-0003 编号保留，sync 的 dev push ADR 改为 ADR-0005）
- 从 `docs/evidence/` 迁移到 `docs/governance/maps/evidence/`
- 从 `docs/work/` 迁移到 `docs/governance/maps/work/`
- 从 `docs/acceptance/` 迁移到 `docs/governance/maps/acceptance/`
- 从 `docs/contracts/` 迁移到 `docs/governance/maps/contracts/`
- 从 `docs/archive/` 迁移到 `docs/governance/maps/archive/`
- 保留 `docs/AI-ONBOARDING.md` `docs/INDEX.md` `docs/README.md` `docs/RESTORE-CHECKLIST.md` 等根 .md
- 更新所有内部链接
- 更新 `tools/check_docs_consistency.py` 路径
- 新增 CHANGE 记录

**检查点**：`python tools/check_docs_consistency.py` 通过 + 所有内部链接有效 + CI 全绿。
**停止条件**：链接断裂或检查失败则停止。

### Phase 4：自动部署脚本落地（独立分支，CN 执行）

- 依赖 Phase 3 完成
- 从 sync 迁移 `scripts/deploy/classify_deployment.py` 到仓库 `scripts/deploy/`
- 从 sync 迁移 `scripts/deploy/panji-deploy-dev.sh` 到仓库 `scripts/deploy/`（修正 health URL 为 `/health`）
- 从 sync 迁移 `scripts/deploy/panji-deploy-gateway.sh` 到仓库 `scripts/deploy/`
- 从 sync 迁移 `scripts/deploy/panji-verify-runtime.sh` 到仓库 `scripts/deploy/`（修正 health URL 为 `/health`）
- 不修改现有 `scripts/deploy_live_runtime.sh` `scripts/sync_live_runtime.sh`
- 不启用 GitHub Actions
- 新增 CHANGE 记录

**检查点**：脚本静态检查通过 + `bash -n` 语法检查 + CN 真实测试（dry-run）。
**停止条件**：脚本错误则停止。

### Phase 5：GitHub Actions 自动部署 workflow（独立分支）

- 依赖 Phase 4 完成 + 用户明确授权
- 从 sync 迁移 `.github/workflows/deploy-dev.yml`
- 不修改现有 `.github/workflows/ci.yml`
- 新增 GitHub Environment `tencent-dev`
- 新增 secrets：`TENCENT_DEPLOY_KEY` `TENCENT_DEPLOY_HOST` `TENCENT_DEPLOY_PORT` `TENCENT_DEPLOY_USER`
- workflow permissions `contents: read` only
- 不在 workflow 存数据库秘密
- **不启用 workflow**（push dev 后不自动触发，需用户手动 enable）
- 新增 CHANGE 记录

**检查点**：workflow YAML 语法检查 + GitHub Actions dry-run + 用户确认。
**停止条件**：用户未授权则不进入此阶段。

### Phase 6：服务器初始化（CN 执行，不进仓库）

- 依赖 Phase 5 完成 + 用户明确授权
- 创建 `panji-deploy` 用户
- 配置 `authorized_keys` `restrict,command="/usr/local/sbin/panji-deploy-gateway"`
- 安装 `/usr/local/sbin/panji-deploy-gateway` + `/usr/local/lib/panji-deploy/panji-deploy-dev.sh`
- 配置 sudoers（最小权限）
- 创建 `/var/lock/panji-deploy.lock`
- 克隆 `/opt/panji-deploy`（clean，detached）
- 核对 `/opt/panji-live` 与 `docker-compose.live.yml` 一致
- 保留 `/root/web_dev` 现有开发目录

**检查点**：SSH forced command 测试 + `/opt/panji-deploy` clean 检查 + `/version` 返回正确 SHA。
**停止条件**：SSH 配置错误或服务器异常则停止。

### Phase 7：自动部署试运行 + 收口

- 依赖 Phase 6 完成 + 用户明确授权
- dev push 触发自动部署（先 docs-only 变更试运行）
- 验证 4 组测试：docs-only / frontend-only / backend-only / migration-blocked
- 验证并发 push 互斥
- 验证失败回滚
- 记录 evidence
- 合并 dev → main 作为阶段稳定锚点
- 新增 CHANGE 记录

**检查点**：4 组测试全通过 + evidence 记录完整 + 用户确认。
**停止条件**：任一测试失败则停止并回滚。

---

## 11. 第一阶段建议修改文件

**本阶段（Phase 0）不修改任何正式文件**。仅创建本审计报告：

| 文件 | 操作 | 说明 |
|---|---|---|
| `sync/outbox/project-governance-audit.md` | 新增 | 本报告 |

Phase 1 建议修改文件（待用户确认后执行）：

| 文件 | 操作 | 说明 |
|---|---|---|
| `docs/governance/rules/README.md` | 新增 | rules 索引 |
| `docs/governance/rules/00-core-governance.md` | 新增 | 事实源 + 边界 |
| `docs/governance/rules/10-product-domain-invariants.md` | 新增 | 产品不变量 |
| `docs/governance/rules/20-market-data-indicators.md` | 新增 | MDAS/复权/Node/SMC/AFC |
| `docs/governance/rules/30-access-security.md` | 新增 | 权限与秘密 |
| `docs/governance/rules/40-testing-quality.md` | 新增 | 测试和质量 |
| `docs/governance/rules/50-git-development-flow.md` | 新增 | dev/main + 提交 |
| `docs/governance/rules/60-trae-work.md` | 新增 | Work 规则 |
| `docs/governance/rules/70-trae-cn.md` | 新增 | CN 多模式 |
| `docs/governance/rules/80-auto-deployment-data-safety.md` | 新增 | 自动部署和数据安全 |
| `docs/governance/rules/85-server-directory-boundaries.md` | 新增 | 三目录职责 |
| `docs/governance/rules/90-deprecated-forbidden.md` | 新增 | 禁止恢复项 |
| `tools/check_docs_consistency.py` | 修改 | 规则 11 白名单允许 `docs/governance/` |
| `tools/check_knowledge_system.py` | 新增 | 知识系统完整性检查 |
| `.github/workflows/ci.yml` | 修改 | 新增 `knowledge-system` job |
| `docs/changes/records/CHANGE-YYYYMMDD-NNN.md` | 新增 | CHANGE 记录 |
| `docs/changes/CHANGELOG.md` | 修改 | 索引更新 |

---

## 12. 明确不修改范围

本阶段（Phase 0）**绝对不修改**以下内容：

| 范围 | 说明 |
|---|---|
| 根 `AGENTS.md` | 不覆盖、不精简、不重写 |
| `docs/current/` 全部 | 不删除、不移动、不重命名 |
| `docs/maps/` 全部 | 不删除、不移动 |
| `docs/changes/` 全部 | 不删除、不移动 |
| `docs/runbooks/` 全部 | 不删除、不移动 |
| `docs/decisions/` 全部 | 不删除、不移动 |
| `docs/contracts/` 全部 | 不删除、不移动 |
| `docs/evidence/` 全部 | 不删除、不移动 |
| `docs/work/` 全部 | 不删除、不移动 |
| `docs/archive/` 全部 | 不删除、不移动 |
| `docs/acceptance/` 全部 | 不删除、不移动 |
| `docs/` 根 .md 文件 | 不删除、不移动 |
| 仓库根 `rules/` `maps/` 顶级目录 | 不创建（Phase 1-3 在 `docs/governance/` 下） |
| `.github/workflows/ci.yml` | 不修改 |
| `.github/workflows/deploy-dev.yml` | 不创建、不启用 |
| `docker-compose.prod.yml` | 不修改 |
| `docker-compose.live.yml` | 不修改 |
| `docker-compose.yml` | 不修改 |
| `scripts/deploy_live_runtime.sh` | 不修改 |
| `scripts/sync_live_runtime.sh` | 不修改 |
| `scripts/deploy.sh` | 不修改 |
| `scripts/cleanup-docker.sh` | 不修改 |
| `scripts/deploy/` | 不创建 |
| `Makefile` | 不修改 |
| `backend/` 全部代码 | 不修改 |
| `frontend/` 全部代码 | 不修改 |
| `tools/check_architecture.py` | 不修改 |
| `tools/check_test_allowlist.py` | 不修改 |
| 任何 migration | 不执行 |
| 任何生产环境配置 | 不修改 |
| 任何服务器配置 | 不修改 |
| 任何 GitHub Actions secret | 不创建 |
| 任何 SSH key | 不创建 |
| 任何飞书配置 | 不修改 |
| 任何数据库 | 不连接、不修改 |

**Git 操作限制**：
- 不 `git add -A` / `git add .` / `git add -u`
- 不 commit
- 不 push
- 不切换分支（固定 dev）
- 不 force push
- 不修改 git 历史

**部署限制**：
- 不连接腾讯云
- 不执行任何部署脚本
- 不启动/停止任何容器
- 不运行全量 E2E

---

## 报告摘要

本审计报告基于 dev 分支 HEAD `06bf510` 的真实代码与现有正式 docs，对 `sync/panji_agents_rules_maps_autodeploy_v2/` 治理文档包进行了完整对比。

**当前事实**：仓库已有完整的 `AGENTS.md`（909 行 v3）+ `docs/` 体系（current/maps/changes/runbooks/decisions/evidence/work/contracts/acceptance/archive）+ 14 服务 docker-compose.prod.yml + Live Mount 部署（CHANGE-20260724-004）+ CI 全量门禁。当前 dev push 只触发 CI 质量门禁，无自动部署。

**sync 草案评价**：草案是目标设计，不是已生效规则。11 份 rules 文件分类清晰，可直接采用；maps 结构合理但与现有 docs/ 命名有差异；自动部署脚本（gateway/classify/verify）逻辑可用但需修正 health URL（`/api/v1/health` → `/health`）；ADR-0003 编号与现有 smc-strict-time-key 冲突，sync 的 dev push ADR 应改为 ADR-0005。

**核心冲突**：文档体系从 `AGENTS.md + docs/` 迁移到 `AGENTS.md + rules/ + maps/` 是大范围重构，必须分阶段进行，禁止 `mv docs maps`。建议在 `docs/governance/` 下先建立 rules/ 和 maps/，避免与现有 docs/ 冲突，迁移完成后再考虑提升为顶级目录。

**实施计划**：7 阶段（Phase 0-7），每阶段独立可检查可停止。Phase 0（本阶段）只读审计已完成；Phase 1 建立 rules/；Phase 2 精简 AGENTS.md；Phase 3 建立 maps/；Phase 4 落地自动部署脚本；Phase 5 启用 GitHub Actions；Phase 6 服务器初始化；Phase 7 试运行 + 收口。Phase 5-7 需用户明确授权。

**第一阶段建议**：仅创建本审计报告，不修改任何正式文件。等待用户确认后进入 Phase 1（rules/ 建立）。

**绝对禁止**：不覆盖根 AGENTS.md、不删除/移动现有 docs、不创建正式 rules/maps（顶级）、不启用 GitHub Actions、不修改 Compose/部署脚本/服务器配置、不连接腾讯云/数据库/飞书、不执行 migration、不运行全量 E2E、不批量 git add、不 commit、不 push、不把 sync 草案描述为已生效、不为符合草案修改业务代码。

**审计完成，等待用户确认。**
