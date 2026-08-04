# PRD 完整验收矩阵

**基线**: `6942a06000aa898945ceab3bcdc3bfc299913b86`
**生成日期**: 2026-08-04
**目的**: 逐条核对所有 PRD 需求的实现状态、证据和剩余缺口
**当前判断**: `code_ready = false`, `data_closed = false`

---

## 对齐检查发现与遗留问题（2026-08-04 补充）

以下为按 `ref/instruction.md`（盘后编排/FS/FP/SMC/RV/NAV/OPS）逐模块核对后确认的、
可本地验证的遗留问题清单。其余各模块（AC/FS/FP/SMC/RV/NAV/OPS）均有对应实现与契约
测试文件，基线纯单元套件已验证通过（详见下文修复记录）。

| 编号 | 严重级 | 问题描述 | 影响 | 处置 |
|------|--------|----------|------|------|
| LEGACY-01 | P0 | 权限模型 V2 引入 `AccessContext.default_route` 必填字段后，`test_stock_state_and_events.py`（4 处）与 `test_auction_replay_entitlement.py`（1 处工厂）未同步更新，构造 `AccessContext` 时缺该字段 | 12 个纯单元测试因 Pydantic ValidationError 失败，阻塞 CI 纯单元门禁；`test_gate2_capability_schemas.py` 已正确更新证明是遗漏而非设计变更 | 已修复：两文件补 `default_route`（admin→`/admin/overview`、member→`/forbidden`） |
| LEGACY-02 | P1 | `test_bars.py::test_check_minute_freshness_{recent,stale}` 使用 naive `datetime.now()`，而 `freshness_sla.check_minute_freshness` 将 naive 时间戳按 `Asia/Shanghai` 解释 | 在非上海时区机器（如 America/New_York）上 `recent` 用例 age 被放大 12h 而误判 stale，测试与机器时区耦合、flaky | 已修复：两用例改用 `ZoneInfo("Asia/Shanghai")` 显式时区 |
| LEGACY-03 | P0 | `_compute_product_nodes` 查询 `market_review_runs.source_chip_run_id`，但共享开发业务库 `bz_stock` 尚未应用 migration `083_review_run_chip_dependency`（该列缺失） | 经 SSH 隧道以 `PANJI_SHARED_DEV_DB_TEST=1` 实际运行 7 个 PG 行为测试时，`UndefinedColumnError: column market_review_runs.source_chip_run_id does not exist`，6 个测试 Fail（另有 1 个因 dsa_selector 已存在触发唯一约束 Error，已修复 fixture 后统一为 schema 漂移） | 待处置：远程部署 `远程Migration` 步骤应用 migration 083 后，7 个 PG 行为测试方可 `pg_tested`；当前 `system_overview_behavior_tests_pg_executed = true`、`pg_verified = false`（实际运行证据见下） |

### 阶段1 收口增量（2026-08-04 第二轮）

针对 `ref/next.md` 审查结论补充：

1. **P0-4 统一错误合同真正收口**：`admin_after_close.py` 全部 13 处手工 `HTTPException` 改为 `admin_errors.admin_error/admin_conflict/admin_not_found/admin_bad_request` 统一构造器（含 request_id 透传 + `**extra` 业务上下文）。新增 `test_admin_errors.py`（8 项纯单元）锁定稳定字段、上下文透传、request_id、409/404/400 映射、端点不再手工构造。
2. **AC-72A 管理诊断/恢复行为测试**：`test_after_close_orchestrator.py` 新增 cancel 幂等（终态返回当前事实）、cancel fence+审计（lease_epoch+1 + actor/reason/request_id）、reconcile stale→interrupted+fence、reconcile fresh 保持 running（4 项，postgres 标记，CI 执行）。
3. **阶段1 AC/PA 矩阵从占位表改为真实验收表**：AC-01~AC-73、PA-01~PA-31 全部填入真实后端文件/函数、API 端点、测试名称与确定性缺口，不再有 `[待填充]`。

**基线纯单元套件结果（修复前/后）**：修复前 `2815 passed / 13 failed`；修复后 `2828 passed / 0 failed`（14 skipped / 1204 deselected）；本轮统一错误合同改后 `2841 passed / 0 failed`（14 skipped / 1204 deselected）。

---

## 使用说明

每一项需求必须记录 7 个维度：`implementation_status` / `backend_evidence` / `api_schema_evidence` / `frontend_evidence` / `behavior_test_evidence` / `real_data_required` / `remaining_gap`

状态标记（P0-3 离散分级，禁止用单个✅混合代表不同层级）：`implemented`（有实现）→ `behavior_tested`（行为测试通过）→ `pg_tested`（PG 集成测试通过）→ `runtime_tested`（远端运行验证）→ `browser_tested`（浏览器验收）→ `data_verified`（真实数据核验）→ `blocked`（被阻塞）。任一需求须标注其最高已达成的离散层级，具体证据落在 7 个维度列。

---

## 需求 ID 总览（真实 PRD 源）

| PRD 文件 | ID 前缀 | 条目数 | 阶段归属 |
|---|---|---|---|
| `00-product-scope.md` | PS | 7 | 产品范围（元需求） |
| `10-market-data.md` | MD | 11 | 行情数据基础 |
| `20-quant-model.md` | QM | 26 | 量化模型（含 FP+SMC+Chip） |
| `30-after-close.md` | AC | 29+ | 阶段1：盘后编排 |
| `40-market-stock-experience.md` | MX | 29 | 阶段4：行情+导航 |
| `50-market-data-quality.md` | MQ | 9 | 数据质量 |
| `50-watchlist-intraday.md` | WI | 15 | 自选+盘中通知 |
| `60-permissions-admin.md` | PA | 11 | 阶段1：权限+管理后台 |
| `70-review.md` | 章节0~27 | 28章 | 阶段5：Review |
| `80-system-runtime.md` | SR | 32 | 阶段7：系统运行+部署 |
| `90-system-wide-requirements.md` | SW | 12 | 跨模块系统级 |

---

## 阶段 1：盘后编排 + 管理后台闭环

