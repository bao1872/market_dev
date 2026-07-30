# CHANGE-20260730-016：盘后链路 P0 永久收口 + 新模型合同冻结

状态：进行中（代码 + 目标纯单元测试 + Ruff 通过；PG 集成待 CI；本轮未部署、未 push main、未修改生产数据；canary 计算已完成静态核验但未改生产）
日期：2026-07-30
类型：behavior + contract + architecture + data
领域：盘后编排 / 复盘模块 / 行情质量 / 量化模型 / 部署运维

相关 PRD：

- `docs/prd/30-after-close.md`：AC-04 / AC-08 / AC-09 / AC-10 / AC-14（发布顺序、恢复、chip worker、聚合依赖）
- `docs/prd/70-review.md`：RV-01～RV-22（第二金字塔、bootstrap 状态、冷启动）
- `docs/prd/75-auction-analysis.md`：AA-01～AA-NN（新增，仅合同草案）

相关 Maps：

- `docs/maps/30-after-close.md`：§4 发布顺序 / §5 失败恢复 / §6 chip worker / §7 聚合依赖
- `docs/maps/70-review.md`：第二金字塔维度定义 / bootstrap 状态机
- `docs/maps/75-auction-analysis.md`：新增（仅设计草案，无实现）

相关 Rules：

- `rules/70-trae-cn.md`：禁止临时生产脚本代替永久修复；Compact 后只读 ledger；成功判定三要素
- `rules/80-deployment-data-safety.md`：禁止 docker cp 与未审计 stdin 脚本；部署版本合同（repo=image=env=runtime SHA）

相关 Runbooks：

- `docs/runbooks/after-close-recovery.md`：DSA / chip / stock_core pointer / 聚合 / Review 冷启动正式恢复路径

相关提交：

- 本轮新增 1 个聚焦提交（dev 分支，未 push main，未部署）

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮一次性收口盘后链路 7 项 P0 永久修复（stock_core 发布闭环、失败 DSA 恢复、chip 任务有执行者、聚合依赖闭环、MDQ verification、Review 冷启动、部署版本合同），并冻结新模型合同（第二金字塔维度定义、竞价锚点与生命周期），仅新增 PRD/Map/DTO 设计草案，不实现业务代码、不部署生产。

## 2. 背景与问题

### 2.1 触发事实

- 2026-07-30 c56d991 部署后，复盘 run 进入 `signals_ready` 但无法发布：`metric_engine` 要求 ≥60 个交易日历史，但系统刚上线无历史；
- 部署脚本仍以 `health=200` 作为唯一成功门禁，曾多次出现 repo SHA 与容器 env `GIT_SHA` 不一致（手工 `sed` 补版本）；
- MDQ verification 复用了 scan run 的 `run_key`，导致 verification run 无法创建；
- MDQ repair 因 `while processed < batch_size` 错误条件只处理 10 条后停止；
- chip_consensus job 已有执行逻辑但 worker 入口未领取；
- DSA 失败时存在裸 SQL 把 `failed` 改回 `queued` 的隐患；
- stock_core pointer 与 board 聚合无强依赖关系，曾出现"主 run succeeded 但 pointer/聚合缺失"。

### 2.2 根因

1. 发布闭环缺少"pointer 已写入 + data_run_id 匹配"的显式断言；
2. 失败恢复缺少正式 service，临时脚本补生产状态；
3. chip worker 入口未注册 `after_close_chip_consensus` 任务类型；
4. 聚合无 source_core_run_id 强绑定；
5. MDQ run_key 不区分 scan/verification；
6. metric_engine 无 granular readiness 报告，冷启动场景静默 force publish；
7. 部署脚本无版本合同门禁；
8. 新模型（第二金字塔、竞价分析）未冻结合同。

## 3. 变化前

- `after_close_orchestrator` publishing 阶段未显式调用 `publish_stock_core`，依赖下游隐式发布；
- 无 `dsa_recovery_service`，失败 DSA 通过裸 SQL 复位；
- `worker.py` 未注册 `after_close_chip_consensus` 任务类型；
- `market_data_quality_service.execute_repair` 因 `while processed < batch_size` 条件错误只处理 10 条；
- `metric_engine` 在历史不足时返回 `unavailable`，不报告 `insufficient_history` 原因；
- `panji-deploy.sh` 使用 `sed -i` 修改 market.env，仅以 `health=200` 判成功；
- 无 `docs/prd/75-auction-analysis.md`、`docs/maps/75-auction-analysis.md`、`docs/runbooks/after-close-recovery.md`。

## 4. 变化内容

### 4.1 P0-1：stock_core 发布闭环

`backend/app/services/after_close_orchestrator.py` publishing 阶段：

