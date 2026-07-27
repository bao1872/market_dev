# CHANGE-20260727-001：Git 分支治理与 PRD20/30 代码对齐审计

状态：已完成（审计阶段，未修改业务代码，未启用自动部署）  
日期：2026-07-27  
对应 PRD：`docs/prd/80-system-runtime.md`、`docs/prd/20-quant-model.md`、`docs/prd/30-after-close.md`  
对应 Map：`docs/maps/80-system-runtime.md`、`docs/maps/20-quant-model.md`、`docs/maps/30-after-close.md`、`docs/maps/00-system-overview.md`

## 1. 变更原因

- 仓库历史分支过多，存在误用旧分支和脏工作区风险；
- PRD20（量化模型）和 PRD30（盘后任务）需要与真实代码形成可核对的差异地图；
- 需要把 Phase 4 的分支治理结果和代码审计事实写入 Maps，供后续开发作为入口。

## 2. 分支治理结果

### 2.1 长期分支策略

已在 `docs/prd/80-system-runtime.md` 增加 `SR-09 长期分支策略`：

| 分支 | 职责 | 进入方式 |
|---|---|---|
| `main` | 稳定版本，对应远程生产运行 | 只接受 PR 合并 |
| `dev` | 日常开发与集成 | 直接在本地 `dev` 提交 |
| `experiment` | 未确认实验、未完成代码 | 从 `dev` 创建 |

本地、`origin` 和远程服务器最终仅保留 `main`/`dev`/`experiment`。

### 2.2 分支前后表

| 范围 | 清理前 | 清理后 |
|---|---|---|
| 本地 | `dev`、`main`、`experiment` + 多个历史分支 | `main`、`dev`、`experiment` |
| origin | `dev`、`main` + 多个历史分支 | `dev`、`main` |
| 服务器 `/root/web_dev` | `refactor/invite-capability-access-v2`（脏工作区）等 | `main`、`dev`、`experiment`；当前检出 `main`，工作区干净 |

### 2.3 删除分支与归档标签

| 原分支 | tip SHA（archive tag） | 相对 dev/main 唯一 patch 数 | 删除理由 |
|---|---|---|---|
| `origin/experiment/bar-temporal-regime-discovery-v1` | `ec495f28` | 6 | 有唯一提交，已归档 |
| `origin/experiment/hierarchical-scene-state-v3` | `93f247fc` | 1 | 有唯一提交，已归档 |
| `origin/feat/portal-replacement-v1` | `99273a29` | 2 | 有唯一提交，已归档 |
| `origin/fix/portal-qr-update` | `10b03453` | 0 | 已 patch 等价，直接删除 |
| `origin/refactor/invite-capability-access-v2` | `0ce0c805` | 6 | 有唯一提交，已归档 |
| 服务器 `fix/stock-detail-market-data-v1` | `eefbe105` | 0 | 已 patch 等价，直接删除 |
| 服务器 `refactor/unified-feature-computation-v1` | `dd25dfb7` | 8 | 有唯一提交，已归档 |
| 服务器本地 `refactor/invite-capability-access-v2`（含未提交修改） | `0f17e7d3` | 12 | 有唯一提交和脏工作区，已归档为 `archive/server-refactor-invite-capability-access-v2-20260727` |

已创建并推送的 annotated archive tags：

- `archive/experiment-bar-temporal-regime-discovery-v1-20260727`
- `archive/experiment-hierarchical-scene-state-v3-20260727`
- `archive/feat-portal-replacement-v1-20260727`
- `archive/fix-portal-qr-update-20260727`
- `archive/refactor-invite-capability-access-v2-20260727`
- `archive/fix-stock-detail-market-data-v1-20260727`
- `archive/refactor-unified-feature-computation-v1-20260727`
- `archive/server-refactor-invite-capability-access-v2-20260727`

### 2.4 服务器脏工作区处理

- 服务器原检出 `refactor/invite-capability-access-v2`，HEAD `0f17e7d`，存在未提交修改；
- 已做密钥扫描，未发现密码/私钥；存在只读数据库 URL，不含敏感凭证；
- 已创建 `archive/server-refactor-invite-capability-access-v2-20260727` 保存服务器本地 tip（含 merge commit）；
- 已比较修改与 `origin/dev`、`origin/experiment` 的差异，无需要保留的未提交源码；
- 可重建的生成/日志/缓存文件已清理；
- 已切换服务器到 `main`，工作区干净，当前运行版本仍为 `origin/main` `13a0ef3`。

### 2.5 阻塞

无阻塞。

## 3. PRD20/30 代码对齐审计摘要

本轮未修改业务代码，仅基于真实代码、测试和运行结果记录实现状态。

### 3.1 PRD20 量化模型