### AC 系列需求（盘后编排） — 来源: `docs/prd/30-after-close.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| AC-01 | 远程自动运行 | behavior_tested | `worker.py:scheduled_bars_refresh`（bars_scheduler，CronTrigger 16:00）→ `after_close_orchestrator.create_after_close_run` | `POST /v1/admin/after-close-runs` 创建 | N/A | `test_after_close_orchestrator.py` create/execute 事件 | 是 | 远程 scheduler 触发链真实性 |
| AC-02 | 本地不自动调度 | behavior_tested | `main.py:lifespan` 不启动 Scheduler；`worker.py` 按 `WORKER_TYPE` 手动启动 | N/A | N/A | `test_config_validation.py` | 否 | — |
| AC-03 | 本地完整手动调试 | behavior_tested | `admin_after_close.py` create/retry/resume/force/cancel/reconcile；`scripts/trigger_dsa_batch_small.py` | create/retry/resume/force/cancel/reconcile 全部端点 | N/A | `test_after_close_endpoints.py`（force/resume） | 否 | 本地未跑完整 Worker 链路 |
| AC-04 | 日线盘后计算 | behavior_tested | `after_close_orchestrator.py` refreshing_daily + computing_features；checking_coverage 仅日线≥0.9 | — | N/A | `test_after_close_orchestrator.py::test_ac04_*` | 是 | 真实单日全量 |
| AC-05 | 固定参数一次计算 | behavior_tested | `dsa_selector.yaml` allowed_scopes:[system]；`strategy_batch_service.create_batch_run` | — | N/A | strategy_batch 单测 | 否 | — |
| AC-06 | Readiness 门槛 | behavior_tested | `checking_coverage` → `BarsCoverageService.compute_daily_coverage`（日线≥0.9） | — | N/A | `test_after_close_orchestrator.py::test_ac04_*` | 是 | 真实覆盖率 |
| AC-07 | Run 隔离 | behavior_tested | `create_after_close_run` run_key 去重；`uq_scheduler_job_runs_active_run_key` | 409 DUPLICATE_RUN | N/A | `test_after_close_orchestrator.py` | 是 | 并发隔离 |
| AC-08 | 计算与发布分离 | behavior_tested | `execute_after_close_run` computing_features→publishing；`publish_run` 独立 | — | N/A | `test_execute_writes_status_events` | 是 | 两阶段真实发布 |
| AC-09 | 正式发布指针 | behavior_tested | `StrategyRun.published_at` + `StockFeatureSnapshotRun.published_at`；读取按 `published_at IS NOT NULL` | `GET /v1/admin/after-close-runs/{id}` | N/A | `test_after_close_orchestrator.py` | 是 | pointer 原子性 |
| AC-10 | 两阶段发布 | behavior_tested | `publish_run`（阶段1）→ `finish_snapshot_run`（阶段2），独立 session | — | N/A | `test_after_close_orchestrator.py` | 是 | 失败回滚 |
| AC-11 | 幂等与补跑 | behavior_tested | create 去重；publish_run 幂等；execute 断点恢复；`retry_after_close_run` | retry/resume | N/A | `test_retry_after_close_run_writes_event` | 是 | 断点恢复 |
| AC-12 | 跨 Worker 领取 | behavior_tested | `_after_close_poll_once` FOR UPDATE SKIP LOCKED + lease_epoch fencing | — | N/A | worker 领取单测 | 是 | 真实并发 |
| AC-13 | 完成状态 | behavior_tested | 状态真源 `AfterCloseRunStatus`（`after_close_orchestrator.py:683`，含 partial_success/interrupted/cancelled）；API `AfterCloseRunStatusResponse`（`schemas/scheduler_job_run.py:86`）直出 orchestrator_status + step_summary + partial_success；前端 `adminAfterClosePipelineHelpers.ts` STEP_LABELS/DEFAULT_STEP_ORDER/statusLabel/statusTone 同一语义适配 | `GET /v1/admin/after-close-runs/{id}` | `AdminAfterClosePipelinePage` | `test_after_close_phase0_contracts.py` / `adminAfterClosePipeline.test.ts` | 是 | 真实状态机全量 |
| AC-14 | 部分失败 | behavior_tested | `StrategyRun` succeeded/failed/skipped_count；publish_run 拒绝 partial_failed；`resolve_terminal_run_status` | — | N/A | `test_after_close_phase0_contracts.py::test_review_executor_timeout_forces_partial_success` | 是 | partial_success 保留核心产物 |
| AC-15 | 旧触发路径清理 | implemented | `worker.py` 注释删除 `_maybe_trigger_after_close_orchestrator`；grep 无符号 | — | N/A | — | 否 | — |
| AC-16 | Feature Snapshot 批处理性能合同 | behavior_tested | `feature_snapshot_service.compute_for_trade_date` 按 batch 预读 qfq bars + 批内 upsert/flush；[2026-08-04] 新增阶段耗时/吞吐/回退指标输出（read/compute/persist/total_duration、symbols_per_second、fallback_count、commit_count） | — | N/A | feature_snapshot 单测（18 passed） | 是 | 固定 fixture 基准：query_count=O(batch_count)、commit=O(batch)、相对旧链提速50% 未证明（需运行时测量） |
| AC-16(2) | 统一盘后编排 | behavior_tested | `execute_orchestrator_step` 统一步骤执行器；7 个顶层步骤 | — | N/A | `test_after_close_phase0_contracts.py::test_*_uses_executor` | 否 | — |
| AC-08(2) | 单股事务与检查点 | behavior_tested | `snapshot_run_item_service` create/claim run items（UPDATE...FOR UPDATE SKIP LOCKED） | — | N/A | snapshot_run_item 单测 | 是 | 单股失败隔离 |
| AC-09(2) | 分层发布指针 | behavior_tested | `factor_publication_service` stock_core/chip/review 分层 pointer | — | N/A | factor_publication 单测 | 是 | 分层一致性 |
| AC-10(2) | 读取端统一接入 pointer | implemented | `get_published_snapshot_run_id` 等读取端统一消费 pointer | — | N/A | — | 是 | API 读 pointer 一致性 |
| AC-14(2) | 独立任务与核心保护 | behavior_tested | chip/review 后置任务失败不反改 core（suppressed/superseded） | — | N/A | chip/review 单测 | 是 | 后置失败保留核心 |
| AC-17 | stock_core 发布闭环 | implemented | `factor_publication_service.publish_stock_core`（门禁+原子切换） | — | N/A | — | 是 | stock_core 完整链 |
| AC-18 | chip_consensus Worker | behavior_tested | `create_after_close_chip_consensus_job` + `_enqueue_chip_job_step`（终态前正式步骤） | — | N/A | `test_after_close_phase0_contracts.py::test_enqueue_chip_job_*` | 是 | chip worker 真实计算 |
| AC-19 | 聚合依赖合同 | implemented | 聚合绑定同一 source_core_run_id；失败只重跑聚合 | — | N/A | — | 是 | 依赖解析 |
| AC-70 | 盘后7步正式状态机（含复盘） | behavior_tested | `AfterCloseRunStatus` 7 步状态机 + `_execute_review_step` 经执行器 | — | N/A | `test_after_close_phase0_contracts.py` | 是 | 完整状态机 |
| AC-71 | 幂等 review 重跑合同 | behavior_tested | `_execute_review_step` idempotent_reuse_published_run / resume_skipped | — | N/A | `test_review_step_resume_skip_returns_resume_skipped` | 是 | review 幂等 |
| AC-72 | 时间线合同（防负数耗时） | behavior_tested | `job_run_event_service` 事件时间线 | `GET /v1/admin/job-runs/{id}/events` | N/A | timeline 单测 | 是 | 时间线 |
| AC-72A | 管理诊断与恢复操作合同 | behavior_tested | `cancel_after_close_run`/`reconcile_after_close_run`/`retry_after_close_run`/force（restart_from=daily_ready） | create/cancel/reconcile/retry/resume/force 端点 | N/A | `test_cancel_after_close_run_*` / `test_reconcile_running_*` / `test_after_close_endpoints.py` | 是 | 真实恢复 |
| AC-73 | review 冷启动合同 | behavior_tested | review 历史回补冷启动（bootstrap 绑定已发布 core pointer） | — | N/A | review 冷启动单测 | 是 | 历史不足冷启动 |