- DSA `publish_run` 成功、snapshot run `succeeded`、`coverage ≥ 0.98` 后，显式调用 `factor_publication_service.publish_stock_core`；
- 只有 pointer 已写入且 `data_run_id == snapshot_run_id` 才写 publishing 检查点和主 run `succeeded`；
- 重复调用同一 run 幂等（`on_conflict_do_update`）；
- 同日 pointer 指向其他 run 时记录事件 `stock_core_pointer_mismatch`，不静默覆盖；
- `metadata` 写 `stock_core_publication_id` / `data_run_id` / `coverage_ratio`。

### 4.2 P0-2：失败 DSA 恢复

新增 `backend/app/services/dsa_recovery_service.py`：

- `recover_failed_dsa_run(job_run_id)`：
  - `completed` / `published` → 复用，返回 `(run, False)`；
  - `running` 且 lease 未过期 → 拒绝恢复；
  - `failed` / `partial_failed` / `max_retries_exceeded` → 创建新 DSA run（`attempt_no` 递增），原子更新 orchestrator metadata 中 `dsa_run_id`，返回 `(new_run, True)`；
  - 恢复次数上限 `_MAX_DSA_RECOVERY_COUNT = 5`，超过抛 `DSARecoveryError`；
- 原 failed run 保留审计，禁止改回 `queued`；
- `scheduler_job_run_recovery_service` 断点恢复读取到 child DSA 为 failed 时调用本 service，禁止裸 SQL。

### 4.3 P0-3：chip 任务有执行者

`backend/app/worker.py`：

- 新增 `WORKER_TYPE=chip_consensus` 分支与 `_chip_consensus_poll_once` 独立 poll 函数；
- 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 `queued` / `resume_queued` 的 `after_close_chip_consensus` 任务；
- `lease_epoch` fencing 防止僵尸 worker 覆盖新 worker 状态；
- `heartbeat_at` 周期更新；
- 断点续算：`get_pending_chip_instruments` 过滤已 `succeeded` 的 instrument，`resume_queued` 任务只重试未成功项；
- chip 失败不反改 core（`execute_after_close_chip_consensus` 内部已隔离）；
- `scheduler_job_run_recovery_service.auto_resume_interrupted_after_close_runs` 同时扫描 `after_close_orchestrator` 和 `after_close_chip_consensus` 两类 `interrupted` 任务；
- 不新增常驻容器，复用现有 after-close worker。

### 4.4 P0-4：聚合依赖闭环

`backend/app/services/after_close_orchestrator.py`：

- stock_core pointer 发布成功后才触发 market aggregation；
- `run_market_factor_aggregation` 必须传入 `source_core_run_id`，校验与当前 stock_core pointer `data_run_id` 一致；
- 聚合失败只重跑聚合，**不影响已发布 stock_core**；
- 主 run 最终状态明确区分 `core_published` 与 `optional_failed`（聚合/板块/review）；
- 禁止"主 run succeeded 但 pointer/聚合缺失"：pointer 写入失败 → 主 run `failed`；聚合失败 → 主 run `partial_succeeded`（core 已发布）。

### 4.5 P0-5：MDQ verification

`backend/app/services/market_data_quality_service.py`：

- `run_key` / `parameter_hash` 新增 `mode=verification` 和 `source_repair_run_id` / `verification_seq` 维度，确保 verification run 与 scan run 完全独立；
- `execute_repair` 修复循环条件：`while True` 拉取候选为空时 break，确保处理全部 eligible items（batch_size=10 仅是吞吐批次，不是总数上限）；
- 新增 `resolve_last_completed_trading_day(now_cst=None)`：使用最近已完成交易日作为默认 `end_date`，避免未收盘/未来日期被误判为 `TAIL_GAP`；
- verification run 创建全新 `market_data_quality_runs` 行，items、结果、run_id 与 source scan 完全不同。

`backend/scripts/market_data_quality_cli.py`：

- `--end-date` 默认使用 `resolve_last_completed_trading_day`，未传时自动解析；
- verification 子命令显式传入 `mode=verification` 和 `source_repair_run_id`。

### 4.6 P0-6：Review 冷启动

`backend/app/domain/review/metric_engine.py`：

- 新增 `readiness` 字段，区分 `raw_ready` / `normalized_ready` 及原因；
- 历史不足时返回 `status=insufficient_history` 而非 `unavailable`；
- 每个 P/Q/U/C/V 分别报告 `raw_ready` / `normalized_ready` / `reason` / `history_observations` / `min_required`；
- `fp_segment_change_pct` 全空时 P metric `value=None`，禁止伪造值（已写合同测试）。

新增 `backend/app/services/review_bootstrap_service.py`：