| PRD 条款 | 状态 | 关键证据 |
|---|---|---|
| QM-01~QM-03 维度顺序、必选/可选、文字化 | 已实现并核验 | `atomic_fact_contract_service.py`、JSON 合同、相关测试 |
| QM-10~QM-13 趋势 / DSA VWAP | 已实现并核验 | `dsa_selector.py`、`dynamic_swing_anchored_vwap.py`、DSA 测试 |
| QM-20~QM-23 结构 / SMC Pine | 已实现并核验 | `smc_pine_core.py`、Pine 对齐测试 |
| QM-30~QM-33 动量 / Bollinger | 已实现并核验 | `bollinger_features_plotly.py`、`sqzmom_lb.py` |
| QM-40~QM-43 筹码共识 / Node Cluster | 已实现并核验 | `node_cluster_engine.py`、架构守护测试 |
| QM-50~QM-51 板块/指数聚合 | 未实现 | 仅有板块同步和列表查询，无聚合服务 |
| QM-60~QM-62 连续因子、参数固定、可追踪 | 已实现并核验 | 模型常量、`effective_config`、snapshot run hash 字段 |

### 3.2 PRD30 盘后任务

| PRD 条款 | 状态 | 关键证据 |
|---|---|---|
| AC-01 远程自动运行 | 已实现并核验 | `worker.py:scheduled_bars_refresh` CronTrigger 16:00 |
| AC-02 本地不自动调度 | 已实现并核验 | `main.py:lifespan` 不启动 Scheduler |
| AC-03 本地完整手动调试 | 已实现未运行核验 | `admin_after_close.py`、触发脚本存在 |
| AC-04 日线盘后计算 | 部分实现 | `checking_coverage` 仍强制检查 15m ready |
| AC-05~AC-07 参数、Readiness、Run 隔离 | 已实现并核验 | manifest、`BarsCoverageService`、run_key 唯一索引 |
| AC-08~AC-10 计算与发布分离、published_run_id、两阶段发布 | 已实现并核验 | `publish_run` + `finish_snapshot_run` |
| AC-11 幂等与补跑 | 已实现并核验 | create 去重、publish 幂等、断点恢复 |
| AC-12 跨 Worker 领取 | 已实现并核验 | `FOR UPDATE SKIP LOCKED` + `lease_epoch` |
| AC-13 完成状态 | 部分实现 | 无显式 pending/partial 字段，语义由 queued/partial_failed 表达 |
| AC-14 部分失败 | 已实现并核验 | `StrategyRun` 计数、publish 拒绝 partial_failed |
| AC-15 旧触发路径清理 | 已实现并核验 | `_maybe_trigger_after_close_orchestrator` 已删除 |

## 4. 偏差与风险索引

已写入 `docs/maps/00-system-overview.md`：

| 领域 | 偏差 | 等级 | 详情位置 |
|---|---|---|---|
| 量化模型 | QM-50/QM-51 板块与指数层聚合尚未实现 | P2 | `maps/20-quant-model.md` §7 |
| 盘后任务 | AC-04 与实现冲突：`checking_coverage` 仍强制检查 15m 覆盖率 | P1 | `maps/30-after-close.md` §7 |
| 盘后任务 | 本地调试若误连远程 Redis DB 0 可能消费正式队列/发布正式结果 | P0 | `maps/30-after-close.md` §7 |
| 量化模型 | SMC 核心未显式保留成交量信息，依赖结构面板成交参与组 | P1 | `maps/20-quant-model.md` §9 |
| 运行体系 | 自动部署代码已准备但链路未启用 | P2 | `maps/80-system-runtime.md` §10 |

## 5. 新增/更新文档

- `docs/prd/80-system-runtime.md`：新增 `SR-09 长期分支策略`。
- `docs/maps/80-system-runtime.md`：更新分支治理结果、远程服务器当前状态。
- `docs/maps/20-quant-model.md`：更新 Phase 4 审计日期、PRD 映射、偏差与风险。
- `docs/maps/30-after-close.md`：更新 Phase 4 审计日期、PRD 映射、偏差与风险。
- `docs/maps/00-system-overview.md`：新增与 PRD 的已知偏差（高风险索引）。
- `docs/runbooks/branch-governance.md`：新建分支治理 Runbook。
- 本文档：`docs/changes/2026/CHANGE-20260727-001-branch-governance.md`。

## 6. 验证与提交

- 已执行 Markdown 链接检查、冲突标记检查和 `git diff --check`。
- 已显式 `git add` 文档文件，创建本地审计提交。
- `dev` 可 fast-forward 推送到 `origin/dev`。
- `experiment` 已推送到 `origin/experiment`。
- 未推送 `main`，未创建 PR，未触发部署。

## 7. 后续工作

- 修复 AC-04 15m 覆盖率检查与 PRD 的局部冲突（需单独需求/行为变化流程）；
- 评估并实现 QM-50/QM-51 板块/指数层聚合；
- 启用自动部署链路（服务器侧脚本安装、GitHub Secrets、workflow 合并到 `main`）。