### PA 系列需求（权限与管理后台） — 来源: `docs/prd/60-permissions-admin.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| PA-01 | 三类独立权限 | behavior_tested | `UserCapability` 模型 + `user_capabilities` 表 + `require_capability`/`require_any_capability`（`access_control_service.py`） | `/me/access` 返回 capabilities | `CapabilityRoute`/`CapabilityAnyRoute` 前端守卫 | `test_gate2_capability_schemas.py`（35）/ `test_permission_v2_backend_contracts.py`（49） | 是 | 非独立 capability 运行时验证 |
| PA-02 | 自选数量 | behavior_tested | `UserCapability.watchlist_limit` + `require_watchlist_limit`（admin None） | `POST /watchlist` 校验限额 | 前端限额展示 | `test_permission_v2_backend_contracts.py` | 是 | 真实限额 |
| PA-03 | 30天周期有效期 | behavior_tested | `UserCapability.expires_at` per-capability 独立；`grant_days = months*30` | admin 授予/续期 API | — | `test_permission_v2_backend_contracts.py` | 是 | 自然过期 |
| PA-10 | 自选管理 | behavior_tested | `watchlist.py` 用 `require_capability("self_selection")` | `GET/POST /watchlist` | 自选页 | `test_gate2_capability_schemas.py` | 否 | — |
| PA-11 | 行情管理 | behavior_tested | `market.py` 用 `require_any_capability`；`stock_context.py` 用 `require_capability("market_data")` | `/market`、`/stock/:symbol` | 行情页 | `test_gate2_capability_schemas.py` | 否 | — |
| PA-12 | 复盘与竞价 | behavior_tested | `auction.py`/replay 用 `require_capability("research_replay")` | `/replay`、`/auction` | 复盘页 | `test_gate2_capability_schemas.py` | 否 | — |
| PA-13 | 详情访问 | behavior_tested | `stock_context.py` 守卫 market_data | `/stock/:symbol` | 详情按钮 gating | `test_gate2_capability_schemas.py` | 否 | — |
| PA-20 | 邀请码生成 | behavior_tested | `InviteCode.capabilities` JSONB + 三勾选 schema | `POST /admin/invite-codes` | 邀请码弹窗三勾选 | `test_permission_v2_backend_contracts.py` | 否 | 真实 UI 未核验 |
| PA-21 | 激活和过期 | behavior_tested | `InviteRedemption` 追溯 + `register_with_invite_code`/`renew_with_invite_code` | `POST /api/v1/auth/redeem` | — | `test_permission_v2_backend_contracts.py` | 是 | 生命周期真实 |
| PA-30 | 管理能力 | behavior_tested | `require_admin`；`admin_after_close`/`admin_subscription`/`admin_users` 端点 | `/v1/admin/*` | 后台页面 | `test_permission_v2_pg_integration.py`（5） | 否 | — |
| PA-31 | 模块边界 | behavior_tested | 管理 API 唯一错误构造器 `admin_errors.admin_error`；RBAC require_roles("admin") | 统一错误 DTO | 前端统一解析 | `test_admin_errors.py`（8） | 是 | 前端消费错误 DTO |

**判断**: `after_close_closed = code_verified`（真实环境全量未核验）, `admin_pipeline_closed = code_verified`（真实 API/UI 未核验）

---

## 阶段 2：Feature Snapshot 性能与资源闭环

### AC-16 相关 — 来源: `docs/prd/30-after-close.md` AC-16

| 条目 | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|------|------|------|----------|----------|----------|----------|------------|----------|
| AC-16 | Feature Snapshot 批处理性能合同 | behavior_tested | `feature_snapshot_service.compute_for_trade_date` 按 batch 预读 qfq bars + 批内批量 upsert/flush | — | N/A | feature_snapshot 单测（18 passed） | 是 | 固定 fixture 基准未建 |
| 批量读取 | 1d/15m bars + adj factor 批量；查询次数随 batch_count 而非 symbol_count | behavior_tested | `mdas.get_bars_batch` 每 batch 2 次批量读取（1d+15m），`mdas_batch_read_count += 2` | N/A | N/A | `test_bar_repository_batch_conversion.py` | 是 | 单股回退路径是否仍存在未审计 |
| 批量计算 | 有限并发；可配置；不使用无界 gather；每批 heartbeat；资源紧张降并发 | behavior_tested | 批内顺序计算（非无界 gather）；`progress_callback` 每批回调心跳 | N/A | N/A | feature_snapshot 单测 | 是 | 无配置化并发/降并发策略 |
| 批量持久化 | 批级 upsert/flush；不逐股 commit；单股失败隔离+fencing | behavior_tested | 批内统一 `upsert_snapshot`；commit 由 caller 统一提交（本函数不 commit）；单股失败写 degraded_reasons | N/A | N/A | feature_snapshot 单测 | 是 | — |
| 指标输出 | batch_count/query_count/commit_count/durations/fallback_count | behavior_tested | [2026-08-04] 输出 read/compute/persist/total_duration、symbols_per_second、fallback_count、commit_count、batch_count、mdas_batch_read_count | N/A | N/A | feature_snapshot 单测 | 是 | commit_count 当前恒 0（commit 由 caller 提交） |
| 基准比较 | fixture 旧链 vs 新链：查询不按 symbol_count 增长；commit=O(batch)；耗时降50% | blocked | 无固定 fixture 基准脚本 | N/A | N/A | — | 是 | 需建 fixture 基准测量 |