- `bootstrap_history(days_back=120, dry_run=False)`：从已发布 `stock_core` 历史回填 scope snapshot；
- `bootstrap_single_date(trade_date, source_core_run_id, ...)`：单日期回填接口；
- 幂等 upsert（`on_conflict_do_update` on `uq_review_scope_snapshots_run_scope`）；
- `dry_run=True` 时只计算不写入（canary 用）；
- Bootstrap 专用版本 `BOOTSTRAP_ALGORITHM_VERSION="bootstrap-1.0.0"`，与正式 review 算法版本隔离；
- 不修改 `stock_core` 数据（只读）、不修改现有 review run（只创建 bootstrap run）、不绕过 publish gate。

`backend/app/services/review_publication_service.py`：

- `evaluate_publish_gate` 检查 market 范围 P/Q/U/C/V 的 `readiness` 字段；
- 报告具体缺失原因，如历史不足需运行 bootstrap；
- 禁止 force publish（`value=None` 时阻断）。

### 4.7 P0-7：部署版本合同

`scripts/deploy/panji-deploy.sh`：

- 新增 `update_env_file()` 函数：使用 temp file + `mv` 原子替换 `market.env` 中的 `GIT_SHA` / `BUILD_TIME`（禁止 `sed -i`）；
- 所有部署 scope（frontend / backend / image / all）构建前先调用 `update_env_file`，确保 build 和 up -d 使用同一 GIT_SHA；
- `health_check()` 新增 SHA gate：
  - 验证容器 env `GIT_SHA` 与目标 `short_sha` 一致；
  - 镜像部署时验证 image tag 包含 `short_sha`；
- 回滚逻辑使用 `update_env_file` 确保 env 正确恢复；
- 移除 goaccess 服务引用（已被 Umami 替代）。

### 4.8 新模型合同冻结（仅 PRD/Map 草案）

新增 `docs/prd/75-auction-analysis.md` 和 `docs/maps/75-auction-analysis.md`：

- **竞价锚点合同**：`anchor_type` / `source` / `direction` / `lower_price` / `upper_price` / `center_price` / `strength` / `freshness` / `validity` / `price_adjustment_version` / `source_core_run_id` / `source_chip_run_id`；
- 结构锚点保存高低点、BOS/CHoCH 触发线、OB、失效线；
- 筹码锚点保存上下共识区和主峰；
- **竞价生命周期**：`formed` / `confirmed` / `weakened` / `failed` / `expired`；
- 不做第三金字塔、不做综合分。

更新 `docs/prd/70-review.md` 和 `docs/maps/70-review.md`：

- 第二金字塔维度定义：状态分布、状态迁移、事件新鲜度、广度、集中度、相对强弱；
- 行业与概念分开聚合；
- 不做总分（第一金字塔保持趋势/结构/动量与波动/可选筹码）。

### 4.9 文档与规则

- `rules/70-trae-cn.md`：禁止临时生产脚本代替永久修复；发现闭环缺口必须代码 + 正式测试；Compact 后只读 ledger；成功判定必须有 pointer / 版本 / 真实数据证据；
- `rules/80-deployment-data-safety.md`：禁止 `docker cp` 和未审计 stdin 脚本修改生产；手工恢复必须走正式 service / CLI 并留审计；部署版本合同（原子更新 market.env + SHA gate）；
- `docs/prd/30-after-close.md` / `docs/maps/30-after-close.md`：发布顺序、恢复、chip worker、聚合依赖；
- `docs/runbooks/after-close-recovery.md`：DSA / chip / stock_core pointer / 聚合 / Review 冷启动正式恢复路径。

## 5. 变化后

- 盘后链路 7 项 P0 全部以代码 + 正式 service / 正式测试完成，禁止裸 SQL / `/tmp` Python / `docker cp` 临时补生产；
- 新模型合同冻结在 PRD/Map 草案，migration 和业务代码留到下一阶段；
- 部署脚本通过版本合同保证 repo=image=env=runtime SHA 一致；
- 复盘冷启动通过 bootstrap service 生成历史观察，不再要求等待 60 个交易日；
- 失败恢复路径全部记录在 `docs/runbooks/after-close-recovery.md`。

## 6. 影响范围

### 6.1 API 或契约

- `factor_publications` 中 `kind=stock_core` 的 pointer 写入由 orchestrator 显式调用，metadata 增加 `stock_core_publication_id` / `data_run_id` / `coverage_ratio`；
- `market_data_quality_runs.run_key` / `parameter_hash` 新增 `mode` 和 `source_repair_run_id` 维度；
- review scope snapshot 新增 `readiness` 字段（P/Q/U/C/V 分别报告 raw/normalized ready 及原因）。

### 6.2 数据

- 无 migration（本轮不实现竞价业务代码）；
- bootstrap service 写入 `market_review_scope_snapshots` 时 `metadata.bootstrap=true` 标记，与正式 review 隔离。

### 6.3 后端

- 新增 service：`dsa_recovery_service.py`、`review_bootstrap_service.py`；
- 修改：`after_close_orchestrator.py`、`worker.py`、`market_data_quality_service.py`、`market_data_quality_cli.py`、`metric_engine.py`、`review_publication_service.py`、`scheduler_job_run_recovery_service.py`。