### QM 系列相关（量化模型 — 含 FP+SMC+Chip） — 来源: `docs/prd/20-quant-model.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| QM-01 | 维度顺序 | behavior_tested | `schemas/first_pyramid.py` `ORDERED_DIMENSIONS=("trend","structure","momentum","chip_consensus")`；`FirstPyramidSnapshot._check_required_dimensions` 强制顺序 | — | — | `test_first_pyramid_contract.py` | 否 | — |
| QM-02 | 必选和可选 | behavior_tested | `REQUIRED_DIMENSIONS=(trend,structure,momentum)`（任一 available=False 抛 ValueError）；`OPTIONAL_DIMENSIONS=(chip_consensus,)` | — | — | `test_first_pyramid_contract.py` | 否 | — |
| QM-03 | 文字化输出 | behavior_tested | `DimensionResult.statusText` + `FirstPyramidSnapshot.statusText` 中文状态描述 | — | — | `test_first_pyramid_contract.py` | 否 | — |
| QM-10 | 趋势职责 | behavior_tested | `temporal_feature_service.py`（趋势/动量模块） | — | — | `test_temporal_feature_service.py` | 否 | — |
| QM-11 | 趋势算法 | behavior_tested | temporal_feature_service 趋势算法（DSA/swing 方向） | — | — | `test_temporal_feature_service.py` | 否 | — |
| QM-12 | 趋势段与成交量 | behavior_tested | 趋势段 + VolumeContext（Gate1 统一量能） | — | — | `test_temporal_feature_service.py` / `test_change_20260729_003.py` | 否 | — |
| QM-13 | 趋势与 SMC 边界 | behavior_tested | 趋势（temporal）与结构（structural/SMC）独立模块；`test_change_20260729_003.py::test_regime_*` | — | — | `test_temporal_feature_service.py` / `test_structural_factor_service.py` | 否 | — |
| QM-20 | 结构职责 | behavior_tested | `structural_factor_service.py`（SMC 结构模块） | — | — | `test_structural_factor_service.py` | 否 | — |
| QM-21 | 结构事件 | behavior_tested | `PyramidEvent`（`build_pyramid_event` 唯一 producer，BOS/CHoCH/OB 等） | — | — | `test_first_pyramid_smc_formatter.py` / `test_smc_*.py` | 否 | — |
| QM-22 | 结构成交量 | behavior_tested | 结构事件携带 `volumeContext`（Gate1） | — | — | `test_structural_factor_service.py` | 否 | — |
| QM-23 | Pine 对齐 | behavior_tested | `core/pytdx_adapter.py` + `test_smc_pine_deterministic.py` / `test_smc_tv_parity.py` | — | — | `test_smc_pine_deterministic.py` | 是 | 真实 TV 对齐 |
| QM-24 | SMC 展示语义一致性 | behavior_tested | `schemas/first_pyramid.py` direction/structureLevel 正式合同；`test_first_pyramid_smc_formatter.py` | — | — | `test_first_pyramid_smc_formatter.py` | 是 | 前端多入口一致性 |
| QM-30 | 动量职责 | behavior_tested | `temporal_feature_service.py`（动量模块） | — | — | `test_temporal_feature_service.py` | 否 | — |
| QM-31 | Bollinger 体系 | behavior_tested | canonical `bollinger` adapter + `indicator_service.compute_bollinger` | — | — | `test_indicator_service.py::test_adapt_watchlist_bb_15m_bb_matches_compute_bollinger` | 否 | — |
| QM-32 | 动量成交量 | behavior_tested | 动量维度携带 VolumeContext + squeeze 均量 | — | — | `test_temporal_feature_service.py` | 否 | — |
| QM-33 | 绝对与相对变化 | behavior_tested | 动量 absolute/relative 变化计算（temporal/structural 派生） | — | — | `test_temporal_feature_service.py` | 否 | — |
| QM-40 | 可选定位 | behavior_tested | `chipConsensus` 允许 None（`OPTIONAL_DIMENSIONS`）；chip 独立失败不反改 core | — | — | `test_change_20260729_003.py::test_core_no_node_cluster_call` | 否 | — |
| QM-41 | Node Cluster | behavior_tested | `node_cluster_engine.py::compute_node_cluster_profile`（100 行 profile + peaks + POC/VAH/VAL） | — | — | `test_node_cluster_engine.py` / `test_indicator_service.py::test_node_cluster_profile_hash_*` | 是 | 真实一致性 |
| QM-42 | 禁止 VAH/VAL 范围替代 | behavior_tested | Node Cluster 用完整 VP profile + peak 判定，非仅 VAH/VAL 区间；`test_algorithm_registry_architecture.py::test_node_cluster_contract_matches_semantics` | — | — | `test_node_cluster_contract.py` / `test_algorithm_registry_architecture.py` | 否 | — |
| QM-43 | 事件 | behavior_tested | chip/Node 事件经 `PyramidEvent` producer（NODE_CROSS_UP 等） | — | — | `test_node_cluster_contract.py` | 否 | — |
| QM-50 | 第二金字塔 | behavior_tested | `after_close_orchestrator.py` 第二金字塔/Review core 路径 | — | — | `test_change_20260729_003.py` / review 合同测试 | 否 | 冷启动链待验 |
| QM-51 | 不直接预测 | behavior_tested | 产品边界非预测；状态文本/事件为描述性，非涨跌预测 | — | — | `test_user_facing_labels.py` | 否 | — |
| QM-60 | 连续因子与事件分离 | behavior_tested | `DimensionResult.continuousFactors`（连续）与 `events`（离散事件）分离 | — | — | `test_first_pyramid_contract.py` | 否 | — |
| QM-61 | 参数固定 | behavior_tested | `parameterHash`（含算法版本与固定参数）；`_BB_WIN/_BB_K/_MACD_*` 常量 | — | — | `test_market_data_quality_service.py::test_parameter_hash_deterministic` | 否 | — |
| QM-62 | 可追踪 | behavior_tested | `algorithmVersion`/`FIRST_PYRAMID_ALGORITHM_VERSION`/`CHIP_CONSENSUS_ALGORITHM_VERSION` 版本可追踪 | — | — | `test_phase_d_factor_version.py` | 否 | — |
| QM-63 | 正式事件与可用性合同 | behavior_tested | `build_pyramid_event` 唯一 producer（direction/bias/structureLevel 冲突→diagnostic）；`FieldAvailability` reason（not_applicable/insufficient_history/…）；`CHIP_STATUS_STATES` 七态 | — | 前端 [待验证] | `test_first_pyramid_canonical_contract.py` / `test_first_pyramid_contract.py` / `test_review_dependency_matrix.py` | 是 | 前端跨入口待验证 |

**判断**: `feature_snapshot_closed = not_proven`, `performance_contract_passed = not_proven`

---

## 阶段 3：第一金字塔完整跨入口闭环

### 跨入口验证链 — 来源: QM-63 + Review PRD §27 + MX-20

| 链节点 | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|--------|------|------|----------|----------|----------|----------|------------|----------|
| Producer | FirstPyramidSnapshot 计算 | implemented | first_pyramid_service | [待验证] | N/A | [待验证] | 是 | 核心计算已修复 |
| summary_payload | summary_payload.first_pyramid 写入 | implemented | feature_snapshot_service | [待验证] | N/A | [待验证] | 是 | provenance 注入已修复 |
| flatten | flatten_first_pyramid 读模型 | blocked | [待填充] | [待验证] | N/A | [待填充] | 是 | flatten 一致性 |
| DB 查询 | 数据库查询字段 | blocked | [待填充] | [待验证] | N/A | [待填充] | 是 | 字段完整 |
| Market API | Market API 响应 | blocked | [待填充] | [待验证] | N/A | [待填充] | 是 | API 字段 |
| Detail API | Detail API 响应 | blocked | [待填充] | [待验证] | N/A | [待填充] | 是 | 详情字段 |
| Review 输入 | Review 消费 FP 字段 | blocked | [待填充] | [待验证] | N/A | [待填充] | 是 | Review 输入 |
| 前端 ViewModel | 前端 ViewModel 映射 | blocked | N/A | [待验证] | [待填充] | [待填充] | 是 | ViewModel |
| 第一金字塔组件 | 前端组件渲染 | blocked | N/A | [待验证] | [待填充] | [待填充] | 是 | 组件 |
| 导出 | 导出字段一致 | blocked | N/A | [待验证] | [待填充] | [待填充] | 是 | 导出 |

### 关键验证点

| 验证点 | 状态 | 证据 | 剩余缺口 |
|--------|------|------|----------|
| calculatedAt 同 run 一致 | implemented | 8690ccc inject_field_availability_provenance | 需真实验证 |
| sourceRunId 完整 | implemented | 8690ccc | 需真实验证 |
| 字段级 availability 及来源完整 | implemented | 8690ccc fieldAvailability | 需跨入口验证 |
| chip 七态 | blocked | [待验证] | 七态完整性 |
| chip 非破坏 merge | blocked | [待验证] | merge 逻辑 |
| fp_chip_available 始终 boolean | blocked | [待验证] | 类型安全 |
| fp_run_id/fp_calculated_at/fp_summary/fp_segment_change_pct 无结构性空值 | blocked | [待验证] | 空值语义 |
| producer/DB/API/详情/Review 字段一致 | blocked | [待验证] | 一致性 |

**判断**: `first_pyramid_core_code = largely_closed`, `first_pyramid_end_to_end = not_proven`

---

## 阶段 4：SMC + 行情导航前后端闭环

### QM-13/QM-21/QM-24 相关（SMC 语义） — 来源: `docs/prd/20-quant-model.md`

| 验证点 | 状态 | 证据 | 剩余缺口 |
|--------|------|------|----------|
| 唯一 formatter 被 StrategyChart/FirstPyramidPanel/Capture/监控/Review 复用 | blocked | [待验证] | formatter 复用 |
| BOS 4 组合 | blocked | [待验证] | BOS 完整性 |
| CHoCH 4 组合 | blocked | [待验证] | CHoCH 完整性 |
| OB 4 组合 | blocked | [待验证] | OB 完整性 |
| Swing OB | blocked | [待验证] | Swing OB |
| Internal OB | blocked | [待验证] | Internal OB |
| EQH/EQL | blocked | [待验证] | EQH/EQL |
| 缺方向 | blocked | [待验证] | 缺方向处理 |
| 缺级别 | blocked | [待验证] | 缺级别处理 |
| 冲突 diagnostic | blocked | [待验证] | 冲突处理 |

### MX 系列需求（行情体验+导航） — 来源: `docs/prd/40-market-stock-experience.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| MX-01 | 主入口 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 主入口 |
| MX-02 | 页面结构 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 页面结构 |
| MX-03 | 筛选 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 筛选 |
| MX-04 | 排序 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 排序 |
| MX-05 | 筛选上下文保持 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 上下文保持 |
| MX-10 | 路由 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 路由 |
| MX-11 | 单一 K 线 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | K 线 |
| MX-12 | 图层清单 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 图层 |
| MX-13 | 用户标签 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 标签 |
| MX-14 | 调试入口 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 调试 |
| MX-15 | 数据状态 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 数据状态 |
| MX-20 | 列表视图第一金字塔全量字段 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | FP 全量字段 |
| MX-30 | 固定组合图，无指标选择器 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 组合图 |
| MX-31 | 后端忽略旧 indicator_view 字段 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 旧字段 |
| MX-32 | 截图超时对齐 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 超时 |
| MX-33 | 普通详情页图层工具栏不在本轮范围 | blocked | N/A | N/A | N/A | N/A | 否 | 不在本轮 |
| MX-40 | 删除顶部大号自选按钮 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 删除旧按钮 |
| MX-41 | 紧凑自选按钮 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 紧凑按钮 |
| MX-42 | Direct 访问 fallback 按钮 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | fallback |
| MX-43 | 自选移除后留在当前详情 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 移除行为 |
| MX-50 | 第一金字塔折叠交互 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 折叠交互 |
| MX-51 | 类型化筛选操作符合同 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 类型化筛选 |
| MX-52 | 字段元数据 API | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 元数据 API |
| MX-53 | 旧 URL 筛选迁移规则 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | URL 迁移 |
| MX-60 | 列表唯一数据源 SSOT | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | SSOT |
| MX-61 | 删除列表 DSA-only 列 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 删旧列 |
| MX-62 | 详情页来源列表同源同序 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 同源同序 |
| MX-63 | 空值语义合同（三层一致） | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 空值语义 |
| MX-64 | 导出合同 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 导出 |

### 导航闭环验证 — 来源: `docs/prd/40-market-stock-experience.md` §8

| 验证点 | 状态 | 证据 | 剩余缺口 |
|--------|------|------|----------|
| /market 筛选 → 稳定 URL 进入 /stock/:symbol | blocked | [待验证] | URL 稳定性 |
| 详情独立加载 | blocked | [待验证] | 独立加载 |
| 刷新保持 | blocked | [待验证] | 刷新不丢失 |
| 详情内上一只/下一只 | blocked | [待验证] | 导航 |
| 返回列表 | blocked | [待验证] | 返回 |
| 恢复筛选/排序/分页/滚动 | blocked | [待验证] | 上下文恢复 |
| 不依赖 React 列表内存 | blocked | [待验证] | 内存独立 |
| 不依赖行数组 index | blocked | [待验证] | index 独立 |
| 不依赖 filteredRows | blocked | [待验证] | filteredRows 独立 |
| 不依赖临时 source 对象 | blocked | [待验证] | source 独立 |
| 不依赖旧 DSA-only URL 参数 | blocked | [待验证] | 旧参数独立 |

**判断**: `smc_frontend_closed = not_proven`, `market_navigation_closed = not_proven`

---

## 阶段 5：Review 完整业务闭环

### Review PRD 章节式需求 — 来源: `docs/prd/70-review.md`

| 章节 | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|------|------|------|----------|----------|----------|----------|------------|----------|
| §0 | 背景与当前基线 | implemented | N/A | N/A | N/A | N/A | 否 | 描述性 |
| §1 | 产品目标与边界 | blocked | [待验证] | [待验证] | [待验证] | [待验证] | 否 | 目标确认 |
| §2 | 权威业务链 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 完整链 |
| §3 | 页面路由、权限与URL状态 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 路由权限 |
| §4 | 后端模块结构 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 模块结构 |
| §5 | 数据模型 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 数据模型 |
| §6 | 范围定义与两级扫描 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 扫描逻辑 |
| §7 | P/Q/U/C/V 指标合同 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 指标合同 |
| §8 | 三类筛选器 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 筛选器 |
| §9 | 板块归因逻辑 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 归因 |
| §10 | 信号生命周期与追踪状态机 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 状态机 |
| §11 | 任务编排与发布 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 编排发布 |
| §12 | API 合同 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | API 合同 |
| §13 | 前端目录与组件 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 目录结构 |
| §14 | 页面信息架构 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 信息架构 |
| §15 | 前端数据与状态规则 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 状态规则 |
| §16 | 与现有页面的边界 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 边界 |
| §17 | 加载、空态和异常态 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 三态 |
| §18 | 性能与缓存 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 性能 |
| §19 | 测试要求 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 测试覆盖 |
| §20 | 验收标准 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 验收 |
| §21 | 文档与记忆系统 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 否 | 文档 |
| §22 | 推荐实施顺序 | implemented | N/A | N/A | N/A | N/A | 否 | 描述性 |
| §23 | P0 强化条款（review-1.1.0） | behavior_tested | 8690ccc 修复 | 8690ccc 修复 | [待验证] | test_review_dependency_matrix | 是 | 核心合同已修复 |
| §24 | 第二金字塔定义与冷启动 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 冷启动 |
| §25 | raw与normalized分离 + 冷启动展示 + bootstrap | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 分离 |
| §26 | Review 计算事实、历史观测与发布终态合同 | blocked | [待填充] | [待填充] | [待填充] | [待填充] | 是 | 终态合同 |
| §27 | Review 依赖矩阵与发布质量硬门 | behavior_tested | 8690ccc 修复 | 8690ccc 修复 | [待验证] | test_review_dependency_matrix | 是 | 依赖矩阵已修复 |