### 6.4 Worker 与任务

- `after-close` worker 新增 `chip_consensus` 分支；
- watchdog 自动恢复扩展支持 `after_close_chip_consensus` interrupted 任务。

### 6.5 部署与运行

- `panji-deploy.sh` 新增 `update_env_file` 和 SHA gate；
- 部署成功门禁从 `health=200` 升级为 `repo HEAD=image tag=container env=runtime SHA` + `health=200`。

### 6.6 前端

- 无变化（本轮不实现竞价前端）。

## 7. 迁移与兼容

- 无 DB migration；
- `run_key` / `parameter_hash` 字段值变化：旧 verification run 已存在的会被新 key 视为不同 run（不冲突，旧 run 保留）；
- `metric_engine` 返回的 payload 新增 `readiness` 字段：旧消费者向后兼容（忽略未知字段）；
- bootstrap snapshot 与正式 review snapshot 通过 `metadata.bootstrap` 标记隔离，不互相干扰。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| Ruff | 修改的 backend 文件 | PASS | `ruff check` 通过 |
| 目标纯单元测试 | `test_dsa_recovery_service.py` / `test_chip_consensus_worker.py` / `test_review_cold_start_contract.py` / `test_market_data_quality_service.py` | PASS | pytest 输出 |
| FP 合同测试 | `fp_segment_change_pct` 全空时不伪造值 | PASS | `TestSegmentChangePctEmptyContract` |
| 历史不足状态 | 无历史时 status=insufficient_history | PASS | `TestInsufficientHistoryContract` |
| MDQ run 不复用 | verification run 与 scan run 独立 | PASS | `test_market_data_quality_service.py` |
| 部署脚本 SHA gate | dry-run 断言 | PASS（静态核验） | `update_env_file` + `health_check` 实现 |
| PG 集成测试 | CI 临时 Postgres | 未验证（待 CI） | 本轮未在 CI 运行 |
| 生产部署 | — | 未验证 | 本轮未部署（按指令禁止） |
| canary review run | — | 未验证 | 本轮未修改生产数据 |
| 浏览器 UI 真实链路 | — | 未验证 | 本轮未启动服务 |

不得用"代码看起来正确"代替运行证据。

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | `docs/prd/30-after-close.md`（发布顺序/恢复/chip worker/聚合依赖）、`docs/prd/70-review.md`（第二金字塔/bootstrap）、`docs/prd/75-auction-analysis.md`（新增） |
| Maps | `docs/maps/30-after-close.md`、`docs/maps/70-review.md`、`docs/maps/75-auction-analysis.md`（新增） |
| Runbooks | `docs/runbooks/after-close-recovery.md`（新增） |
| Rules | `rules/70-trae-cn.md`、`rules/80-deployment-data-safety.md` |
| CHANGE | 本文件 |
| INDEX | `docs/changes/INDEX.md` 新增本条 |

## 10. 回滚方案

- 代码：revert 本提交即可（无 migration，无破坏性数据变更）；
- bootstrap snapshot：可通过 `DELETE FROM market_review_scope_snapshots WHERE metadata->>'bootstrap' = 'true'` 清理（本轮未写入生产）；
- market.env：`update_env_file` 在每次部署都会原子覆盖，回滚部署时自动恢复到目标 SHA；
- 部署脚本：revert 本提交恢复 sed 实现（不推荐，仅紧急回退用）；
- run_key 字段：旧 verification run 保留，新 key 与旧 key 不冲突，无需清理。

## 11. 遗留问题与风险

- DSA / stock_core pointer / 市场聚合 / Review bootstrap 的 CLI 包装尚未在 `backend/scripts/` 下新增（runbook 中已说明禁止 `/tmp` Python 绕过，需先补 CLI 再执行生产恢复）；
- canary review run 未在生产环境实际运行（本轮未部署）；
- 浏览器 UI 真实链路验收待用户手工登录（受 AGENTS.md §8 阻塞）；
- PG 集成测试待 CI 临时容器运行；
- 竞价分析 PRD/Map 仅为合同草案，下一阶段需补 migration 和业务代码；
- 第二金字塔维度定义已冻结，但实现代码留到下一阶段。

## 12. 后续变化

- 下一阶段：竞价分析 migration + 业务代码 + 前端；
- 下一阶段：第二金字塔实现；
- 下一阶段：补 `backend/scripts/` 下正式 CLI 包装（dsa_recovery_cli / publish_stock_core_cli / market_factor_aggregation_cli / review_bootstrap_cli）；
- 待 CI：PG 集成测试；
- 待用户：浏览器 UI 真实链路验收；
- 待部署授权：生产部署 + canary review run + 全量 review run 发布。