### Review 计算链验证

| 验证点 | 状态 | 证据 | 剩余缺口 |
|--------|------|------|----------|
| stock_core 失败阻断 | blocked | [待验证] | 阻断逻辑 |
| chip 缺失生成 core-only | blocked | [待验证] | 降级 |
| auction 失败不阻断 | blocked | [待验证] | 非阻断 |
| 59条不足 → 不可发布 | blocked | [待验证] | 质量门 |
| 60条 ready | blocked | [待验证] | 质量门 |
| 不读未来 | implemented | 8690ccc trade_date > gate | 已修复 |
| industry/concept 隔离 | blocked | [待验证] | 隔离 |
| coverage 按有效 raw/readiness | implemented | 8690ccc COUNT(DISTINCT) | 已修复 |
| all-null 不可发布 | blocked | [待验证] | null 发布 |
| pointer 原子更新 | blocked | [待验证] | 原子性 |
| 重复 publish 零写入 | blocked | [待验证] | 幂等 |
| 旧 pointer 在失败时保留 | blocked | [待验证] | 保留 |

### Review 前端验证

| 验证点 | 状态 | 证据 | 剩余缺口 |
|--------|------|------|----------|
| rawValue/normalizedValue/status/reason | blocked | [待验证] | 字段展示 |
| observationCount/requiredObservationCount/coverage | blocked | [待验证] | 计数展示 |
| source run/chip coverage/degraded reasons | blocked | [待验证] | 来源展示 |
| 区分：无信号/无追踪/历史不足/字段缺失/API错误/provisional/published | blocked | [待验证] | 状态区分 |

**判断**: `review_core_code = largely_closed`, `review_end_to_end = not_proven`

---

## 阶段 6：代码验收门

### 代码质量门禁

| 门禁项 | 状态 | 证据 |
|--------|------|------|
| 目标后端行为测试 | blocked | [待填充] |
| 前端组件与交互测试 | blocked | [待填充] |
| 跨模块合成 E2E | blocked | [待填充] |
| Ruff | blocked | [待填充] |
| Mypy 增量 | blocked | [待填充] |
| TSC | blocked | [待填充] |
| ESLint | blocked | [待填充] |
| Frontend build | blocked | [待填充] |
| Architecture | blocked | [待填充] |
| Docs consistency | blocked | [待填充] |
| Governance | blocked | [待填充] |
| Git status clean | blocked | [待填充] |
| origin/dev SHA 一致 | blocked | [待填充] |

**判断**: `code_ready = false`（只有前面所有垂直切片完成并通过门禁后才能设为 true）

---

## 阶段 7：受控部署与真实数据闭环

### SR 系列需求（系统运行+部署） — 来源: `docs/prd/80-system-runtime.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| SR-01 | 两个运行位置 | implemented | 本地+远程 | N/A | N/A | N/A | 否 | 已确认 |
| SR-02 | 本地使用原生进程 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | 本地进程 |
| SR-03 | 远程使用 Docker Compose | blocked | [待填充] | N/A | N/A | [待填充] | 是 | Docker |
| SR-09 | 长期分支策略 | implemented | main/dev/experiments | N/A | N/A | N/A | 否 | 已确认 |
| SR-10 | 日常开发分支（dev-only） | implemented | 当前在 dev | N/A | N/A | N/A | 否 | 已确认 |
| SR-11 | 稳定分支 | implemented | main | N/A | N/A | N/A | 否 | 已确认 |
| SR-12 | 开发不自动部署 | implemented | N/A | N/A | N/A | N/A | 否 | 已确认 |
| SR-13 | 稳定版本可识别 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 版本标识 |
| SR-14 | 稳定版本手工部署 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 手工部署 |
| SR-15 | 本地参考/传输目录不得进入仓库 | implemented | .gitignore | N/A | N/A | N/A | 否 | 已确认 |
| SR-20 | 共享数据库 | implemented | bz_stock | N/A | N/A | N/A | 否 | 已确认 |
| SR-21 | 本地可读写 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 本地读写 |
| SR-22 | Schema 兼容 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 兼容性 |
| SR-30 | 共享实例 | implemented | Redis | N/A | N/A | N/A | 否 | 已确认 |
| SR-31 | 逻辑库隔离 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | 隔离 |
| SR-31.1 | 本地 Redis DB15 正式保留 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | DB15 |
| SR-32 | 本地 Redis 安全启动 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | 安全启动 |
| SR-33 | 代码一致 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 一致性 |
| SR-40 | 本地 Scheduler | implemented | 不启动 | N/A | N/A | N/A | 否 | 已确认 |
| SR-41 | 本地调试能力 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | 调试 |
| SR-42 | 远程 Scheduler | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 远程调度 |
| SR-43 | 本地启动默认不写入共享库 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 本地安全 |
| SR-50 | 同一套代码 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 代码一致 |
| SR-51 | 差异配置化 | blocked | [待填充] | N/A | N/A | [待填充] | 否 | 配置化 |
| SR-52 | 代码一致，承载方式允许不同 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 承载差异 |
| SR-60 | 部署不默认重建数据服务 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 不重建 |
| SR-61 | 远程任务稳定优先 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 稳定优先 |
| SR-62 | 端口 80 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 端口 |
| SR-70 | 部署环境定位 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 环境定位 |
| SR-71 | 禁止的部署方式 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 禁止方式 |
| SR-72 | panji-test-deploy 正式入口 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | 正式入口 |
| SR-73 | 部署与 CI 的关系 | blocked | [待填充] | N/A | N/A | [待填充] | 是 | CI 关系 |

### 部署流程

| 检查项 | 状态 | 证据 |
|--------|------|------|
| Preflight 与 Migration | blocked | [待填充] |
| 精确 SHA 部署 | blocked | [待填充] |
| Canary 验证 | blocked | [待填充] |
| 单日全量闭环 | blocked | [待填充] |
| 最终数据验收 | blocked | [待填充] |

**判断**: `deployment_phase_ready = false`, `data_closed = false`

---

## 跨模块需求

### SW 系列需求 — 来源: `docs/prd/90-system-wide-requirements.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| SW-01 | 统一时区 | behavior_tested | `Asia/Shanghai` 统一（bar_repository/pytdx_adapter/chart_snapshot_service） | — | — | `test_quote_timezone.py` | 否 | — |
| SW-02 | 时间语义明确 | behavior_tested | 业务交易日 trade_date 与时间戳分离；point-in-time 截断 | — | — | `test_feature_snapshot_service.py` | 否 | — |
| SW-10 | 稳定标识 | behavior_tested | instrument.id UUID + 稳定 symbol；snapshot_run_id/source_run_id 溯源 | — | — | `test_first_pyramid_canonical_contract.py` | 否 | — |
| SW-11 | 标识不可由展示名称替代 | implemented | 内部 ID 独立于 symbol/名称；API 用稳定 ID | — | — | — | 否 | — |
| SW-20 | 统一状态语义 | behavior_tested | `AfterCloseRunStatus` 枚举真源（含 pending/running/partial_success/succeeded/failed/cancelled/interrupted）；API 直出 orchestrator_status + step_summary | `GET /v1/admin/after-close-runs/{id}` | 管理后台 | `test_after_close_phase0_contracts.py` | 是 | 真实状态机全量 |
| SW-21 | 失败不伪装成功 | behavior_tested | run/step 终态含 error_code/error_message；partial_success 保留核心产物不标 succeeded | — | — | `test_after_close_orchestrator.py` | 是 | — |
| SW-30 | 来源可追踪 | behavior_tested | `factor_publication_service` 分层 pointer（stock_core/chip/review）；source_run_id 溯源 | — | — | `factor_publication 单测` | 是 | 分层一致性 |
| SW-31 | 运行版本可追踪 | behavior_tested | snapshot schema_version、algorithm_version、run key 参数 hash | — | — | `test_market_data_quality_service.py::test_algorithm_version_constant` | 是 | — |
| SW-40 | 单一正式结果 | behavior_tested | published pointer 为唯一正式结果来源；读取端统一消费 | — | — | `test_incremental_publication.py` | 是 | read pointer 一致性 |
| SW-41 | 调试与正式结果分离 | behavior_tested | computed/provisional 与 published 分离；未过门禁不可 publish | — | — | `test_review_publication_safety.py` | 是 | — |
| SW-50 | 关键失败可见 | behavior_tested | run/step error_message + 管理事件时间线；heartbeat stale 标记 | `GET /v1/admin/after-close-runs/{id}` | 管理后台 | `test_worker_heartbeat_stale_cleanup.py` | 是 | — |
| SW-51 | 不虚构完成 | behavior_tested | 无指针/无真实数据不标 succeeded；publication gate 校验 | — | — | `test_review_publication_safety.py` | 是 | — |

### PS 系列需求 — 来源: `docs/prd/00-product-scope.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| PS-01 | 产品价值 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |
| PS-02 | 产品定位 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |
| PS-03 | 非预测定位 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |
| PS-04 | 目标用户 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |
| PS-05 | 核心模块 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |
| PS-06 | 产品表达 | blocked | [待验证] | [待验证] | [待验证] | [待验证] | 是 | 待验证一致性 |
| PS-07 | 当前非目标 | implemented | docs/prd | docs/prd | docs/prd | N/A | 否 | 无 |

### MD 系列需求 — 来源: `docs/prd/10-market-data.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| MD-01 | 数据范围 | behavior_tested | `models/bar.py` bars_daily/bars_15min；`instrument_maintenance_service.py` 活跃 A 股范围 | `GET /bars` | 行情页 | `test_bars.py` / `test_instruments.py` | 是 | 真实范围 |
| MD-02 | 日线优先 | behavior_tested | 盘后 refreshing_daily 先于 computing_features；coverage 日线≥0.9 门槛 | — | — | `test_after_close_orchestrator.py::test_ac04_*` | 是 | 真实覆盖率 |
| MD-03 | 统一业务时区 | behavior_tested | `Asia/Shanghai` 遍布 bar_repository/pytdx_adapter/chart_snapshot_service | — | — | `test_quote_timezone.py` | 否 | — |
| MD-04 | 统一股票标识 | behavior_tested | `instrument_maintenance_service.normalize_symbol`（去空格/后缀/大写）；instrument.id UUID 主键 | — | — | `test_instrument_seed.py` | 否 | 跨入口 symbol 一致性需运行时验证 |
| MD-05 | 复权口径明确 | behavior_tested | `adjustment_factor_service.py` qfq/hfq 计算；快照统一 qfq | — | — | `test_adjustment_factor_calculator.py` | 是 | 真实复权 |
| MD-06 | 数据来源可识别 | behavior_tested | `core/pytdx_adapter.py`、mootdx 数据源；source bar hash 溯源 | — | — | `test_pytdx_adapter_minute_aware.py` | 是 | 真实来源 |
| MD-07 | 缺失语义明确 | behavior_tested | `market_data_quality_service` classification（NOT_LISTED/SUSPENDED/DELISTED/DB_MISSING…）+ missing_dates | — | — | `test_market_data_quality_service.py` | 是 | — |
| MD-08 | Readiness | behavior_tested | `BarsCoverageService` coverage（日线≥0.9 门槛） | — | — | `test_bars_coverage_service.py` | 是 | 真实 readiness |
| MD-09 | 数据分层 | behavior_tested | raw bars 表 + qfq 视图 + canonical frame 分层 | — | — | `test_market_data_ssot_architecture.py` | 否 | — |
| MD-10 | 核心资产与可重算结果分离 | behavior_tested | raw OHLCV（核心）与 adj_factor/派生指标（可重算）分离；repair 只写 raw | — | — | `test_market_data_ssot_architecture.py` | 否 | — |
| MD-11 | 数据修复范围明确 | behavior_tested | MQ repair 仅 DB_MISSING/FACTOR_MISSING，写 raw 后按 SSOT 重算 adj_factor | — | — | `test_market_data_quality_service.py` | 否 | — |

### MQ 系列需求 — 来源: `docs/prd/50-market-data-quality.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| MQ-01 | dry-run（零持久写） | behavior_tested | `scripts/market_data_quality_cli.py`（`_run` dry-run 分支）：不创建 run/items，只解析 symbols 打印计划 | N/A | N/A | `test_market_data_quality_service.py`（dry-run 相关） | 否 | 真实全市场 dry-run 未跑 |
| MQ-02 | scan（写审计 run/items，不改 bars） | behavior_tested | `_run` scan 分支 → `MarketDataQualityService.create_run`/`execute_scan`（只写 run/items 审计，不改 bars） | N/A | N/A | `test_market_data_quality_service.py` scan 用例 | 否 | 真实全市场 scan 未跑 |
| MQ-03 | repair（写 raw OHLCV） | behavior_tested | `_run` repair/scan-and-repair 分支 → `execute_repair`（仅 DB_MISSING 写 raw OHLCV，幂等 upsert，不写 qfq） | N/A | N/A | repair 相关用例 | 否 | 真实修复未跑 |
| MQ-04 | verification scan（新 run，禁止复用旧 run） | behavior_tested | repair 后强制 `create_run(mode="verification", source_repair_run_id=...)`；run_key 含 verification → 与 scan 完全独立 | N/A | N/A | `test_market_data_quality_service.py::test_verification_run_key_differs_from_scan` | 否 | 真实 verification 未跑 |
| MQ-10 | --resume 必须显式 --run-id | behavior_tested | `_run` 校验 `--resume` 必须提供 `--run-id`，否则 return 2 | N/A | N/A | resume 相关用例 | 否 | — |
| MQ-20 | --canary 必须在查询前应用 symbols/limit | behavior_tested | `_run` 在 `create_run` 前应用 symbols/limit，并断言 `total_instruments <= limit` | N/A | N/A | canary 断言用例 | 否 | — |
| MQ-30 | market_data_quality_runs | behavior_tested | `MarketDataQualityRun`（`models/market_data_quality.py`，run_key 幂等唯一约束、status/coverage/issue_summary） | N/A | N/A | 模型字段自测 | 否 | — |
| MQ-31 | market_data_quality_items | behavior_tested | `MarketDataQualityItem`（run_id+instrument_id 唯一、classification/issue_type/repair 状态） | N/A | N/A | 模型字段自测 | 否 | — |
| MQ-40 | 命令行参数 | implemented | `_parse_args`：--scan/--repair/--scan-and-repair 互斥、--symbols/--all/--canary、--timeframe、--start/--end、--batch-size、--dry-run/--no-dry-run、--resume、--run-id、--limit | N/A | N/A | — | 否 | — |

### WI 系列需求 — 来源: `docs/prd/50-watchlist-intraday.md`

| ID | 摘要 | 状态 | 后端证据 | API 证据 | 前端证据 | 测试证据 | 需真实验证 | 剩余缺口 |
|----|------|------|----------|----------|----------|----------|------------|----------|
| WI-01 | 自选能力 | behavior_tested | `app/api/watchlist.py` GET/POST/DELETE /watchlist；`require_capability("self_selection")` | `GET/POST/DELETE /watchlist` | 自选页 | `test_watchlist.py`（9 项） | 否 | — |
| WI-02 | 数量限制 | behavior_tested | `require_watchlist_limit()` + `_check_limit_if_needed`（admin=None 无限制；member=int 限额，超限 409） | `POST /watchlist` 校验限额 | 限额展示 | `test_watchlist_limit.py` | 是 | 真实限额 |
| WI-03 | 排序一致 | behavior_tested | 列表按 symbol 稳定排序；`watchlist.py` 列表查询 | — | 列表排序 | `test_watchlist.py::test_list_watchlist` | 否 | — |
| WI-04 | 用户隔离 | behavior_tested | watchlist 按 user_id 过滤；注入 user_id 被忽略 | `GET /watchlist` | 自选页 | `test_watchlist.py::test_user_id_injection_ignored` | 否 | — |
| WI-10 | 权限归属 | behavior_tested | `require_capability("self_selection")` 守卫 watchlist 端点 | `/me/access` | Capability 守卫 | `test_watchlist_permission_uses_access_context.py` | 否 | — |
| WI-11 | 监控对象 | behavior_tested | `GET /watchlist/monitor-status` 返回监控快照 | `GET /watchlist/monitor-status` | 盘中监控 | `test_watchlist_monitor_status_snapshot.py` | 是 | 真实监控 |
| WI-12 | 信息定位 | behavior_tested | monitor-status 按 symbol 定位每只自选的状态 | — | 监控卡片 | `test_watchlist_monitor_status_snapshot.py` | 是 | — |
| WI-13 | 异常标记 | behavior_tested | monitor 事件异常/降级标记（stale/unavailable reason） | — | 监控卡片 | `test_monitor_batch_text_content.py` | 是 | — |
| WI-14 | 信息收益 | behavior_tested | 事件级监控信息（text + batch capture） | — | 监控卡片 | `test_monitor_batch_*.py` | 是 | — |
| WI-15 | 盘中与盘后分离 | behavior_tested | 盘中监控（monitor scheduler）与盘后（after-close）独立 Worker 入口 | — | — | `test_monitor_batch_*.py` / `test_after_close_*.py` | 是 | — |
| WI-20 | 仅两类触发事件 | behavior_tested | 盘中事件判定 Worker 只处理固定触发事件（SMC 结构 + 价格触碰） | — | 事件卡片 | `test_smc_monitor_five_event_types.py` | 是 | — |
| WI-21 | 任一事件固定生成组合图 | behavior_tested | 事件触发固定组合图 capture（MiniKline + FP compact） | — | 事件图片 | `test_monitor_batch_capture_image.py` | 是 | — |
| WI-22 | 事件文字与图片语义分离 | behavior_tested | 文字事件与图片 capture 分离生成，语义独立 | — | 事件卡片 | `test_monitor_batch_text_content.py` / `test_monitor_batch_capture_image.py` | 是 | — |
| WI-23 | 历史兼容 | behavior_tested | 旧 monitor 事件字段经兼容 adapter 读取 | — | — | `test_monitor_rhythm_regression.py` | 否 | — |
| WI-24 | 不新增常驻资源 | implemented | 监控复用现有 Worker 入口，无新增常驻容器 | — | — | — | 否 | — |

---

## 总结

### 当前阶段判断

```text
baseline = f0816ef

after_close_closed = code_verified        # AC-01~AC-73 有真实代码/测试证据；真实环境全量未核验
feature_snapshot_performance_closed = code_verified  # AC-16 批处理指标已输出；固定 fixture 基准/提速50% 未证明
first_pyramid_core_code = largely_closed
first_pyramid_end_to_end = not_proven
smc_core_code = largely_closed
navigation_closed = not_proven
review_core_code = largely_closed
review_end_to_end = not_proven
quant_model_contract_closed = code_verified  # QM-01~QM-63 后端合同有真实代码/测试证据；前端跨入口待验证
cross_system_closed = code_verified        # SW/MD/MQ/WI 后端合同有真实代码/测试证据；真实全市场/监控未核验
admin_closed = code_verified              # PA-01~PA-31 + 统一错误合同有真实代码/测试证据；真实 API/UI 未核验

code_ready = false
deployment_phase_ready = false
data_closed = false
```

### 下一步行动（按 `ref/next.md` 完整端到端执行）

> 本文件是**当前最终 SHA（`f0816ef`）的唯一验收事实源**，不是分阶段交付清单。
> 开发按 `ref/next.md` 的连续端到端工作单执行：从 `ad8b07d` 开始，按依赖顺序走完
> 代码审查/修复 → 后端/API/前端/测试 → 完整质量门 → push origin/dev → 远端代码审查 →
> Migration/部署 → 真实单日数据闭环 → 浏览器验收 → 一次性返回。中途不按模块停下等待指令。

```text
WP-A 建立最新唯一验收基线（本文件基线已对齐 f0816ef）
WP-B 盘后编排 + 权限 + 管理后台最终闭合（AC/PA/SW）
WP-C Feature Snapshot 性能 + 量化模型（AC-16/QM/MD）
WP-D 第一金字塔跨入口 E2E（QM-63/MX-51~64）
WP-E SMC + 行情/详情 + 自选前端闭环（QM-13/21/24/MX/WI）
WP-F Review §0~§27 完整业务闭环
WP-G 跨模块 PRD 收口（PS/SW/MD/MQ/WI）
WP-H 完整代码质量门（含 py_mini_racer 修复）
WP-I 部署 + 真实数据 + 浏览器验收（需一次性明确授权，本轮未授权）
```

状态演进：`code_verified → pg_verified → runtime_verified → browser_verified → data_verified`；
只有远端代码审查通过后设 `code_ready = true`；部署/数据/浏览器验收需在一次性授权后执行。
