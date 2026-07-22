# 项目修改索引

本文件只做索引。每次代码、配置、测试、部署或当前设计变化，都必须使用独立分支并在 `records/` 下建立独立记录。

## 2026-07-22

- CHANGE-20260722-003: 生产验收证据文件 — Phase 4.2 完整验收记录
  - **新增**: `docs/evidence/evidence-2026-07-22-fix-production-pipeline-stability-v1.md`
  - **覆盖**: A-H 全部 8 项验收（容器健康 / 5 周期+实时 / 来源上下文 / Node Cluster / 飞书手动+自动 / SMC 5 类事件 / lease_epoch fencing / 000688 复权）
  - **部署 SHA**: b29da0e（已部署到生产 12 容器，0 重启）
  - **Migration**: 067_scheduler_job_runs_lease_epoch_attempt_no 已应用
  - **资源**: Mem 4.3GiB available、Swap 473M 稳定、Disk 46G free
  - **回滚 SHA**: 8aae487（镜像级别回滚）
  - **遗留问题**: PINE_PARITY_PENDING / instruments 因子版本列未写入 / smc_equal_lows_retest 今日未触发 / 生产未触发 auto-resume
  - **不修改**: 代码/配置/测试/migration/contracts/AGENTS/current/maps
  - **诚实声明**: 所有 ✅ 均有 SQL/API/容器/测试证据支撑；生产未触发 auto-resume 属事实陈述

- CHANGE-20260722-002: mypy baseline 修正 — 从 0 更新为真实诊断集合（CP-20）
  - **mypy 版本对齐**：系统 mypy 1.9.0 → 2.1.0（匹配 pyproject.toml + CI）
  - **main vs branch 对比**：临时 worktree 对比 origin/main（631b191）与 branch HEAD（8aae487），mypy 2.1.0 结果完全相同（1 error + 10 notes）
  - **baseline 更新**：`tools/quality_baselines/mypy.json` 从 `total:0, diagnostics:[]` 更新为 `total:1, diagnostics:[redis_client.py attr-defined]`
  - **处理规则 b**：main 与 branch 完全相同，记录历史债务，不是修复；redis_client.py `aclose` error 是 pre-existing
  - **验证**：`compare_mypy_baseline.py` exit 0，`OK: No new or increased mypy diagnostics relative to baseline.`
  - **.gitignore**：添加 `mypy-report.jsonl`（CI 临时文件）
  - **诚实声明**：10 个 annotation-unchecked 是 note 不是 error，不计入 baseline；redis_client.py error 未修复

- CHANGE-20260722-001: docs/记忆系统真正收口 — AGENTS.md v3 压缩 + ADR/Runbook/Evidence 模板 + MANIFEST baseline 新鲜度门禁（CP-19）
  - **CP-19.1 AGENTS.md v3 收口**：从 909 行压缩到 290 行；移除内联 clause 39-64 变更历史，保留硬规则与必读入口；变更历史指向 `docs/changes/CHANGELOG.md` + `docs/changes/records/CHANGE-*.md`
  - **CP-19.2 ADR/Runbook/Evidence 模板**：`docs/decisions/README.md` + ADR-0001（Atomic Snapshot 单 MDAS）+ ADR-0002（Node Cluster 输入契约隔离）；`docs/runbooks/README.md` + 3 个 Runbook（after-close-recovery / feishu-image-issues / branch-deployment-rollback）；`docs/evidence/README.md` 生产验收证据模板
  - **CP-19.3 MANIFEST baseline 新鲜度门禁**：`tools/check_docs_consistency.py` 新增规则 16（baseline SHA 必须在 HEAD 的最近 50 个 commit 内）；修复 PROMPT.md §4 指出的问题（旧 baseline `18049da` 落后 88 commit 仍通过）；`docs/current/MANIFEST.md` baseline 同步到 CP-18 HEAD `2c4ad50`；3 个新测试场景
  - **诚实声明**：不包含部署、生产验收、merge main；Runbook 命令需部署后验证；Evidence 模板待部署后填充
  - **不变量**：不新增依赖/表/migration；不重写 Node/SMC/Canonical/After-close；MDAS 仍为唯一行情读取出口；`PINE_PARITY_PENDING` 保留

## 2026-07-21

- CHANGE-20260721-002: Display Frame Contract V2 + Node DTO V2 + 移动舞台 V2 + 复权闭环收口（合并 Phase 2-6）
  - **Phase 2：Display Frame Contract V2**：删除 `_display_window=100` 硬编码，改用请求 bars 参数；`display_frame` 新增 V2 字段（`requested_count`/`actual_count`/`first_time`/`last_time`/`include_realtime`/`is_partial`/`adjustment_as_of`）；indicators API 新增 `include_realtime`/`completed_only`/`adjustment_as_of` 参数与 `/bars` 同款；`DisplayWindowSpec.to_cache_suffix()` 编码三参数追加到 indicator_cache key；`ALGORITHM_VERSION` v11→v12 强制旧缓存失效
  - **Phase 3：Canonical Node DTO V2**：`node_cluster` DTO 升级为 `node_regions` + 独立 `price_state`；四链统一消费（详情/盘后/盘中/Capture）；类型特定 Ready 条件（不同 indicator_view 有额外 Ready 条件）
  - **Phase 4：MobileIndicatorStage 1440×2560 V2**：移动舞台组件 + 新建 `chartRenderScale.ts` 集中管理 Canvas 字号/线宽/节点标记尺寸（`renderDensity`，desktop 保持不变，mobile_capture 放大）；`ChartTypography`/`ChartStrokeScale`/`ChartGeometryScale` 三类缩放规格接口；`StrategyChart` 所有 27 处 `drawText` 调用显式传 `scale.fonts.*`；`drawLine` 增加 `scale.strokes.grid` 参数；Playwright 截图选择器 `[data-testid="stock-detail-capture"]`；`global.scss` 风险提示字号 30-32px + 透明度 ≥0.72
  - **Phase 5：复权+Snapshot 生产闭环**：`_invalidate_downstream_caches` 扩展为四层（新增 Capture 缓存精确清理）；新增 `_invalidate_capture_cache` 方法按 instrument_id 精确扫描 `CAPTURE_CACHE_DIR` 删除匹配文件；`_audit_and_rebuild_factors` summary 补全 PROMPT.md §5.4.2 字段（`trade_date`/`audit_rebuilt`/`failed_symbols`）；`_rebuild_factors_if_needed` 返回值新增 `trade_date`
  - **Phase 6：完整测试套件验证**：后端 599 passed + 14 skipped；前端 420 passed + 3 pre-existing failed（structural-state-toggle，StockDetailPage 未修改）；Ruff 全绿；Mypy 3 pre-existing errors；TypeScript 仅 pre-existing errors；ESLint pre-existing 模块损坏；架构测试 46/46 PASS；修复 3 个测试（v11→v12 断言、新增字段断言、chartRightPadding 正则匹配）+ 新增 4 个 Capture 缓存清理测试
  - **诚实声明**：分支部署 + 真实飞书 E2E + 全市场 Snapshot 重算（schema_version=4, ALGORITHM_VERSION=v12）+ PINE_PARITY_PENDING 待后续
  - **不变量**：不新增依赖/表/migration；不重写 SMC 算法；MDAS 仍为唯一行情读取出口；`PINE_PARITY_PENDING` 保留

- CHANGE-20260721-001: 移动飞书指标舞台与数据链修复 V1.0（合并 Phase 2 剩余 + Phase 3 + Phase 4 + Phase 5 后端测试）
  - **Phase 2 剩余：Feature Snapshot 写入 Canonical Node 字段**：`_SCHEMA_VERSION` 3→4（旧快照不可见）；`feature_snapshot_service` `node_cluster` 始终写入 `availability`/`degraded_reason`（即使 profile 为 None 也写入最小诊断字段）；`atomic_fact_contract.py` 新增 `NodeAvailabilityInfo` Pydantic 模型 + `AtomicFactsContextResponse.nodeAvailability` 必填字段；`stock_context.py` 新增 `_build_node_availability` 函数 + 5 态状态机（NO_PUBLISHED_RUN/SNAPSHOT_MISSING/NODE_PROFILE_EMPTY/NODE_15M_MISSING/NODE_COMPUTE_FAILED/NODE_INSUFFICIENT_DAILY_BARS/LEGACY_SNAPSHOT_NO_NODE_CLUSTER）；9 个 StockContext nodeAvailability 测试 + 4 个 Feature Snapshot node_cluster 字段写入测试 + 4 个五周期 Node profile_hash 一致性测试
  - **Phase 3：FR-11 缓存失效修复**：`AdjustmentFactorService.rebuild_factor_series` 改为调用新增 `_invalidate_downstream_caches` 方法（原仅调 `_invalidate_mdas_cache`）；精确失效 MDAS + bars + indicator 三层 Redis 缓存（按 `instrument_id`）；单层失效失败不阻塞其他层（缓存 TTL 会自然过期）；监控 Profile（in-process）和 Capture（filesystem per-event）依赖 TTL 自然过期；4 个 `_invalidate_downstream_caches` 测试
  - **Phase 4：indicator_view 真正控制 Capture**（前序会话已完成）：`stockResearchTypes.ts` `INDICATOR_VIEW_LAYER_PRESETS` + `normalizeIndicatorView` + `getIndicatorViewLayerPreset`；`StrategyChart.tsx` `indicatorView` prop + `effectiveLayers` 预设替代 `FEISHU_CAPTURE_LAYERS`；新增 `MobileIndicatorStage.tsx` 1440×2560 9:16 移动舞台组件；`CaptureStockPage.tsx` 读取 `indicator_view` URL 参数 + `MobileIndicatorStage` 包裹；`backend/app/constants/indicator_view.py` `IndicatorView` 类型 + `is_valid_indicator_view`；`backend/app/api/capture.py` Snapshot API 接收 `indicator_view` 参数驱动 `include_smc`
  - **Phase 2 修复：ChartRenderFrame 帧比对门禁**（前序会话已完成）：`chartRenderFrame.ts` `displayHash`/`displayRangeKey` 优先，降级到 `sourceBarHash`；`indicator_service.py` `build_display_frame()` 生成 display_frame（展示帧与算法输入诊断分离）；新增 `indicator_display_frame.py` 独立模块；`bars.py` + `bar.py` bars API 返回 display_frame
  - **Phase 5 测试**：244 passed + 1 skipped；ruff 全绿（修复 `_DISPLAY_WINDOW` → `_display_window`）；mypy 3 个预存 baseline 错误（`redis_client.py`/`bar_repository.py`，与本次改动无关）；前端测试待 Phase 6 Docker 构建时运行
  - **诚实声明**：Phase 6 部署 + 前端测试 + Playwright E2E + 真实飞书 E2E + 全市场 Snapshot 重算（schema_version=4）待后续
  - **不变量**：不新增依赖/表/migration；不重写 SMC 算法；MDAS 仍为唯一行情读取出口；`PINE_PARITY_PENDING` 保留

## 2026-07-20

- CHANGE-20260720-001: 日线监控 SMC + 三类独立飞书图片 + Canonical 四链生产接入（合并 §一-§五）—— 提交 `cf70e91`
  - **§一 详情页 Node 输入修复**（commits `fffcea1`/`69e700e`）：`MarketDataContext` 字段拆分为 `bars_display`/`display_timeframe`/`bars_daily`/`bars_15min`/`bars_minute`；`WATCHLIST_MONITOR` required_inputs 修复为 `{daily, 15min}`；新增 `_compute_independent_node_cluster` 固定输入 completed qfq 1d×250 + 15m×4000；返回 `data.node_cluster` + `availability` + `degraded_reason`；五周期一致性测试 `TestNodeClusterFivePeriodConsistency`（1d/15m/1h/1w/1mo profile_hash 完全一致）
  - **§二 日线 SMC 盘中监控**（commit `2def073`）：新增 `backend/app/strategy/monitors/smc_monitor.py`；`WatchlistMonitor` 改为 `self._bb + self._vn + self._smc` 三合一；主输入已完成前复权日线，1m 仅用于触发检测；调用现有 SMC Canonical Adapter（禁止复制公式）；FVG 完全排除；第一版事件 `smc_bos_retest`/`smc_choch_retest`/`smc_order_block_first_touch`（含稳定 `smc_entity_id` + `touch_episode` 去重）；`MonitorState` 升级为 `bb/node_cluster/smc/market` 命名空间；`EVENT_LABELS` 5→8
  - **§三 三类监控独立飞书图片**（commit `278b937`）：新增共享枚举 `backend/app/constants/indicator_view.py`（`IndicatorView = Literal["node_cluster", "bollinger", "smc"]` + `EVENT_TYPE_TO_INDICATOR_VIEW` 自动映射 + `resolve_indicator_view`）；`IndicatorView` 贯穿 `StrategyEvent.payload` → `NotificationMessage.resource_refs` → Capture 请求 → `CaptureJob` → 输出文件名 → 缓存键 → 幂等键 → 状态查询 → 前端 URL 参数；三套 Capture Preset `FEISHU_CAPTURE_PRESETS`（layers 互斥，除共享 candlestick）；`build_monitor_event_text(indicator_view, ...)` 按 view 拆分文字卡片字段；`CaptureJob` 新增 `indicator_view` 字段（nullable 兼容历史）；监控自动发送从事件类型映射 `indicator_view`
  - **§四 详情页发送飞书增加选择**（commit `278b937`）：`SendFeishuRequest` body schema（`indicator_view: Literal["node_cluster", "bollinger", "smc"] | None`）；前端弹窗三单选项（筹码共识价/布林带/SMC 结构）；`useStockDetailFeishu` 新增 `selectedIndicatorView`/`setSelectedIndicatorView`/`handleSendFeishu(indicatorView)`；POST body 透传到 capture worker；`output_filename` 加 `-{indicator_view}` 后缀
  - **§五 Canonical 生产接入**（commit `cf70e91`）：`canonical_adapters.py` 新增 re-exports 区段（node_cluster_engine/smc_view_adapter/structural_factor_service/temporal_feature_service/bollinger_features_plotly/merged_dsa_atr_rope_bb_factors/smc_indicator/sqzmom_lb）；`indicator_service.py` 移除 5 个直接 kernel import；`feature_snapshot_service.py` 移除 4 个直接 kernel import；`monitor_batch_service.py` 修复 L1847 延迟 import；`stock_capture_service.py` 早已无直接 kernel import；`compute_macd` 改为延迟 import 避免循环；`test_four_chain_no_direct_kernel_import` 移除 `xfail=True`，AST 守护升级为硬失败；`tests/allowlist.json` 移除 issue #83 条目
  - **诚实声明**：§五 四链当前通过 `canonical_adapters` re-export 接入（满足 AST 门禁），未全部改为 `compute_with_mdas()` 调用（后续优化）；SMC 对齐基准为京东方 A 000725 真实 golden，`PINE_PARITY_PENDING` 保留；§六 文档同步更新 docs/current + maps + AGENTS + CHANGELOG；§七 分支部署 + 真实飞书 E2E + merge main + 最终部署待后续
  - **不变量**：SMC 算法不重写；MDAS 仍为唯一行情读取出口；`ref/smc_user_source.pine` 843 行 + SHA256 不变；`PINE_PARITY_PENDING` 保留；不新增依赖/表/migration

## 2026-07-18

- CHANGE-20260718-001: SMC Pine parity 真实对齐修复（show_trend 默认值 + SMC input contract + deterministic 模式 + parity 测试参数化扩展 + first-divergence 报告 + 对齐范围声明）—— 提交 `6167ce1`
  - **Fix 1 (show_trend 默认值)**：`smc_pine_core.py` `DEFAULT_PARAMS["show_trend"]` 从 `True` 改为 `False`，匹配 Pine L74 `showTrendInput=input(false,...)`；gate 已由 `show_internals=True` 满足，不改计算行为
  - **Fix 2 (SMC input contract)**：`indicator_service.py` 新增 SMC 输入契约文档，明确各周期实际范围（1d DB 全量、15m bars+1000、1h DB 全量、1mo ≥200），禁止再称"全历史"
  - **Fix 3 (deterministic 模式)**：所有 SMC bar 获取改为 `completed_only=True`（强制 `include_realtime=False`）；输出 `smc_mode="deterministic"`，与 TV 历史导出可比；realtime 模式独立标识不得混比
  - **Fix 4 (parity 测试扩展)**：`test_smc_tv_parity.py` 从 515→937 行；TV_COLUMNS 20→37 列；参数化所有 fixture；新增 7 测试函数（internal_bias/trailing/pivot_level/ATR/parsedHigh-Low/default_params/scope_declaration）；NaN 安全 `_float_eq()`
  - **Fix 5 (first-divergence 报告)**：`_format_first_divergence()` 输出首差异 bar 前后 5 根 OHLC + 全部状态 + 触发条件
  - **Fix 6 (对齐范围声明)**：明确"默认结构检测子集对齐"，禁止"Pine 完全对齐"；FVG/MTF/Premium-Discount/Pine 原色显式排除
  - **不变量**：真源 `ref/smc_user_source.pine` SHA256 `0bd3d2ad` 不变；`PINE_PARITY_PENDING` 保留至用户提供 TV CSV fixture；不新增表/migration/worker/依赖

- CHANGE-20260718-002: 文档全盘审核（消除非规范 docs 顶层目录 + 引用收口 + 增强 check_docs_consistency.py）
  - **文档迁移**：`docs/analysis/smc-user-pine-parity.md` → `docs/maps/smc-pine-parity-map.md`（实现地图）；删除 `docs/analysis/`（3 文件）与 `docs/architecture-audits/`（2 文件）非规范目录及空目录
  - **引用收口**：`current/{01,02,05,code-doc-alignment}.md`、`maps/{backend-module-map,test-coverage-map}.md`、`AGENTS.md` clause 45、`smc_indicator.py`/`smc_pine_core.py` docstring 引用全部改指规范路径（CHANGE records / maps）；历史引用保留原路径不可变
  - **增强 check_docs_consistency.py**：新增 `check_unauthorized_top_level_dirs()`（只允许 current/maps/changes/archive + 根 .md）+ `check_change_references()`（校验 CHANGE-YYYYMMDD-NNN.md 引用目标存在）
  - **AGENTS.md clause 57**：docs 顶层目录规范长期规则
  - **不变量**：不删除生产代码/表/migration；不把有效文档移入 archive 制造重复事实源

- CHANGE-20260718-003: 磁盘和构建性能优化（Dockerfile ARG 重排 + BuildKit cache mount + 日志轮转 + cleanup 策略）
  - **版本 ARG 移到依赖层之后**：backend Dockerfile runtime 阶段 `ARG GIT_SHA/BUILD_TIME` 从 apt-get 之前移到所有 COPY 之后，GIT_SHA 变化不再使 apt-get/venv/源码 COPY 层失效；builder 阶段 pip install 仅由 `COPY pyproject.toml` 触发失效；frontend Dockerfile ARG 移到 `npm ci` 之后
  - **BuildKit cache mount**：pip `--mount=type=cache,target=/root/.cache/pip`，npm `--mount=type=cache,target=/root/.npm`；移除 `PIP_NO_CACHE_DIR=1`；所有 Dockerfile 添加 `# syntax=docker/dockerfile:1.4` directive
  - **基础镜像 digest 固定**：`python:3.11-slim@sha256:e031123e...`、`node:20-alpine@sha256:fb4cd12c...`、`nginx:alpine@sha256:54f2a904...`；Capture 保留 playwright tag（维护时显式 --pull）；默认不 --pull
  - **json-file 日志轮转**：`docker-compose.prod.yml` 新增 `x-logging` 锚点（max-size 50m × max-file 5 = 250MB/容器），14 服务全部引用；`/etc/docker/daemon.json` 新增 log-driver/log-opts + builder gc（defaultKeepStorage 20GB）
  - **cleanup 脚本**：`scripts/cleanup-docker.sh` 重写为 KEEP_VERSIONS=2（当前 + 1 rollback），移除 7 天过滤，保护基础镜像（python/node/nginx/postgres/redis/playwright），rmi 前检查运行容器使用，禁止 prune -a/volume prune
  - **Makefile**：`docker-build` target 启用 `DOCKER_BUILDKIT=1` + 传递 `PYPROJECT_LOCK_HASH`（pyproject.toml sha256 → LABEL 审计）
  - **构建验证**：冷构建 backend pip install 85.9s / apt-get 1253s（debian.org 网络慢）；frontend npm ci 4.4s / build 15.7s；热构建 builder 全 CACHED（pip install 0s）+ runtime apt-get 跨版本 CACHED ✓
  - **不变量**：不重建 Capture/PostgreSQL/Redis；不删除 volume/生产数据；不新增项目依赖；buildx 为 Docker CLI 系统插件（apt docker-buildx）

- CHANGE-20260718-004: Node Cluster 唯一语义合同 + 计算内核 + ref/ 彻底隔离 + 文档记忆系统
  - **Node Cluster 三链统一**：新增 `indicator_semantics.py`（语义合同 frozen）+ `node_cluster_engine.py`（计算内核唯一入口）；盘后/详情/监控三链改走 engine，`profile_hash` 必须一致；`NodeClusterProfileResult` frozen dataclass + 鸭子类型适配器
  - **ref/ 彻底隔离**：`git rm --cached ref/smc_user_export.pine`；新增 `test_ref_isolation.py`（AST + 文本扫描守护）；`check_docs_consistency.py` 新增规则 13/14/15；修复 10 处文档 ref 隔离违规（"真源"→"参考源（人工阅读）"）
  - **文档记忆系统**：新增 `docs/current/08-indicator-calculation-contracts.md`（指标计算合同）+ `docs/maps/indicator-computation-map.md`（指标计算地图）；AGENTS clause 59/60 ref/ 隔离规则
  - **版本升级**：`ALGORITHM_VERSION` v10→v11（旧缓存自动失效）；`NODE_CLUSTER_ALGORITHM_VERSION="nc-v1"`、`NODE_CLUSTER_OUTPUT_SCHEMA_VERSION=1`、`NODE_CLUSTER_CONTRACT_FINGERPRINT="nc-cf-v1"`；schema_version 2→3
  - **不变量**：SMC 算法不重写；MDAS/复权/构建治理不重新实现；`ref/smc_user_source.pine` 843 行 + SHA256 不变；`PINE_PARITY_PENDING` 保留；不新增依赖/表/migration

- CHANGE-20260718-005: 复权因子全市场一致性审计 + 串行修复基础设施
  - **因子算法版本常量**：新增 `backend/app/constants/factor_contract.py`（`FACTOR_ALGORITHM_VERSION='fq-v1'` / `FACTOR_RECONCILIATION_VERSION=1` / `FACTOR_COMPARISON_TOLERANCE=1e-6`）；版本变化时触发全市场重审，弥补 xdxr fingerprint 无法发现存量错误的缺口
  - **只读审计服务**：新增 `backend/app/services/factor_consistency_audit.py`（`FactorConsistencyAuditor`）：`audit_single_stock` 加载 stored 因子 → 重算 expected → 逐日比对 → 分类 mismatch（含 603538 bug 模式识别）；`audit_active_stocks` 分批 yield 全市场审计结果；`_compare_factors` 纯函数 6 类场景；`_hash_factor_series` 因子序列内容 hash；零副作用（不写库、不失效缓存、不导入 rebuild，架构守护测试强制）
  - **串行修复任务**：新增 `backend/app/services/factor_reconciliation.py`（`FactorReconciliationTask`）：`dry_run` 只读审计 → 生成修复计划（零副作用）；`rebuild_batch` 全程串行，每只股票独立事务，失败回滚不影响其他；失败不写 1.0 伪装成功（`error_code` 非空、`after_hash` 为空）；`partial_success`（rebuild 后仍不一致）标记失败
  - **只读重算方法**：`bar_repository.compute_expected_adj_factors` 只读重算预期因子序列（不写库），与写库的 `rebuild_adj_factors` 区分
  - **migration 065**：`instruments` 表新增 3 列（`factor_algorithm_version VARCHAR(8)` / `factor_reconciliation_version INTEGER` / `factor_reconciled_at TIMESTAMPTZ`），全部可空，兼容历史 instruments（NULL=未对账）；Instrument 模型同步更新
  - **安全约束**：全程串行禁止并发 rebuild；失败不得用 1.0 伪装成功；不做无边界全市场重跑（只重建审计发现的不一致股票）；603538 真实数据缺失时仅对该股票做小范围补齐/重建
  - **测试**：`test_factor_consistency_audit.py`（442 行，24 passed）+ `test_factor_reconciliation.py`（376 行）+ 38 instrument tests passed；migration 065 upgrade/downgrade/upgrade 验证通过
  - **不变量**：`bars_daily.adj_factor` 仍为权威因子序列；MDAS 仍为唯一行情读取出口；不新增依赖；不运行全市场回补

- CHANGE-20260718-007: 80 端口前端 P0 修复 + CI 诊断清零 + 部署合同静态测试 + PostgreSQL Integration Tests 修复 —— 提交 `51f1178`（待续提交）
  - **P0 80 端口修复**：`frontend/Dockerfile` 误将构建产物 COPY 到 `/usr/share`（非 nginx root），导致用户入口 80 呈现默认 nginx 欢迎页而非 SPA；修复为 `RUN rm -rf /usr/share/nginx/html/*` + `COPY --from=builder /app/dist/ /usr/share/nginx/html/`；5 项内容探针全 PASS（root div / assets 200 / SPA 回退 / API 代理 / 默认 index 删除）
  - **部署合同静态测试**：新增 `tools/tests/test_frontend_runtime_contract.py`（14 用例）守护 Dockerfile COPY 目标、nginx root、compose 80:80、卷挂载、多阶段一致性；CI 阻断回归
  - **CI 诊断清零**：25 Ruff（W292/I001/F401/C401/F541/F841）+ 4 mypy（attr-defined/arg-type/assignment）+ 4 architecture（duplicate-plan-feature-list）全部修复；所有 required CI checks 绿色
  - **PostgreSQL Integration Tests 修复（20 失败 → 0）**：(A) 生产缺陷——`watchlist.py`/`market_stocks_service.py` 共 4 处 `schema_version == 1` 硬编码改为 `_SCHEMA_VERSION`（=3），修复 `_SCHEMA_VERSION` 1→2→3 升级后消费侧读不到新快照的缺陷；`stock_context._empty_atomic_response` 新增 `run` 参数修复 `snapshot_missing` 场景 `hasSucceededRun` 错误归零；(B) 测试未跟进——5 文件修复（watchlist_monitor_status schema_version / admin_after_close_pipeline schema_version / dsa_full_feature patch 目标改 MDAS / stock_state_and_events 删 `_event_to_dto` 过时测试+更新 P0-2 新 API 断言 / worker_idempotency board_sync 迁移到 after_close_orchestrator）；本地全量 2258 passed
  - **不变量**：不改端口（80:80 永久固定）；不改 nginx proxy 逻辑；不改 backend health 路由；不新增依赖；不改 SMC/MDAS/Atomic Fact 业务语义

- CHANGE-20260718-006: 全算法族 SSOT 统一计算网关 + 飞书图片失败状态机 + 周期切换原子渲染 + 四链地图文档
  - **Section 2 算法合同注册表**：新增 `backend/app/contracts/algorithm_registry.py`（12 算法族预注册 + `AlgorithmRegistry` 单例 + `AlgorithmContract` frozen dataclass + `ALGORITHM_REGISTRY_VERSION='reg-v1'`）；新增 `backend/app/services/canonical_computation_service.py`（`CanonicalComputationService` 统一调度 + `result_hash` SHA256 前 16 字符 5 维度确定性）；AST 守护 `test_algorithm_registry_architecture.py`（329 行，16 tests，3 测试类）禁止生产模块直接 `import` kernel 绕过注册表
  - **Section 3 飞书状态机升级**：`partial_failed` → `failed`（图片确定性失败 `image_definitively_failed`：capture 失败 / `image_delivery` failed/dead / `image_upload_status=failed`）或 `pending`（图片仍在进行中）；要求图片时 `card_status=success` 但 `image_status!=success` 整体必须 `failed` 或 `pending`（不允许 `success`）；测试覆盖 `test_state_machine.py`（+208 行）+ `test_stock_detail_feishu_status.py`（+25 行）
  - **Section 4 周期切换原子渲染**：新增 `frontend/src/utils/chartRenderFrame.ts`（279 行，`ChartRenderFrame` + `computeSourceBarRangeKey` + `buildBarsFrame`/`buildIndicatorsFrame` + `isFrameMatched` + `computeVisiblePriceBounds` + `shouldIncludeNodeInPriceRange` + `shouldIncludeSmcTrailingInPriceRange`）；`StrategyChart.tsx` 集成 frame mismatch 横幅 + 纵轴 domain policy（远端 Node/trailing 不参与纵轴候选）；`StockResearchWorkspace.tsx` 构造 `barsFrame` 传入；`api/endpoints.ts` BarListResponse 新增 `source_bar_hash`/`adj_factor_hash`/`market_data_contract_version`/`adjustment_as_of` 契约字段；测试 `chartRenderFrame.test.ts`（397 行，38 tests）+ 前端套件 149 passed + 21 contract passed
  - **Section 5c 文档扩展**：`docs/current/08` 扩展为 12 算法族合同（170→422 行，Section 5 总表 + 5.2.1-5.2.12 各族 12 项规范 + 5.3 调度流程 + 5.4 守护测试）；`docs/maps/indicator-computation-map.md` 扩展为四链地图（148→263 行，详情/盘后/盘中/Capture + 算法族→Kernel→调用链矩阵 + result_hash 缓存层）；`AGENTS.md` clause 61 新增 6 条长期规则（样本股只能验证/每算法族唯一 Kernel/四条链统一网关/Bars+Indicators 原子渲染帧/图片失败不掩盖/复权版本变化触发全市场审计）
  - **不变量**：SMC 算法不重写；MDAS 仍为唯一行情读取出口；不新增依赖/表/migration；`ref/smc_user_source.pine` 843 行 + SHA256 不变；`PINE_PARITY_PENDING` 保留；四条链迁移到 `CanonicalComputationService` 为软约束（逐步迁移）

## 2026-07-17

- CHANGE-20260717-001: SMC Pine 逻辑对齐最终收口（warmup/历史分离 + execution gate + trailing NaN + OB 顺序 + EQH/EQL 几何 + Strong/Weak 起点 + golden 测试重做 + 确定性测试 + 导出增强 + ALGORITHM_VERSION v10）
  - **warmup/历史分离**：`indicator_service.py` 新增 `_SMC_WARMUP_BARS=1000`/`_SMC_MONTHLY_MIN_BARS=200`/`_SMC_MONTHLY_LOOKBACK_DAYS=7000`；15m 独立查询 5000 根（计算）后 adapter 裁成 4000（展示）；1mo 扩展回看 7000 天确保 ≥200 根（ATR200 可初始化）；1d 用完整日线，1h/1w 用 macd_bars 完整历史；删除 "15m≈12000" 失真注释
  - **execution gate 严格复刻 Pine L784/L787**：`smc_pine_core.py` `DEFAULT_PARAMS` 新增 `show_internals`/`show_structure`/`show_trend` 三参数；`internal_gate = show_internals OR show_internal_order_blocks OR show_trend` 门控 internal structure；`swing_gate = show_structure OR show_swing_order_blocks OR show_high_low_swings` 门控 swing structure
  - **trailing NaN 严格复刻 Pine**：`update_trailing_extremes` 改为 `if self.trailing.top == self.trailing.top:`（非 NaN）才更新，删除旧 `or` 分支凭空用 high/low 初始化；`math.max(high, na)=na`，trailing 只能由 swing pivot 初始化
  - **OB 顺序 newest-first**：`store_order_block` 从 `append`（oldest-first）改为 `insert(0, ...)`（newest-first，Pine `array.unshift` 语义）；前端 `slice(0,5)` 取最新 5 个 active internal OB
  - **EQH/EQL 两端点线几何**：`StrategyChart.tsx` 从单一 `eq.level` 水平线改为两端点 `prev_level`→`level`（Pine L396）；EQH=bearish 绿 `SMC_BEAR_COLOR` + label_down，EQL=bullish 红 `SMC_BULL_COLOR` + label_up；标签位于两 pivot 中点（Pine L397）
  - **Strong/Weak 线起点**：新增 `timeToDisplayIdx` 辅助（ISO 时间→display index）；线起点从 `lastDisplayIdx`（最后可见 bar）改为 `trailing.last_top_time`/`last_bottom_time`（Pine L721-727）；终点延伸到 `plotRight`（约 20 bar）；颜色按 strong/weak 区分列为有意视觉差异
  - **adapter 文档清理**：`smc_view_adapter.py` docstring 删除 "12000 根" 失真描述，更新 warmup 分离后的准确描述
  - **golden 测试修复**：`test_smc_tv_parity.py` EQ 类型直接用 core 输出（不再误映射 EQH→EQL）；日内时间戳用 `isoformat()`（不压缩为日期）；容差从 ±1 改为 0（严格逐 bar）；新增 OB/EQ 端点/全链 3 个测试
  - **新增确定性测试**：`test_smc_pine_deterministic.py`（427 行，8 测试类：CHoCH 规则/BOS/warmup 一致性/OB 顺序/OB 全链/trailing NaN/execution gate/EQ 几何），不依赖 TV CSV fixture
  - **导出增强**：`ref/smc_user_export.pine` 追加 8 个隐藏 plot（trailing top/bottom/lastTopTime/lastBottomTime + swing/internal pivot level）；真源 `ref/smc_user_source.pine` 不可变（SHA256 `0bd3d2ad`）
  - **ALGORITHM_VERSION v9→v10**：`indicator_cache.py` bump 使旧 SMC 缓存强制失效；`test_indicator_cache.py` 断言 v10
  - **parity 口径**：以已完成 bar 为阻断口径；无真实 TV CSV fixture 时 `PINE_PARITY_PENDING`（代码级修复通过，输出级 parity pending），不伪造 fixture，不声称"完全对齐"

- CHANGE-20260717-002: 市场数据 SSOT 统一出口 + 前复权修复（MDAS 唯一出口 + AdjustmentFactorService + adj_factor 列信任 Bug 修复 + adjustment_as_of point-in-time + factor rebuild 完整重建 + 架构守护 + 603538 真实回归）
  - **MDAS 唯一行情出口**：`MarketDataAggregationService` 为行情读取 + 复权应用 + 周/月聚合唯一出口；业务层（indicator/strategy_batch/feature_snapshot/chart_bars/structural/capture/monitor/bars API）全部收口走 MDAS，禁止导入 repository 私有 `_query_*`/`_get_adj_factor_df`/`apply_adj_factor_to_bars`/旧 `get_bars`
  - **adj_factor 列信任 Bug 修复**：`adj_factor.py::_apply_adj_factor_core` 始终使用权威因子序列 `merge_asof` 结果 `_adj`，移除 `if "adj_factor" in merged.columns` 分支；pytdx hybrid bar / 15m/60m/1m 行内自带 `adj_factor=1.0` 不可信，必须由权威日线因子覆盖
  - **adjustment_as_of point-in-time 复权**：公式 `qfq = raw × factor(bar_date) / factor(as_of)`；`AdjustmentFactorService.get_factor_series(as_of=)` 返回只含 `trade_date <= as_of` 的截断因子序列，禁止未来除权事件泄漏；当前页面锚定请求业务日（None=最新），盘后/历史回算 `as_of=trade_date`
  - **日内/周/月复权口径统一**：15m/60m/1m 同一交易日映射同一权威日线因子；周/月"日线完成复权后再聚合"（经 `kline_aggregator`，仅 MDAS 导入）；禁止 raw 聚合后再复权
  - **factor rebuild 完整重建**：公司行为/fingerprint 变化时从最早受影响日期重新计算完整日线 factor 序列并原子 upsert（禁止只更新最近 5 根）；成功后精确失效该股票 MDAS/indicator 缓存；失败不得用 1.0 伪装成功，返回 degraded + 原因
  - **盘后顺序**：原始日线刷新 → 公司行为/factor 重建（成功）→ 覆盖率门禁/DSA → snapshot 发布；因子未完成不得创建 DSA 或发布 snapshot
  - **MDAS v2 请求契约**：参数 timeframe/adj/include_realtime/completed_only/start-end/limit/warmup_bars/adjustment_as_of；返回 bars + market_data_contract_version/source_bar_hash/adj_factor_hash/adjustment_as_of/completed_through/degraded 诊断；缓存键含全部参数 + 版本，true/false 隔离
  - **feature_snapshot schema v2**：盘后调用 MDAS 显式 `include_realtime=False, end_date=trade_date, adjustment_as_of=trade_date`，保存 hash/contract_version/completed_through/as_of 到 run metadata；schema version 递增，禁止新旧语义混用；alembic 063↔064 落库
  - **strategy_batch SSOT 收口**：从 `bar_repository.get_bars` 迁移到 MDAS，DSA 使用 `timeframe=1d, adj=qfq, include_realtime=False, completed_only=True, adjustment_as_of=run.trade_date`
  - **架构守护 AST 测试**：`test_market_data_ssot_architecture.py` 5 个测试（禁止业务模块导入 repository 私有查询/直接导入 adj_factor/导入 kline_aggregator/自行 resample 周/月；正向守护 MDAS 导入私有查询）；例外 `strategy_assets/algorithms/` 算法内部特征计算（SMC PDH/PDL）
  - **603538 真实回归**：验证脚本 `verify_603538_step6.py` 6 步全通过（factor rebuild 856 根/除权日 07-09 factor 0.7115→1.0；1d/15m/1h × none/qfq 价格连续；as_of 三锚点无未来泄漏；对照股 600276 factor 全 1.0；跨调用方 hash 一致 source=48d5bd812528ca42/adj=262a210aea141032；rebuild 幂等）
  - **SMC parity**：仅验证输入一致和无回归，不宣称 Pine 输出级完全对齐，保留 `PINE_PARITY_PENDING`

## 2026-07-16

- CHANGE-20260716-007: 板块同步迁移 pywencai 唯一数据源（BoardSnapshot 原子切换 + 软失败编排 + 陈旧数据契约 + 行业关键词 ilike 筛选 + BoardFilterCombobox 自定义下拉 + Dockerfile Node.js + BOARD_SYNC_ENABLED 环境变量注入）
  - **数据源迁移**：删除 `backend/app/services/qstock_fetcher.py`、`backend/app/services/ths_adapter.py`、`backend/tests/test_qstock_fetcher.py`；新增 `backend/app/services/wencai_board_provider.py`（pywencai 作为唯一板块分类数据源，查询 `同花顺概念，行业分类`，通过 `asyncio.to_thread` 包装同步调用，附带 Referer 头，3 次重试）
  - **board_sync_service 重构**：采用 `BoardSnapshot` 暂存集合 + 事务内原子切换（TRUNCATE+INSERT）；硬门禁（绝对门禁）——原始记录 ≥5000、代码唯一性 ≥99.9%、行业板块 ≥200、概念板块 ≥300、关系数 ≥60000、解析率 ≥95%；相对门禁（与上一成功版本比较降幅 >20% 拒绝）；首次同步不做相对降幅检查
  - **after_close_orchestrator 新增 syncing_boards 步骤**：位于 `refreshing_daily` 与 `waiting_dsa_worker` 之间；软失败设计（不阻断 DSA/snapshot/publish 链路）；非交易日自动跳过；`mode=dsa_only` 模式跳过
  - **worker.py**：删除原 17:00 独立 qstock board_sync 任务；`BOARD_SYNC_ENABLED` 环境变量保留并改为 pywencai 语义（默认 `true`，`false` 时 `syncing_boards` 步骤跳过）
  - **/market/boards 响应扩展**：新增 `source`（str\|null，数据源标识）、`stale`（bool，存在旧数据但最新同步失败时为 true）、`last_attempt_status`（str\|null，最近一次同步状态）；stale=true 时前端展示"沿用上次板块数据"
  - **前端筛选行为**：行业/概念输入仅 Enter/失焦提交（不再每次按键提交）；清空立即提交并重置分页；`boards.available=false` 时输入禁用；`stale=true` 时输入仍可用但显示"沿用上次板块数据"提示；行业值 `-` 在前端可渲染为 `/`（API 值不变）
  - **Redis 状态跟踪**：`record_sync_status()`/`get_sync_status()` 写入 key `board_sync:status`，TTL 7 天
  - **依赖变更**：`pyproject.toml` 移除 `py-mini-racer`，新增 `pywencai==0.13.1`，保留 `qstock==1.3.1`
  - **测试覆盖**：后端 `test_wencai_board_provider.py`（53 用例）、`test_board_sync.py`（17 用例，含 source=wencai）、`test_after_close_board_sync.py`（10 用例覆盖软失败/非交易日跳过/dsa_only 跳过）、`test_board_filter_helper.py`（23 用例覆盖 ilike 匹配/转义/NFKC/AND）、`test_board_sync_enabled_config.py`（12 用例覆盖环境变量解析）；前端 `wencaiBoardSyncContract.test.ts`（28 用例覆盖 BoardFilterCombobox + stale/source/last_attempt_status/禁用输入/Enter 提交/`-`→`/` 渲染）、`marketToolbarSearch.test.ts`（8 用例 keywordInput）
  - **ALIGN-041 关闭**：pywencai 配合 `WENCAI_COOKIE` 在物理机真实烟测返回完整目录与代表性成分，ALIGN-041 由 KNOWN_GAP 移入 CLOSED
  - **PR #77 收口（行业关键词筛选 + 自定义 Combobox + 同步链路遗留修复）**：
    - **行业筛选语义改为关键词匹配**：`board_filter_helper.build_board_filter_conditions` 的 industry 条件从 `MarketBoard.name == industry` 改为 `MarketBoard.name.ilike('%keyword%', escape='\\')`，匹配完整路径中的任意一级；`_normalize_keyword()` NFKC 规范化 + trim；`_escape_ilike_pattern()` 转义 `\`/`%`/`_`；空值不生成条件；concept 保持精确匹配；industry+concept 继续 AND；market stocks/StrategyRunResults/行情/自选/Excel 复用同一 helper；URL/preset/导出字段名继续用 `industry`（语义为关键词）
    - **BoardFilterCombobox 自定义下拉**：删除 `MarketToolbar.tsx` 原生 `<datalist>`，新增 `BoardFilterCombobox.tsx`（行业+概念共用）；行业模式允许任意关键词、placeholder「搜索行业关键词」、本地过滤完整路径最多 12 条建议、展示「一级 / 二级 / 三级」并高亮命中、Enter 提交关键词/点击提交完整路径/清空立即提交；概念模式本地搜索目录、只提交精确概念、不逐字符请求后端；ArrowUp/Down/Enter/Escape + 点击外部关闭 + 清除按钮 + aria-combobox/listbox/option + 150ms blur 延迟解决点击问题；盘迹 SCSS 变量、行业 200~240px、概念 160~200px、深色 panel/1px 边框/8px 圆角/轻阴影/荧感绿 focus/青绿色 hover/最大高度 320px/z-index 高于表格
    - **Dockerfile Node.js 依赖**：`backend/Dockerfile` 安装 `nodejs`（pywencai `get_token()` 需 `subprocess.run(['node', ...])` 执行 `hexin-v.bundle.js` 计算反爬 token）；纳入 commit，不能让 c41799c 镜像与 Git 代码不同
    - **source=wencai 修复**：`board_sync_service.sync_boards()` 成功返回 dict 显式带 `source: "wencai"`，防止手工 `record_sync_status(result)` 丢失 source；`/market/boards` 不再返回 `source=None`
    - **BOARD_SYNC_ENABLED 环境变量注入**：`config.py` 新增 `_resolve_board_sync_enabled()`（优先级：环境变量 > CONFIG_FILE > 默认 False）；`docker-compose.prod.yml` worker-after-close 服务注入 `BOARD_SYNC_ENABLED: ${BOARD_SYNC_ENABLED:-false}`；启用时真实执行 syncing_boards，禁用时明确 skipped
    - **249 只未解析股票分类**（仅调查，不降低 95% 门禁）：全部 `DB_NOT_EXIST`，BJ 234 + SH 13 + SZ 2，以 920xxx 北交所新股为主；调查脚本 `backend/scripts/classify_unresolved_stocks.py`（PR #77 收口第二轮已删除，仅一次性调查用途）
  - **PR #77 收口第二轮（P0 前端 + P1 后端 + 新测试）**：
    - **P0 BoardFilterCombobox 行为收紧**：`openPanel()` 与 `handleInputChange` 改为 `activeIndex=-1`（不再默认选中首项，Enter 不自动选第一条）；Enter 无激活建议时行业提交关键词（非首条路径）、概念仅当存在精确匹配时提交；`normalizeInput` 增加 `.normalize('NFKC')` 与后端 `_normalize_keyword` 对齐；`useId()` 生成唯一 listbox/option ID 防多实例冲突；新增 `suggestionRank()`（exact=0/prefix=1/contains=2 + `localeCompare('zh-Hans-CN')` 稳定排序）；清除按钮移除 `tabIndex={-1}` 恢复键盘可达；新增「无匹配行业」/「未找到该概念」无结果反馈（`hasInputNoMatch`）；SCSS 行业面板宽 360-480px（原 220px）、概念面板最大 240px、`.comboboxPanel` 改 `min-width: 100%`（原 `left:0;right:0`）；建议项 `<li>` 增加 `title` 显示完整行业路径
    - **P1 后端修复**：`board_filter_helper.py` concept 应用 `_normalize_keyword()`（NFKC+trim）后再 `==` 精确匹配（原 raw string）；`config.py::_resolve_board_sync_enabled()` 严格解析（truthy 集合 `{1,true,yes,on}` / falsy 集合 `{0,false,no,off}`，非法值 `RuntimeError` fail-fast，修复 `bool("false")` bug，同时处理 bool 与 str）；`after_close_orchestrator.py` 模块 docstring 状态机补 `syncing_boards`，新增 `_record_board_sync_outcome()` 同时写 `job_run_events`（`append_event`）和 `SchedulerJobRun.metadata_json.board_sync_result`，在 success/failure/skip 三分支调用；`market_stocks_service.py` 新增 `_get_board_sync_status_from_job()` 从近期 `SchedulerJobRun.metadata_json` 读取 `board_sync_result` 作为 Redis 缺失时的回退，`get_market_boards()` Redis None 时回退 job metadata，DB 有数据但无任何状态源时 `source="unknown"`（非 None）；删除一次性调查脚本 `backend/scripts/classify_unresolved_stocks.py`
    - **新测试**：`test_board_filter_helper.py` +8（concept NFKC+trim，共 32）；`test_board_sync_enabled_config.py` +16（config 文件严格解析，共 28）；`test_board_sync_status_fallback.py` 新增 14（Redis 回退 + event/metadata 写入）；`wencaiBoardSyncContract.test.ts` +15（P0 修复契约 + SCSS 宽度契约，共 37）；全部通过
    - **EXPLAIN ANALYZE 性能验收**（生产数据）：「电子」0.819ms、「半导体」0.589ms、「电子 + 光刻机」AND 1.044ms
  - **部署**：`COMPOSE_PARALLEL_LIMIT=1 NODE_OPTIONS=--max-old-space-size=1536 CORE_ONLY=1 ./scripts/deploy.sh` 重新部署前后端和核心 worker

- CHANGE-20260716-005: AFC V1 终审修正（M5 单侧缺失 + per-fact 精度 recentChanges + PersistedAtomicFactsPayload 严格 schema + as_of 截止语义 + legacy degradedReasons + meta 三版本 + presentation secondaryLabel 真源 + 前端布局修正）
  - **后端 M5 单侧缺失**：`_squeeze_state` 改 `if on is None or off is None: return None`（任一缺失即缺失，旧 `and` 改 `or`）；新增四个单侧缺失组合测试均不进入 Core
  - **RecentChanges per-fact 精度**：`_quantize_fact_value` 按 presentation `valuePrecision` 量化（禁止统一 `round(...,4)`）；`FACT_DIMENSION_BY_ID` 从冻结合同导出 fact_id→dimension 映射，事实消失时仍返回正确维度（禁止默认 trend）；`_combine_text` 组合短值和 category（避免丢失 M3 双文本状态）
  - **PersistedAtomicFactsPayload Pydantic schema**：`extra="forbid"` + `model_validator` 严格校验四版本/core 键/publicKey 维度/无重复/T3/T6/V1 禁止/availability 一致/无 debug；不兼容必须 fallback 不得 500；7 种损坏类型测试均 fallback
  - **as_of 截止语义**：`trade_date <= as_of` + DESC 取最新 1 条；周末/无批次日期返回之前最近发布状态
  - **Legacy degradedReasons**：legacy snapshot 存在但 source_run_id 缺失/歧义时，reasonCode 加入 degradedReasons（不清除原因）；无 snapshot 才用 reasonCode 作空态原因
  - **meta 三版本**：`AtomicFactsMeta`（payloadVersion/researchFreezeVersion/presentationVersion）加入 `AtomicFactsContextResponse`；前端禁止硬编码 V4.13
  - **presentation secondaryLabel 真源**：`_secondary_text_for` 统一从 presentation 映射生成 secondaryText；`unclassifiedLabel` 顶层字段；移除散落 `ATR / 根日K`/`个交易日`/`分类未启用` 硬编码常量
  - **前端 factRow secondary 右列**：`grid-template-areas "label value" / ". secondary"` + `text-align: right`
  - **前端 PositionRow 独立布局**：`grid-template-areas "label caption" / "track track"`（轨道横跨整组宽度）；`railScale` `space-between` 四刻度；`min-height` 预留刻度高度禁止与 caption 重叠
  - **前端 RecentChanges deltaText**：渲染 `c.deltaText`（`changeDelta` class）；4 列 grid；禁止 publicKey
  - **前端 Drawer Tab 双向**：正向 Tab 也检查 `!drawer.contains(active)`，与 Shift+Tab 对称
  - **前端 Header meta**：移除 `AFC_RESEARCH_VERSION` 常量；从 `data.meta.researchFreezeVersion` 读取
  - **测试**：后端 56/56（service + stock_context）+ ruff clean + mypy 仅 pre-existing；前端 contract 26/26（新增 5 项）+ tsc/eslint clean + vite build 成功；4 docs 检查全 PASS
  - **文档**：07/02/04/05/MANIFEST/code-doc-alignment/maps/AGENTS + 本 CHANGE + CHANGELOG；明确 10 个 Aux 中仅 8 个可展开、as_of 截止语义、严格 persisted schema、worker 旧镜像 Known Gap
  - **部署**：仅 `docker compose up -d --no-deps backend frontend`，不重启 worker/capture/PG/Redis；worker 旧镜像 Known Gap（新 summary 持久化未生产验证）

- CHANGE-20260716-004: AFC V1 原子值 UI 改造（短原子值 + visualKind 统一枚举 + 持久化/调试分离 + 前端状态观察重构）
  - **后端展示契约**：`valueText` 改短原子值（T1=`上行`、T2=`+0.0123`+`ATR / 根日K`、T4=`18`+`个交易日`、T5=`1.23×`+`分类未启用`、M1/M5/S1/S2=仅 categoryLabel、M3=`+0.000300`+categoryLabel、S3=`0.63`+轨道、S7/S8=`1.23 ATR`+`尚未到达/已越过`、V3=`1.11×`+`分类未启用`）；统一格式器 `_fmt_atomic_value` 读 presentation `valuePrecision`（禁止散落 `.4f/.6f`）；`visualKind` 统一枚举 `metric/value_with_category/relation/position/distance/ratio`；M5 任一缺失即缺失+双true质量异常+文案`正在收紧/正在释放/正常`；`recentChanges` 加中文 `label`（禁止 publicKey 泄露）
  - **持久化/调试分离**：`compute_atomic_facts()` 仅 core/aux/availability（无 debug）；`compute_atomic_fact_debug()` 管理员即时生成；`build_persisted_afc_payload()` 包装四版本字段（`payloadVersion=1`/`researchContractVersion`/`researchFreezeVersion=V4.13`/`presentationVersion`）+ core/aux/availability，无 debug；`feature_snapshot_service` 改用之；`_is_valid_stored_afc` 严格校验四版本+四组+publicKey+无 debug，不满足→fallback 重算（不回写）；admin debug 走 `compute_atomic_fact_debug(snapshot.payloads)`；persisted-first==fallback；GET 零写入；旧 worker 旧格式由 validator fallback 兼容
  - **前端重构**：`FactRow` 按 visualKind 渲染去重（relation 仅 categoryLabel 一次、distance 徽章+数值各一次、ratio secondaryText 仅一次）；`.factRow` 改 CSS Grid 透明行（非嵌套卡片）；S3 完整轨道（低位/0.33/0.67/高位+圆点+`0.63 · 中间`）；Auxiliary 按 动量补充/结构补充/成交补充 分组默认收起；RecentChanges 中文 label；Drawer 焦点 trap+关闭恢复焦点+body 滚动锁定+Escape/遮罩/按钮关闭
  - **测试**：后端 44/44（service 25 + stock_context 14 + contracts 5）+ ruff clean + mypy 仅 pre-existing；前端 contract 21/21 + tsc/eslint clean + vite build 成功；模块自测 OK
  - **文档**：07/02/04/05/MANIFEST/code-doc-alignment/maps/AGENTS + 本 CHANGE + CHANGELOG；明确 valueText=短、visualKind 渲染、summary 无 debug、M5 任一缺失规则、worker 旧镜像 Known Gap
  - **部署**：仅 `docker compose up -d --no-deps backend frontend`，不重启 worker/capture/PG/Redis；worker 旧镜像 Known Gap（新 summary 持久化未生产验证）

- CHANGE-20260716-003: AFC V1 双合同分离 + 前端 Compact/Expanded 重构与契约对齐（在 002 基础上继续）
  - **双合同分离**：`atomic_fact_contract_v1.json`（V4.13 冻结研究合同，移除全部 `public_key`/`public_label`，不含产品层语义）+ 新增 `atomic_fact_presentation_v1.json`（按 Fact ID 映射 `publicKey/publicLabel/visualKind/valuePrecision/groupTitle/secondaryLabel`，**恰好 14 Core + 8 Auxiliary，排除 T3/T6/V1**）；生产服务同时读取两份合同（frozen 决定事实/顺序/公式/阈值/路径，presentation 决定产品文案与 UI 类型）
  - **DTO 拆分**：`PublicAtomicFactItem`（无 `factId`/`sourcePath`/`formula`/`thresholdRef`）+ `AdminAtomicFactDebugItem`（保留 factId/publicKey/sourcePath/rawValue/thresholdRef/thresholdEnabled/featureFlag/missing）；缺失事实由 `compute_atomic_facts` 从 Core 数组直接省略（分母固定 14，`availability.coreMissing` 用 publicKey）；M3 不声称 1e-6 已确认（仅 raw>0→增加/raw<0→减少/raw==0→基本不变，`thresholdEnabled=false`）；M5 任一输入缺失即省略、双 true→dataQuality 异常；S1 未知枚举省略；S3 越界省略；S7/S8 管理员 `sourcePath` 随趋势方向动态变化；recentChanges 按展示精度比较返回 fromText/toText/deltaText
  - **persisted-first**：Context API 优先读取已持久化 `summary_payload.atomic_fact_contract_v1`（校验后直接返回），缺失/版本不符/结构不匹配 → 同一纯函数 fallback（不回写旧快照）
  - **前端重构**：`AtomicFactsPanel` 重写组件树（Header/CoreFactGroup/FactMetricRow/RelationBadge/PositionRail/BoundaryRow/AuxiliaryAccordion/RecentChangesStrip），compact `/market` 右栏四张组卡（趋势 info/动量 brand/结构 purple/成交 warning，每项一行，S3 0–1 轨道 0.33/0.67、T5/V3 比值+「分类未启用」、S7/S8「尚未到达/已越过」）；新增 `AtomicFactsDrawer`（右侧 overlay，宽 `min(1080px, calc(100vw-48px))`，不压 K 线，Escape/遮罩/关闭可关，4/2/1 列响应式，Auxiliary 默认收起展开 8 项，T3/T6/V1 不出现 DOM）；`StockDetailPage` 改用 Drawer 替代内联窄 aside；`AdminStockDebugPage` 近期变化列改用 publicKey/fromText/toText/asOf；scss 仅用 `variables.scss` token 无硬编码十六进制
  - **测试**：后端纯函数 25 + 双合同结构 5 + API 集成 14 = 44/44（独立测试库 `bz_stock_test`），ruff/mypy 0；前端 contract 8、tsc/eslint/vite build 通过
  - **文档**：07/02/04/05/MANIFEST/code-doc-alignment（ALIGN-073）/backend+api+frontend+test+deployment maps/AGENTS/本 CHANGE/本 CHANGELOG 全部同步；明确 frozen↔presentation 分离、backend/frontend 早期部署 worker 旧镜像、当前靠 fallback、worker 升级前新 summary 持久化未生产验证、T5/V3/M3 阈值未确认、近期变化非 Core、未证明投资价值
  - **部署**：仅 `docker compose up -d --no-deps backend frontend`，不重启 worker/capture/PostgreSQL/Redis；worker 旧镜像 Known Gap（ALIGN-073）
  - **遗留**：全生产链路 E2E（CDP）待补；worker 升级后验证新 summary 持久化 + persisted-first 直读

- CHANGE-20260716-002: Atomic Fact Contract V1 个股状态观察（纯函数 + 快照/Context API + 前端面板）
  - Canonical Registry `atomic_fact_contract_v1.json`（V4.13 冻结，14 Core / 10 Aux / 1 Rejected=V1 累计成交量比；S2 存在；T3/T6 `ui_enabled=false`）
  - 纯函数 `compute_atomic_facts` / `compute_recent_changes`（新快照与旧 summary fallback 共用同一公式；近期变化非 V4.13 Core Fact）；S3 严格 0.33/0.67；S7/S8 禁止负距离；T5/V3 阈值未确认→仅比值+「分类未启用」
  - `stock_context.py` 复用接口返回原子事实结构（contractVersion/asOf/core/auxiliary/availability/recentChanges/dataQuality）；GET 零写入、as_of point-in-time、V1 永不进 payload；admin debug 含 `rawDebug` 可追溯底层 feature/factor
  - `feature_snapshot_service.build_summary_payload` 追加 `atomic_fact_contract_v1`（仅新快照写入，旧已发布快照受 upsert 保护不覆盖）
  - 前端 `AtomicFactsPanel`（compact=`/market` 右栏 + expanded=`/stock/:symbol`，按钮「显示/隐藏状态观察」）；删除旧 `EventStatePanel`；复用 `useStockContext`（收起 enabled=false → 0 请求）
  - 测试：后端纯函数 19/19 + API 集成 6/6（独立测试库，非生产库）；前端 contract 58/58、tsc/eslint 0、build 成功；ruff/mypy 0
  - 文档：07-atomic-fact-contract-v1 + AGENTS + 02/04 + code-doc-alignment + 4 maps + 本 CHANGE
  - 遗留：生产部署验收待步骤六执行

- CHANGE-20260716-001: 盘后任务历史恢复 API + SMC 逐 Bar 对齐 Pine + MiniKline 真实留白 + indicator_service 按需加载（合并原 001-007）
  - **目标一 盘后 resume**：`POST /admin/after-close-runs/{id}/resume` — SELECT FOR UPDATE、同日 queued/running 互斥（409 SAME_DAY_ACTIVE_RUN）、幂等返回 queued、清 worker/heartbeat/lease、metadata 写 resume_requested_at、唯一 manual_resume 事件
  - **P0 修复**：repair 按 `source_run_id==snapshot_run.id` 统计；DSA 未 published 不标 succeeded（`resume_pending`）；publishing 从 DB 读真实 count（禁止 0/None）；feature_snapshot 复用已有 running run；repair 后补 commit
  - **管理页**：交易日选择器（默认最新，可选历史日期如 2026-07-15）+ "继续未完成任务"按钮（interrupted/failed 显示）
  - **SMC 核心**：`ta.crossover/crossunder` 每 Bar 快照上一 Bar pivot level（`close[i]` 对当前 level，`close[i-1]` 对上一 level，禁止 current_level 传两次）；三个 leg() 独立持久状态（swing50/internal5/equal3）
  - **EQH/EQL DTO 三时间点**：anchor=前pivot，second_pivot=i-size（视觉端点），confirmed=i（因果）；阈值用确认 Bar 的 `0.1*ATR200` 严格 `<`
  - **OB 统一**：`anchor_index/time`，slice end-exclusive，unshift 最多 100，新建 OB 参与同 Bar mitigation；前端只画头部最近 5 个 `internal&&!mitigated`
  - **swing_bias 后端返回**：`swing_bias=self.swing_trend.bias`（1/-1/0），前端从 DTO 读取禁止猜测
  - **view adapter**（`smc_view_adapter.py` 新增）：核心用完整历史，API 只输出展示窗口有界 DTO 并统一重基准索引；跨左边界 event/EQ/OB 保留并 `clipped_left=true`
  - **纵轴范围**：加入可见 event.level、OB high/low、EQ level、trailing
  - **缓存版本**：SMC/non-SMC 隔离，cache key 含算法版本
  - **输入门禁**：API 返回 `smc_source_bar_hash/first_time/last_time/bars/adj`；输入不一致写 `INPUT_BAR_MISMATCH`，禁止改算法迎合截图
  - **TV fixture**：`ref/smc_user_export.pine` 保留导出代码；无 TV CSV 时 `PINE_PARITY_PENDING`（不宣称完全对齐）
  - **FVG 彻底排除**：不计算、不返回、不缓存、不渲染、无 toggle
  - **MiniKline 真实留白**：visibleData 切片（48/44/40/36/30 根）；`setVisibleLogicalRange({from:-2,to:dataLength-1+3})`；autoscale 基于 visibleData（上 12%/下 15%）；tabs 五等分 grid；chart 190px/价格轴 56px；cleanup 清旧 data/rAF/ResizeObserver
  - **detailSource 统一**（`detailSourceContext.ts` 新增）：`normalizeResearchSource/defaultStrategyForSource` 唯一定义点
  - **indicator_service 按需加载**：只加载当前周期和实际可用策略 required_inputs（`_REQUIRED_INPUTS` 映射 + `_determine_required_bars()` 合并）
  - 测试：after_close pytest 42 passed（含 3 新增 resume 测试）、SMC pytest 75 passed/4 skipped（PINE_PARITY_PENDING）、前端 171 passed、contract 274 passed、ruff/mypy/typecheck/eslint 0 新增错误、前端 build 成功
  - 约束：`ref/smc_user_source.pine` 保持原 843 行和 SHA256；resume API 禁止手工 SQL 恢复
  - 遗留：Pine golden fixture PENDING（等待用户从 TradingView 导出 CSV）；生产 E2E 待部署后执行

## 2026-07-15

- CHANGE-20260715-006: MiniKline 闭包根治 + SMC Pine 对齐（RMA NA 语义 + 首个 pivot off-by-one + EQH/EQL 三时间点）
  - MiniKline 闭包根治：`MiniKlineCard.tsx` 新增 `barsLengthRef`/`timeframeRef`/`rafIdRef`；`applyViewportRange` 改为 `useCallback([], )` 稳定函数从 refs 读取；新增 `scheduleApplyRange` 稳定函数取消 pending rAF 后调度新 rAF；`ResizeObserver` 回调调用 `scheduleApplyRange`（不直接闭包捕获 bars/timeframe）；卸载清理取消 pending rAF
  - SMC Pine 对齐 `pine_rma` NA 语义：`smc_pine_core.py` 的 `pine_rma` 在 `bar_index < length-1` 返回 `na`（非逐步 SMA），`bar_index == length-1` 写入 SMA 种子，之后 Wilder 递推；严格复现 Pine v5 `ta.rma`
  - SMC Pine 对齐首个 pivot off-by-one：`start_of_new_leg`/`start_of_bearish_leg`/`start_of_bullish_leg` 从 `i > size` 改为 `i >= size`；`get_current_structure` 从 `if i <= size: return` 改为 `if i < size: return`；首个 leg/pivot 在 `i == size` 检测（对齐 Pine `ta.change(leg)`）
  - EQH/EQL DTO 三时间点：EQL 和 EQH 两处新增 `detection_index`/`detection_time`（leg change 确认 bar, i），与 anchor（前一 pivot bar）/confirmed（新 pivot bar, ref_i=i-size）分离
  - 核对通过：ATR200=`pine_rma(tr,200)`、highest/lowest 窗口 `[ref_i+1, ref_i+length+1]`、crossover/crossunder NaN→False、OB slice `[start:end)` end-exclusive、trailing 顺序 `update_trailing_extremes → swing → internal → equal → display → delete OB`
  - 测试：`test_smc_indicator.py` 的 `test_pine_rma_min_periods_before_seed` 更新为断言 NaN；`miniKlineCardContract.test.ts` 新增 5 项闭包契约测试（16-20）
  - 遗留：Pine golden fixture PENDING；生产 E2E 验证待部署后执行

- CHANGE-20260715-005: 详情左栏来源状态拆分 + 表格 sticky 列和工具栏对齐根治
  - 详情左栏四态拆分：`useStockDetailActions.ts` 新增 `sourceListError`/`sourceListEmpty`/`sourceContextInvalid` 字段；`source` 参数优先级：显式 source > returnTo 推断；source=selection → sourceListKind=market（即使 returnTo 无效也不回退 watchlist）
  - `normalizeInternalReturnTo` 上限 500 → 4096（复杂筛选 URL 编码后可能超过 500）
  - 表格结构 `table-wrap` → `table-shell > meta-bar + search-bar + table-scroll > table + pager`：只有 `table-scroll` 设置 `overflow-x: auto`；meta-bar/search-bar/pager 移出横向滚动容器；删除 `position:sticky;left:0;width:100%` 补丁
  - `isStickyColumn(col)` 统一判断函数：只允许 `col.key === 'stock'` 为 sticky 列；header 和 body 共用；删除死 CSS `.sticky-col-change-pct`
  - `AdminAfterClosePipelinePage` 迁移到 `table-shell` + `table-scroll` 结构
  - 测试：`detailSourceLoadingContract.test.ts`、`marketWorkspaceUrlState.test.ts`、`stickyHeader.test.ts` 更新
  - 遗留：8 项 baseline contract 失败需独立修复；生产 E2E 验证待部署后执行

- CHANGE-20260715-004: Bug 1 修复（详情左栏 loading 占位）+ Pine 真源文件入 Git 跟踪
  - Bug 1 修复（详情左栏空白后才出现列表）：`useStockDetailActions` 新增 `sourceListLoading: boolean` 字段（`hasMarketContext` 时为 `publishedRunsQuery.isLoading || !activeRunId || sourceResultsQuery.isLoading`，否则为 `monitorStatusQuery.isLoading`）；`StockDetailPage` 新增 loading 占位渲染分支（`<aside data-testid="detail-source-list-loading">` + `<div class="tv-source-list-placeholder">加载中…</div>`）；`global.scss` 新增 `.tv-source-list-placeholder` 样式
  - Pine 真源文件入 Git 跟踪：`git add -f ref/smc_user_source.pine`（SHA256 0bd3d2ad，843 行），`.gitignore` 仍排除 `ref/` 其他文件，仅此单文件例外
  - 9 项源码契约测试：`detailSourceLoadingContract.test.ts`（sourceListLoading 字段、loading 占位渲染、列表渲染条件排除 loading、header 显示、CSS 存在、handleNavigateToStock 显式传 source/strategy、URL 完整性、不使用 useMarketStocks、上一只/下一只保留 returnTo）
  - 测试：tsc✅ eslint✅ contract 358pass/8fail(baseline一致)✅ 4 docs checks✅
  - 遗留：Pine golden fixture PENDING；Bug 1 生产 E2E 验证待部署后执行

- CHANGE-20260715-003: SMC trailing 执行顺序修复 + Pine 真源文件命名 + 行情列表 sticky 修复 + 工具栏对齐 + MiniKlineCard 契约测试
  - SMC trailing 顺序修复：`smc_pine_core.py` 的 `_SMCPineState.run()` 中 `update_trailing_extremes` 移到循环体最前面（第1步），对齐 Pine lines 766-807 执行顺序（trailing 必须在 getCurrentStructure 之前）
  - Pine 真源文件命名：`ref/smc_ref.txt` → `ref/smc_user_source.pine`（SHA256 0bd3d2ad，843 行，内容相同）；`smc_pine_core.py` docstring + AGENTS clause 44 更新引用路径
  - Bug 2 修复（sticky 列横向滚动重叠）：`global.scss` 定义 CSS 变量 `--stock-col-width: 150px`/`--select-col-width: 40px`；`.sticky-col` 固定 width/min-width/max-width；内容溢出 `overflow: hidden` + `text-overflow: ellipsis` + `white-space: nowrap`
  - Bug 3 修复（工具栏横向滚动消失）：`.table-meta-bar` + `.table-pager` 添加 `position: sticky; left: 0; width: 100%; z-index: 6`，横向滚动时保持可见
  - MiniKlineCard 契约测试：新增 `miniKlineCardContract.test.ts`（15 项源码契约测试，验证无 fitContent、setVisibleLogicalRange、autoscaleInfoProvider、ResizeObserver、requestAnimationFrame、五周期按钮、A 股配色等）
  - Parity 文档：新增 `docs/analysis/smc-user-pine-parity.md`（674 行，14 章节，逐项 Pine→Python 对照）
  - 测试：ruff✅ mypy✅ pytest 109pass/1skip✅ tsc✅ eslint✅ contract 349pass/8fail(baseline一致)✅ 4 docs checks✅
  - 遗留：Bug 1（详情左栏变自选）未修复；Pine golden fixture PENDING

- CHANGE-20260715-001: SMC 智能资金指标（ref/smc.py 重写版）+ MiniKline viewport P0 修复 + 图层 7→8
  - **SUPERSEDED BY CHANGE-20260715-002**：用户已提供本人原创并授权的 Pine 实现（`ref/smc_ref.txt`），SMC 算法真源从 ref/smc.py 升级为用户 Pine 代码；本条目中"非 LuxAlgo Pine 翻译"和"clean-room 声明"已过时，仅作历史保留
  - SMC 模块：基于用户提供的 `ref/smc_ref.txt` Pine 源码（用户原创，SHA256 0bd3d2ad，授权盘迹商业项目使用），纯函数仅依赖 stdlib；默认参数与 ref 一致；BOS/CHoCH/internal OB/EQH/EQL/Strong-Weak High-Low；anchor/confirmed 因果契约；逐 bar 增量=全量；未来 bar 不修改已确认事件
  - FVG 完全排除：不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关；生产计算路径无 FVG 函数或状态；输出级别断言（keys/events/OB/EQH/EQL/params/state 6 项）
  - include_smc 按需计算：compute_all_indicators 新增 include_smc=False 默认参数；False 时跳过 SMC 计算（0 CPU）；True 时注入 smc 图层
  - 缓存隔离：ALGORITHM_VERSION v5→v6（旧缓存自动失效）；include_smc=True 追加 :smc 后缀；同 symbol 切换开关不返回旧缓存
  - API：/api/v1/instruments/{id}/indicators 新增 include_smc 查询参数（bool，默认 false）
  - 前端图层 7→8：ChartLayerKey 新增 'smc'；manifest 8 条目；smc 默认关闭（watchlist/selection 均 false）；旧 localStorage 迁移 smc=false；StrategyChart SMC Canvas 渲染
  - MiniKline viewport P0：纯函数 computeMiniKlineViewport 替代 fitContent；五周期 clamp（15m/60m 50-64，日线 48-58，周线 40-52，月线 30-40）；右侧 3 bar 留白；切周期不沿用旧 range；12 项测试
  - 测试：SMC 34 + 缓存 10 + 服务 38 + viewport 12 + manifest 15 = 109 项 PASS；ruff/mypy/tsc/eslint 0 error；contract 316 pass/8 fail（与 a9ac03c 基线对照 0 新增失败）；build 248 modules 0 error
  - 隔离边界：SMC 只进入 /stock 指标链；/market 右栏小 K 线不显示 SMC；Node/DSA/盘中监控/Capture 未修改；不新增表/migration/worker/依赖
  - 文档：AGENTS clause 43 + current 01/02/04/05 + maps + CHANGE-001 + CHANGELOG

- CHANGE-20260715-002: SMC Pine parity 核心 + MiniKline viewport 重写 + SMC renderer 对齐
  - smc_pine_core.py（新增 852 行）：唯一 Pine 语义核心；Pine 原语 `pine_rma`（Wilder RMA，SMA 播种+递推）、`pine_atr`=RMA(TR,200)、`pine_cumulative_mean_range`（`ta.cum(ta.tr)/bar_index`，bar0=NaN）、`pine_highest/lowest`、`pine_crossover/crossunder`；`_SMCPineState` 状态机完全按 Pine 执行顺序（swing→internal→equal→BOS/CHoCH→trailing→mitigation）；FVG 完全排除；anchor/confirmed 因果契约；events 使用 `internal: bool` 替代旧 `kind` 字段
  - smc_indicator.py（重构 957→117 行）：薄包装委托 `compute_smc_pine`；`_SMCState = _SMCPineState` 别名；签名不变
  - warmup 修复：1d timeframe 使用 `full_daily_bars`（DB 全量日线，≥500 warmup）；其他周期复用 macd_bars；不调用 `_truncate_lists` 截断 SMC 输出（time 数组需完整长度对齐 anchor/confirmed）；前端 `smcToDisplay` 按时间过滤展示区事件
  - 缓存隔离：ALGORITHM_VERSION v6→v7（旧 v6 SMA 缓存强制失效）；`:smc` 后缀不变
  - StrategyChart SMC renderer 对齐 Pine：SmcEvent `kind?`→`internal?: boolean`；BOS/CHoCH 线型按 scope（internal=虚线 [4,3]/tiny 8px，swing=实线/small 11px）；标签中点 `(x1+x2)/2`+`'center'`；trailing 文案"强高/弱高/强低/弱低"（强高 if bias=-1 else 弱高；强低 if bias=1 else 弱低）；OB 半透明 box（active alpha 0.12，mitigated 0.05）；Historical 全事件
  - MiniKline viewport 彻底重写：目标根数 15m=48/60m=44/日=40/周=36/月=30；barSpacing clamp 5.5–8px；左侧 1-2 根留白 `from=max(-2,n-visible-1)`；右侧 3 根留白 `to=n-1+3`；不调用 fitContent/resetTimeScale/scrollToRealTime；`autoscaleInfoProvider` 扩展价格范围（上 12% 下 15%）；`rightPriceScale` autoScale=true + scaleMargins {0.08,0.08} + minimumWidth=56；图表高度 190px；切周期不复用旧 logical range；15 项纯函数测试
  - Pine golden fixture：状态 PENDING（等待 TradingView 导出）；新建 `backend/tests/fixtures/smc_pine/README.md`（TV 导出步骤+隐藏 plot 代码+CSV 格式）；无 fixture 时 `TestPineGoldenFixture` skip，不得宣称"完全对齐"
  - Pine 语义测试：`TestPineSemantics` 8 项（RMA Wilder 递推、RMA min_periods、CMR bar0=NaN、ATR=RMA(TR)、crossover、crossunder、highest、lowest）；`test_event_kind_valid`→`test_event_internal_field_valid`
  - 隔离边界：SMC 仅进入 /stock 指标链；/market 右栏不请求 SMC；true/false 缓存键隔离；不新增表/migration/worker/依赖；Node/DSA/监控/Capture/published run 未修改
  - 文档：AGENTS clause 44 + current 01/02/04/05/code-doc-alignment + maps + analysis/smc-pine-parity.md + CHANGE-002 + CHANGELOG

## 2026-07-14

- CHANGE-20260714-001: 最新行情涨跌幅与 DSA 日期分离 + preset=none + 股票名称筛选 + 详情左栏滚动 + 五周期小 K 线 + pywencai 探测 + SMC 撤回
  - latest_change_pct：bars_daily window function（lag+row_number）取最新两根日线计算，与 DSA run payload 分离；API 新增 latest_change_pct/latest_change_trade_date；服务端排序/筛选/Excel/详情左栏统一使用 latest 字段；9 项 pytest（T-1/T/停牌/null/prev_close=0/红涨绿跌/sort/filter/N+1）
  - preset=none：清除筛选时 URL 写 preset=none，默认 effect 跳过，returnTo 保留，不清列显示/顺序/pageSize
  - 股票名称筛选：stock_name/stock_name_op 独立于 keyword（contains/not_contains/eq），统一转义 ILIKE
  - 详情左栏：SourceStockItem 含 changePct，DSA 来源用 latestChangePct，自选 fallback 用 monitor-status 聚合，scrollTop sessionStorage 恢复
  - AdminAppShell：桌面侧栏+小屏 topbar 始终可见"← 返回行情"
  - 五周期小 K 线：15m/60m/日/周/月，segmented control，attributionLogo=false，修复外框裁切（.mini-kline-card + .mini-kline-chart 添加 width:100%;max-width:100%;min-width:0;box-sizing:border-box;overflow:hidden）
  - pywencai 探测：0.13.1 import 成功，5 项查询全部失败（iwencai.com 返回 HTTP 401 + captcha_url，与 THS 反爬同源），不可用，未加入生产依赖
  - SMC 撤回（已 SUPERSEDED BY CHANGE-20260715-002）：用户已提供本人原创并授权的 Pine 实现（`ref/smc_ref.txt`），SMC 不再涉及第三方许可证问题；本记录中 SMC 撤回相关的许可证和 clean-room 结论仅作历史保留
  - 基线对照：contract suite 8 个失败在 149eb09 基线和当前状态完全一致，0 新增失败
  - 文档：AGENTS clause 42 + CHANGE-001 + CHANGELOG

## 2026-07-13

- CHANGE-20260713-010: 个股详情市值 + 列表 Excel 导出 + 右栏小 K 线 + 股票名称筛选 alias + 文档修正
  - 市值：pytdx get_finance_info → 每日 18:00 同步到 DB → quote 端点从 DB 读取计算；migration 063 新增 total_share/float_share/share_as_of
  - Excel 导出：POST /strategy-runs/{run_id}/results/export + 标准库 OOXML + MAX_EXPORT_ROWS=10000 + 公式注入防护；9 单元 + 21 集成 + 14 share_capital = 44 项目标 pytest
  - 小 K 线：MiniKlineCard（lightweight-charts v4）+ useMiniKlineData（1d=80/1w=60/1mo=48）+ MarketRightPanel；收起 0 请求，只请求活动周期
  - filterAlias：stock 列 filterAlias='keyword' 与顶部搜索共用唯一真源；双向同步（onApply/onClear + URL + preset）；stock/action 不入 metric_filters
  - 文档修正：source_total/universe_total/filtered_total/items 四层语义；删除"source_total 受 universe 影响"旧描述；AGENTS clause 25 preset 增加 industry/concept
  - 契约测试：change010Contract.test.ts 49 个 + 全仓 contract 319 个通过；mypy 基线 0 项新增 0 项；84 项无截图 E2E PASS
  - 文档：AGENTS clause 41 + current 02/04 + maps + CHANGE-005/006 修正 + CHANGE-010

- CHANGE-20260713-009: 详情页来源上下文修复（P0）
  - MarketWorkspacePage 根据 scope 传 source/strategy；returnTo 保存完整当前 URL
  - 新增共享纯函数 decodeMarketListContext/buildStrategyResultQueryParams（任意合法 /market URL 都识别为 market context）
  - useStockDetailActions 从 useMarketStocks 切换到 DSA published results 链
  - sourceBadge/左栏标题：scope=market→"行情来源"，scope=watchlist→"自选来源"
  - normalizeInternalReturnTo 长度限制 200→500（/market URL 含 filters JSON 编码后可能超过 200）
  - 新增 7 项契约测试；tsc/eslint/contract/build/docs checks 全通过
  - 文档：AGENTS 规则 40 + maps + current 04 + CHANGE

- CHANGE-20260713-008: K线右侧 18-22% 留白 + 交互坐标同步
  - StrategyChart 引入 RIGHT_PADDING_RATIO=0.20，step 使用 effectivePlotW，所有交互坐标自动同步；网格线/十字线保持全宽；不修改 Node/Profile/POC 算法
  - 新增 chartRightPadding.test.ts 7 项契约测试
  - 文档：04-frontend-ux/05-testing/test-coverage-map 更新

- CHANGE-20260713-007: 管理员后台入口 P0 修复 + 行业/概念 preset 集成 + 数量契约修正 + 批准 Logo PNG + 视觉 V1.0 残留清理
  - 管理员入口：getAccountMenuItemsForVariant variant='user' + is_admin 显示"管理后台"；AdminRoute accessLoading 防止刷新误判；普通用户 DOM 不渲染入口
  - 行业/概念 preset：StrategyDataTable/TablePresetMenu 恢复+保存 industry/concept；MarketWorkspacePage boardsValidation + stale toast
  - 数量契约：source_total/universe_total/filtered_total/items 四层语义文档修正+测试
  - 批准 Logo：BrandLogo 改用 PNG 资产，停用手绘 SVG
  - 视觉残留：LandingPage/BetaApplicationModal/global.scss 旧蓝色改为品牌绿
  - 新增 brandLogo.test.ts/visualTokens.test.ts + 后端 total 语义测试
  - 文档：AGENTS/current 00/02/04/05/code-doc-alignment/maps 更新

- CHANGE-20260713-006: /market 行业/概念筛选恢复 + 盘迹品牌视觉 V1.0
  - 行业/概念筛选恢复：新建 board_filter_helper 共享 EXISTS 条件构造器（market_stocks_service + strategy_result_repository 共用），/strategy-runs/{run_id}/results 增加 industry/concept Query 参数，AND 语义，items/filtered_total 一致（source_total 不受筛选影响）
  - preset 持久化：TableViewPresetConfig（后端 Pydantic + 前端 TS）增加 industry/concept 可选字段，白名单同步，不新增表/migration，旧 preset 兼容
  - 前端 URL state：marketWorkspaceUrlState 增加 industry/concept，MarketToolbar 恢复"搜索、行业、概念"同一行布局，boards.available=false 时输入禁用+提示
  - StrategyDataTable 受控 props：externalIndustry/onIndustryChange/externalConcept/onConceptChange，currentConfig/applyPresetConfig 集成
  - 盘迹品牌视觉 V1.0：variables.scss 更新为莹感绿 token 体系（#00F6C2 主色，#0A0F14 背景，#F2F6F8 文本，红涨绿跌不变），BrandLogo 重写为四节点折线+末端高亮共识节点
  - 硬编码清理：global.scss 30 处 + MarketWorkspace.module.scss 71 处 + AccountMenu/UserAppShell/MarketInstrumentPane 替换；新增 variables 导入
  - 品牌资产：复制 logo_symbol_128/256.png + logo_horizontal_dark.png 到 frontend/src/assets/brand/（共 80KB）
  - 测试：后端 6 industry/concept 测试 + 前端 tsc 0 错误；Ruff 仅 B011 基线
  - 文档：AGENTS 规则 34-35 + docs/current/00/02/04/05 + maps + code-doc-alignment + CHANGE
- CHANGE-20260713-005: PR #74 阶段五 — DSA 列表产品回归 / 消息数量 SSOT / K 线 Pointer 拖拽 / 用户文案
  - /market 列表：action 列改名"自选"（加入/移除自选按钮），股票名称改为可点击链接进入 /stock/:symbol?returnTo=，股票单元格不再显示行内涨跌幅（独立 change_pct 列保留），watchlist 单次请求+Set 禁止 N+1，批次信息 admin-only 默认折叠
  - /market 单一搜索 SSOT：MarketToolbar 顶部唯一搜索框（Enter/blur/清空提交），StrategyDataTable searchable={false} + externalKeyword/onKeywordChange 受控模式
  - 后端 keyword pinyin_initials 匹配：strategy_result_repository 3 处 or_ 分支同步匹配 symbol/name/pinyin_initials
  - 消息数量 SSOT：useUnreadCount 作为未读权威，"全部"显示后端 total（非 items.length），页头"共 X 条 · 未读 Y 条"，AccountMenu unread>0 进入 /messages?filter=unread + badge
  - 消息跳转：单只股票 → /stock/:symbol?event_id=...&returnTo=/messages，selection_composite → /market
  - K 线 Pointer Events 拖拽：setPointerCapture/releasePointerCapture，dragRef {startClientX,startViewport,pointerId}，4px 阈值抑制 click，grab/grabbing cursor
  - 用户文案：sqzmom→"挤压动量"，node→"筹码共识价"，POC 峰→"核心共识价"，峰→"共识价"，缺失提示→"筹码共识价暂不可用"；内部字段名不变
  - 测试：前端 263 contract（columns 13 + chartLabels 5 + chartDrag 7 + marketToolbarSearch 8 + messagesCounts 8 + indicatorManifest 12）+ 后端 3 keyword + 4 universe；tsc/eslint/build/docs checks 全通过
  - 文档：AGENTS 规则 27-33 + docs/current/02/04/05 + code-doc-alignment + maps + CHANGE
  - Ruff 基线 1 项（B011 既有债务），本轮 0 新增
  - Node/monitor/capture 未改动
- CHANGE-20260713-004: PR #74 阶段四 — /market DSA 列表恢复 + 列对齐修复 + 列设置 CRUD + columnOrder
  - DSA 列表恢复：MarketWorkspacePage 改用 StrategyDataTable + getTrendSelectionColumns，数据来自 usePublishedRuns + useStrategyRunResults
  - P0 列对齐修复：reorderVisibleColumns 纯函数提取到 columnOrdering.ts；thead th/tbody td/colgroup col 三者同源
  - 列设置 CRUD：columnOrder 支持（localStorage + preset config）；TableViewPresetConfig 白名单新增 columnOrder
  - URL 状态简化：/market 契约简化为 scope/selected + StrategyDataTable 内置 screenerUrlState
  - MarketStockTable 删除；MarketToolbar 简化为仅 scope 分段按钮
  - Node Cluster 边界保护：未修改 indicator_contract/indicator_service/monitor_batch_service/volume_node_monitor/watchlist_monitor/capture
  - 测试：后端 50 preset + 前端 220 contract（含 31 columnAlignment + 12 marketWorkspaceUrlState）+ docs checks 全通过
  - 文档：AGENTS 规则 14 全面更新（8/11/12/13/18/22/23 + 新增 24/25/26）+ 04-frontend-ux + code-doc-alignment + maps
- CHANGE-20260713-003: PR #74 阶段三 — 行情列表简化 + 图层/因子语义纠正 + 右栏按需加载
  - boards 单一真源：MarketWorkspacePage 唯一调用 useMarketBoards，向下传 props
  - 删除最近事件列：market_stocks_service 删除 stock_state_event 批量查询（SQL 9→8），字段兼容保留 null
  - 形态状态中文映射：mapStructureStateLabel/mapDsaStateLabel 纯函数 + 数据日期提示
  - 删除 tv-strategy-legend：StrategyChart JSX + isGroupActive + DISPLAY_GROUPS/DisplayGroupDef + CSS
  - MACD 语义纠正：feature_snapshot 附加日线辅助指标，watchlist/selection 默认关闭
  - 右栏默认收起：/market + /stock 首次默认收起，localStorage 持久化
  - AGENTS.md 规则 8/11/16/17/18 重写 + 新增 19/20/21/22/23
- CHANGE-20260713-002: PR #74 阶段二 — StockContext reasonCode + 快照归属修复工具 + EventStatePanel 纯函数抽取
  - reasonCode 机制：StockContext API 返回 dataQuality.reasonCode（no_published_full_run/snapshot_missing/snapshot_run_not_linked/legacy_snapshot_ambiguous/null）+ runTradeDate/runPublishedAt/hasSucceededRun/hasSnapshot/degradedReasons
  - 快照归属修复工具：tools/repair_snapshot_run_ownership.py（dry-run + --apply，按 trade_date+schema_version+timeframe+adj 匹配 canonical succeeded+published+full run，幂等 UPDATE source_run_id）
  - EventStatePanel 纯函数抽取：reasonCodeMessages.ts（纯 TypeScript，可测试）
  - GET context 只读：无写副作用
  - 测试：9 项 required 测试全通过（6 API + 2 repair + 1 前端）
  - 生产修复：dry-run 21172 repairable / 100 orphan / 0 ambiguous；--apply 写入 21172 条
  - 生产验证：10 自选股 + 10 市场股 + 1 异常股 = 21/21 state non-null
- CHANGE-20260713-001: PR #74 补充修复 — ConsensusZone 移除 + 图层单一状态源 + K 线 viewport 修复 + Published snapshot 保护
  - ConsensusZone 移除：删除 consensus_zone_service.py / consensus_zone.py / test_consensus_zone.py；Phase 5 前成交量分布保持禁用
  - 图层单一状态源：ChartLayerVisibility 类型（7 键 trend/node/boll/volume/macd/sqzmom/breakout）；localStorage key panji:chart-layer-visibility:v2；删除 indicatorVisibility/detail-chart-strategy-groups/setLayers 旧状态源
  - K 线 viewport 修复（P0-5）：删除 makeDefaultViewport；viewport 复合 key ${symbol}:${timeframe}；auto-follow effect；初始 toIndex === calc.length 定位到最新 K 线
  - Published snapshot 保护（P0-4）：PublishedSnapshotRunExistsError + get_published_full_run + upsert_snapshot WHERE 子句 + after_close 优雅降级 + backfill --allow-republish 标志
  - 测试：后端 121 passed + 前端 177 contract passed + tsc/build/docs consistency 全通过
  - 文档：02-data-api-contracts/03-jobs/04-frontend-ux/05-testing/test-coverage-map 更新

## 2026-07-12

- CHANGE-20260712-003: PR #74 综合修复 — 快照归属/发布原子性/stock_context/前端修复
  - 快照归属：upsert_snapshot 冲突更新 source_run_id；删除 ORM 冗余索引 ix_feature_snapshot_source_run_id
  - 发布原子性：快照计算后不提前写 succeeded/published_at；DSA publish_run 成功后才发布 snapshot run
  - stock_context：_event_to_dto 映射 evidence DTO；ZoneInfo("Asia/Shanghai")；cutoff 次日 00:00 exclusive；run 查询确定性倒序
  - 幂等键：改为 symbol:source_run_id:algorithm_version（每股票每 run 最多一条事件）
  - 前端：UserAppShell 删除角色预览；StockDetailPage eventPanelCollapsed 默认展开；EventStatePanel 展示 evidence+MACD；MarketStockTable 名称 sticky+字号修正+来源徽章修复
  - 部署：CORE_ONLY 模式不构建 worker-capture
  - 测试：后端 87 测试 + 前端 141 契约测试全部通过
- CHANGE-20260712-002: C10 收口 — 板块同步降级保护(BOARD_SYNC_ENABLED) + /market/boards available/reason_code + 前端筛选降级 + 废弃CSS清理 + 文档同步
- CHANGE-20260712-001: PR #74 两项架构纠偏 — board_sync 合并进 bars_scheduler + ConsensusZone 数据源修正
  - arch1: `worker-board-sync` Docker 服务移除；`run_board_sync_scheduler_worker()` 函数删除；`board_sync_scheduler` 不再是有效 WORKER_TYPE；board_sync job 注册进 `run_bars_scheduler_worker()`（同一 AsyncIOScheduler，17:00 CronTrigger，max_instances=1）；qstock 同步调用通过 `asyncio.to_thread()` 包装
  - arch2: `indicator_service` ConsensusZone 数据源从 `macd_bars` 改为 `daily_bars`（固定 250 根日线窗口）；`timeframe` 固定 `"1d"`；`as_of` 取最后一根日线 bar 时间；缓存键 `consensus_zone:{symbol}:{as_of}:1d:{algo_version}:{data_version}` 显示周期切换时稳定；V1 仅日线成交分布，15m 细化为未来工作
  - 测试：`test_board_sync_registered_in_bars_scheduler`、`test_board_sync_not_separate_worker_type`、`test_consensus_zone_independent_of_display_count`、`test_consensus_zone_independent_of_display_timeframe`
  - 文档：03-jobs、02-data-api-contracts、worker-job-map、deployment-runtime-map、test-coverage-map 更新

## 2026-07-11

- CHANGE-20260711-007: PRD V1.1 §7.4/7.5 — ConsensusZone 真实实现 + qstock 板块同步 + industry/concept 筛选
  - P2: ConsensusZone 真实算法（峰簇识别 + 谷底分簇 + 成交量加权 P10/P50/P90 + 因果性过滤 + Redis 版本化缓存），22 tests pass
  - P2: qstock 板块同步服务（暂存集合 + 完整性校验 + 事务原子切换 + 失败保留旧数据），11 tests pass
  - P2: migration 062 新增 market_boards + market_board_memberships 表（只存最新态）
  - P2: market API industry/concept 筛选接入（移除 422，通过 market_boards 表过滤）
  - P2: migration 061 冗余索引修复（删除单列索引，组合索引最左前缀覆盖）
  - P2: sourceField/idempotencyKey 字段完全排除（dict + 递归 pop，JSON 中字段消失不是 null）
  - 遗留：qstock 每日任务未接入 scheduler；ConsensusZone 前端图层未启用；生产未部署

- CHANGE-20260711-006: PRD V1.1 纠偏 — 路由恢复 + EventStatePanel + StockState 字段剥离 + state 筛选
  - P0: 恢复 `/stock/:symbol` 为 StockDetailPage（删除 StockDetailRedirect），新增 3 个路由契约测试
  - P0: source_run_id migration 061 审计完成（保留，upsert 不覆盖已发布快照来源）
  - P0: 权限测试改为 7 个真实 HTTP 集成测试（0 skip），44 tests pass
  - P1: EventStatePanel 替代 ResearchContextPanel，删除 12 个 orphan 文件
  - P1: StateValue.sourceField 和 StateEventDTO.idempotencyKey 改为 Optional，用户接口通过 strip_internal_fields_for_user 剥离
  - P1: state 筛选实现（up/down/sideways），industry/concept 保持 422（后在 CHANGE-007 中实现）
  - P1: DISTINCT ON 替换为标准 SQL 子查询 + JOIN
  - 28 market tests + 141 contract tests pass；ruff/mypy/tsc 无新增错误

- CHANGE-20260711-005: 统一行情工作区 P0/P1 收口修正
  - P0：`/admin/stock-debug` 独立管理员调试路由；`debug` 从 `/market` URL 契约移除；`ResearchContextPanel` 只渲染 4 张用户卡
  - P1：新建 `buildStructureSummary`/`buildUserEventExplanation` 纯函数（DTO 路径修正 + instrument mismatch 校验）；`normalizeInternalReturnTo` returnTo 安全校验
  - AGENTS §12.14 重写为长期规则（删除阶段名称/已删除文件引用/debug 矛盾）
  - docs/current/04/05 更新；code-doc-alignment 新增 ALIGN-040
  - 119 tests pass、tsc/eslint 0 errors、vite build PASS、4 docs checks PASS
  - 不改后端/API/DB/Worker

- CHANGE-20260711-004: 统一行情工作区原型最终对齐（阶段五）
  - ScreenerPage/MessagesPage 查看详情改进入 `/market`（含 returnTo/event_id）；`/market` URL 扩展 debug/returnTo
  - 新建 `features/research-context/`：ResearchContextPanel/EventExplanationCard/StructureSummaryCard/AdminFactorDebugPanel/useResearchContext
  - 普通用户看事件通俗解释和结构状态人类可读总结；管理员 `debug=1` 看原始 factor/feature/JSON
  - `StockStructuralStatePanel` 新增 debug props；按三张原型 PNG 重做 CSS（响应式+focus-visible）
  - 删除旧 WatchlistPage.tsx 和 IndexPage.tsx（死代码）及对应契约测试
  - 181 tests pass、tsc/eslint 0 errors、vite build PASS、4 docs checks PASS
  - 不改后端/API/DB/Worker/CaptureStockPage

- CHANGE-20260711-003: StockDetailPage 共享研究核心重构（阶段四）
  - `StockDetailPage` 降为路由适配器（813→453 行），复用 `useStockResearchData` + `StockResearchWorkspace`
  - 新建 `stockResearchTypes.ts` 共享类型；依赖方向修正为 market-workspace → stock-research
  - 新建 `useStockDetailActions.ts`（自选/上下切换/memo）+ `useStockDetailFeishu.ts`（截图轮询/超时/清理）
  - `StockResearchWorkspace` 新增 toolbar/rightPanel/chartColumnProps 可选 props
  - `quoteStatus`/`barsStatus` 统一：不显示"日线回退"，partial 含当前周期
  - 13 项纯函数测试 + 21 项回归测试 + 39 项 CDP E2E 全 PASS
  - 不改后端/API/DB/Worker/CaptureStockPage

- CHANGE-20260711-002: 统一行情工作区第一版（阶段三）
  - `/market` 渲染 `MarketWorkspacePage`（三栏：左列表+中K线+右结构状态可收起）
  - `useStockResearchData` 集中 bars/indicators/quote/events/memo 请求；`StockResearchWorkspace` 复用组件
  - URL 状态 `scope=watchlist|market&symbol=xxx&timeframe=1d`；切换股票不整页刷新
  - `detailNavigation` watchlist fallback 改为 `/market?scope=watchlist`
  - 图表 timeframe 仅展示，不改 1d+15m 监控配置或 1m 事件触发
  - 7 项 URL 状态测试 + 32 项总测试全通过；tsc/eslint 0 errors；vite build 通过
  - 不改后端/API/数据模型/Worker；不删除 IndexPage/ScreenerPage/CaptureStockPage

- CHANGE-20260711-001: 用户/管理员壳层与导航路由拆分（阶段二）
  - 普通用户主入口 `/market`（复用 WatchlistPage）；`/overview`→`/market`、`/watchlist`→`/market?scope=watchlist` 兼容重定向
  - `UserAppShell`（顶栏品牌+一级导航行情/趋势选股+账户菜单；无左侧栏）；`AdminAppShell`（独立管理导航+账户菜单）；`ProtectedLayout` 只返回 Outlet
  - `AccountMenu` 下拉（消息/设置/管理后台仅admin/退出）；`appNavigation.ts` 集中路由常量
  - 登录/续期/兜底/AdminRoute 重定向 `/overview`→`/market`；Capture 路由不变
  - 新增 6 项导航阻断测试；tsc 0 errors；eslint 0 errors；vite build 通过
  - 不改后端/API/数据模型/Worker；不删除 IndexPage/WatchlistPage/ScreenerPage/StockDetailPage

## 2026-07-10

- CHANGE-20260710-002: 恢复飞书盘中截图 1d 业务契约，分离截图实时性与监控计算口径
  - 修复 PR #65 业务语义偏差：飞书截图（手动分享 `stock_detail_feishu_service` + 自动盘中监控 `_send_chart_images_via_outbox`）capture_payload 由 15m 改为业务默认 1d（常量 `FEISHU_CAPTURE_TIMEFRAME`）
  - `monitor_batch_service` 计算输入 `bars_daily`/`bars_15min` 恢复 `include_realtime=False`，不被截图实时性污染；盘中监控触发仍只基于最新已完成 1m bar
  - Capture Snapshot API 多周期能力（15m 透传等）保留，明确“API 能力 ≠ 飞书业务默认 15m”
  - 不引入 DB migration、不重启 postgres/redis、不部署、不跑 research backfill
  - 测试：pytest 指定文件 38 passed、mypy 0 errors、ruff 改动文件 0 errors、`check_docs_consistency`/`check_architecture`/`check_test_allowlist`/`update_docs --check` 全 PASS
  - 纠正 docs/current/AGENTS.md/docs/maps 中把 15m 写成飞书业务默认/验收的错误

- CHANGE-20260710-001: 飞书盘中高清实时截图（高清 + 不复用旧图/旧指标 + K线标题股票名称）
  - 三件事：① 高清截图 viewport 1920×1200 + dsf=2（env，默认非 4）；② cache key 扩展 + disable_cache + force_refresh + 实时 source_bar_time，不复用旧图/旧指标；③ K线主标题显示 `名称（代码）`
  - 修改 `stock_capture_service` / `capture_main` / `capture.py` / `monitor_snapshot_service` / `indicators.py` / `stock_detail_feishu_service` / `notification_service` / `monitor_batch_service` + 前端 `CaptureStockPage` / `StockDetailPage` / `StrategyChart` / `endpoints`
  - Capture Snapshot 端点 `include_realtime=True` + 周期透传；`monitor_batch` daily/15m `include_realtime=True`
  - 不引入 DB migration、不重启 postgres/redis、仅单次飞书实测
  - 更新 `docs/current/02/03/04/05`
  - 测试：pytest 173 passed、mypy 0 errors、ruff 改动文件 0 errors、前端 build 通过
  - Follow-up（第二轮，阻断修复，仅修阻断不 merge/部署）：`capture.py` 的 `get_bars`/`_df_to_responses`/`compute_all_indicators` 此前回退 `_CAPTURE_TIMEFRAME` 且 `include_realtime=False`，截图仍是 1d 非实时；修复为透传 URL `timeframe` + `include_realtime=True`，新增 15m 透传阻断测试；前端 `CaptureStockPage` 实时状态改从 `snapshot.bars.last_live_bar_time` 读取，`endpoints.ts` 删除 `CaptureSnapshotResponse` 顶层 `last_live_bar_time` 并补 `BarListResponse` 对应字段

## 2026-07-09

- CHANGE-20260709-011: tests mypy 债务清零
  - `backend/tests/` mypy 300 errors → 0 errors（133 source files），`mypy app` 仍 0 errors（249 source files）
  - conftest.py 新增 `AsyncFactory[T]` 类型别名 + `make_asgi_transport(app)` helper（桥接 httpx/Starlette 第三方存根缺口）
  - 54 个测试文件：异步工厂 fixture 改用 `AsyncFactory[T]`、Optional 分支显式 `assert x is not None` 收窄、mock 改用真实 ORM/Protocol
  - `app/services/access_control_service.py`：`require_feature`/`require_quota` 返回类型收紧为 `Coroutine`（类型-only，无运行时行为变化）
  - `app/strategy/runtime.py`：`execute` 返回类型 `StrategyResult` → `StrategyResult | None`（诚实 typing，batch service 已处理 None）
  - 1 处 `cast`（make_asgi_transport 第三方存根缺口，集中单点），无 `type: ignore` / `Any` 掩盖
  - 不构建/部署/重启服务、不跑 coverage
  - 更新 `docs/current/05-testing-acceptance.md`（§5.1.4 tests mypy 清零规则）、`docs/current/03-jobs-integrations-operations.md`（§12 tests mypy 债务治理）

- CHANGE-20260709-010: ruff strategy_assets C408/N806 债务清零
  - ruff baseline total 889→274，删除 615 个 C408/N806 条目（440 C408 + 174 N806 + 1 N806）
  - C408 全部 `ruff --fix --unsafe-fixes` 自动修复（`dict()` → `{}`）
  - N806：`instrument_seed.py` `BATCH_SIZE`→`batch_size` 重命名；3 个大量算法变量文件 per-file `# ruff: noqa: N806`；4 个少量算法变量文件 inline `# noqa: N806`，均注释 "kept to match upstream algorithm naming"
  - 不改变算法输出、默认参数、返回结构；不构建/部署/重启服务
  - 更新 `docs/current/05-testing-acceptance.md`（§5.1.3 Ruff baseline 规则）、`docs/current/03-jobs-integrations-operations.md`（§11 Ruff baseline 债务治理）

- CHANGE-20260709-009: mypy baseline 全量清零
  - 剩余 186 个 baseline 错误（141 unique）全部清零
  - Batch A: 小生产文件 19 个（config, pytdx_adapter, chart_bars, calendar_seed 等）
  - Batch B: repositories + services + worker 61 个（bar_repository, system_overview, worker 等）
  - Batch C: models SQLAlchemy metadata 31 个（新增 _table_meta.py helper）
  - Batch D: metrics/plotly_mock/bars_metrics/bars_scheduler 31 个
  - Batch E: strategy/strategy_assets 34 个（含 indicator_contract Literal 修复）
  - 新增 `app/models/_table_meta.py`、修改 `app/constants/indicator_contract.py`
  - `tools/quality_baselines/mypy.json` total=0, unique=0, diagnostics=[]
  - 新增 §5.1.2 禁止新增 baseline 规则

- CHANGE-20260709-008: API 路由 BaseRoute.path mypy baseline 债务清零
  - `app/api/*` + `capture_main.py` 的 20 个 BaseRoute.path attr-defined 错误降为 0
  - 新增 `app/core/route_utils.py`：`iter_api_routes` / `get_route_paths` 类型收窄 helper
  - 15 个文件中的 `[r.path for r in router.routes]` 替换为 `get_route_paths(router.routes)`
  - 不使用 type:ignore/cast；不改变 API 行为
  - 更新 `tools/quality_baselines/mypy.json`（total 206→186, unique 156→141）
  - 更新 `docs/current/03-jobs-integrations-operations.md`（§9 API 路由治理、§10 工具通道规则）、`docs/current/05-testing-acceptance.md`（§5.1 验收规则、§5.1.1 路线图）

- CHANGE-20260709-007: after_close_orchestrator mypy baseline 债务清零
  - `after_close_orchestrator.py` 的 22 个 mypy baseline 错误降为 0
  - 新增 `_get_job_run_or_raise` / `_get_strategy_run_or_raise` 类型收窄 helper，替换所有裸 `db.get` 调用
  - `_get_or_create_job_run` 在 `is_new=True` 后显式校验 `job_run is not None`
  - 不改变状态机语义、不增减异常类型、不使用 cast/type:ignore
  - 更新 `tools/quality_baselines/mypy.json`（total 228→206, unique 163→156）
  - 更新 `docs/current/03-jobs-integrations-operations.md`（§8 类型债务治理）、`docs/current/05-testing-acceptance.md`（§5.1 债务治理验收规则）

- CHANGE-20260709-006: after_close feature_snapshot 心跳保活 + stuck running snapshot run 修复
  - 修复 `feature_snapshot` 阶段无独立心跳导致 orchestrator 被误标 `interrupted`，但 `stock_feature_snapshot_run` 仍卡在 `running` 的问题
  - `feature_snapshot_service.compute_for_trade_date` 新增 `progress_callback`，每 batch 汇报进度；`after_close_orchestrator` 在 feature_snapshot 阶段启动 `_job_run_heartbeat_loop` 并写入 `metadata.feature_snapshot_progress`
  - 新增 `repair_stale_after_close_snapshot_runs`：orchestrator `interrupted`/`failed` 且 snapshot run 超时后，按实际 snapshot 行数 / `expected_count` 比例（≥95% 标 `succeeded`，否则标 `failed`）收口 running run
  - `execute_after_close_run` 启动前自动调用 repair，避免 stuck running snapshot_run 阻塞新任务
  - `/admin/after-close/pipeline` 在 `interrupted + snapshot_run running` 时第 6 步显示 `running`，返回 `feature_snapshot_lost_contact=true`，摘要暴露 `feature_snapshot_run_id` 与 `feature_snapshot_progress`
  - 更新 `docs/current/03-jobs-integrations-operations.md`（§2.3 heartbeat 机制、§2.3.2 修复 runbook、§2.3.3 盘后任务优先级）、`docs/current/05-testing-acceptance.md`（§3.6/§3.6.1 回归用例）
  - 新增/更新测试：`test_after_close_orchestrator.py`（heartbeat/progress/repair）、`test_admin_after_close_pipeline.py`（中断后 UI）、`test_feature_snapshot_service.py`（progress callback）
  - 无 migration、不删库卷镜像、不生成 coverage/截图/DB 备份；生产修复通过 repair + 重跑 after_close 完成

- CHANGE-20260709-005: 飞书配置权限隔离 + 趋势选股返回状态恢复
  - 修复 `SettingsPage` 普通用户可见 admin-only「发送最近事件实测」按钮的问题：引入 `useAuthStore` 按 `user?.is_admin` 渲染，普通用户隐藏该按钮，admin 显示文案改为「管理员实测最近事件」
  - 所有飞书渠道卡片增加「发送测试消息」/「测试并启用」按钮，调用 `POST /notification-channels/{id}/test`；测试成功后 toast「测试成功，飞书渠道已启用」并刷新渠道列表；失败展示 `delivery.error_code` / `error_message`
  - 飞书配置表单 `receive_id_type` 下拉补充 `chat_id`/`union_id`，帮助文案区分个人/群；「验证并保存」改为「保存配置」，保存后状态仍为 `pending`
  - 后端 `POST /notification-channels/{channel_id}/test-latest-event` 保持 admin-only，普通用户调用返回 403 detail「最近事件实测仅管理员可用，普通用户请使用发送测试消息」
  - 趋势选股页 URL 状态持久化：`ScreenerPage` 同步 strategy key，`StrategyDataTable` 抽出 `screenerUrlState.ts` 纯函数同步 keyword/sort/filters/page/pageSize；filters 用 compact JSON 只保存 key/op/value/value2；decode 丢弃当前 columns 中不存在的陈旧 key；切换策略时重置 page=1
  - `ScreenerPage.goDetail` 将当前 `location.pathname + location.search` 作为 `returnTo` 通过 `navigate(..., { state: { returnTo } })` 传入个股详情；`StockDetailPage` 返回按钮优先使用 `location.state.returnTo`，没有时按 source fallback 到 `/screener` 或 `/watchlist`
  - 新增 `frontend/src/pages/detailNavigation.ts` 纯函数抽离 URL/state 构建与返回路径解析
  - 新增前端 node tests：`settingsFeishuActions.test.ts`（5 用例）、`screenerUrlState.test.ts`（5 用例）、`detailNavigation.test.ts`（3 用例）
  - 文档更新：`docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`，`docs/maps/api-route-map.md`、`frontend-route-map.md`、`test-coverage-map.md`
  - 无 migration、无数据回补、无新依赖

- CHANGE-20260709-004: PR #52 真实端到端问题 hotfix（preset 持久化 + sticky 表头）
  - 修复 `backend/app/api/me_table_view_presets.py` create/update/delete 只 `flush()` 不 `commit()` 导致生产 preset 丢失的问题：POST/PATCH/DELETE 成功后均 `await db.commit()`，异常分支 `await db.rollback()` 后 re-raise
  - 新增 3 个跨 session 持久化测试：create/update/delete 后用独立 `TestAsyncSessionLocal` 验证真实持久化
  - 修复前端 `TablePresetMenu` 保存后列表不刷新：抽出 `tablePresetMenuLogic.ts::savePreset`，成功后 `presetsQuery.refetch()` 并清空输入框，失败时在下拉内显示后端 detail 并 toast
  - 新增 `frontend/src/components/__tests__/tablePresetMenu.test.ts`（4 用例）覆盖空名提示/成功刷新/失败显示 detail/默认错误文案
  - 修复趋势选股页 sticky 表头：`StrategyDataTable` 新增 `stickyHeaderMode="viewport"`，ScreenerPage 传入 viewport，`.table-wrap.viewport-sticky { overflow: visible }`，表头 `top: var(--topbar)`、`z-index: 18`
  - 新增 `frontend/src/components/__tests__/stickyHeader.test.ts`（4 用例）覆盖 prop/class/overflow/top/z-index 契约
  - 文档更新：`docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`，`docs/maps/frontend-route-map.md`、`api-route-map.md`、`test-coverage-map.md`
  - 无 migration、无数据回补、无新依赖

- CHANGE-20260709-002: 趋势选股批量加入修复 + change_pct 独立列 + 表格视图配置预设 + sticky 表头
  - 修复 `ScreenerPage.handleBatchAdd` 按 `r.resultId` 匹配导致 selected 永远为空的 bug：rowKey 是 `instrumentId`，selectedKeys 保存 instrumentId，handleBatchAdd 改用 `r.instrumentId` 匹配 + 去重；选中后无可加入股票 toast 提示而非静默；成功/失败 toast 真实反映数量；保留 `useAddToWatchlist` 缓存失效逻辑
  - 新增趋势选股表头"当日涨跌幅"独立列：key=`change_pct`、title=当日涨跌幅、shortTitle=涨跌幅、dataType=percent、sortable=true、filterable=true、width≈86，render 用 `fmtChange` + A股涨红跌绿（`changePctColorClass`）；后端 `dsa_selector.yaml` manifest 已支持 filterable/sortable（无需改后端白名单）；`change_pct` 已为百分比数值，筛选输入 3% 传 3 不乘除
  - 新增 `user_table_view_presets` 表 + `/me/table-view-presets` API（GET/POST/PATCH/DELETE），JWT user_id 隔离，权限与趋势选股一致（active subscription + trend_selection feature，admin 豁免），每 user+table_id+strategy_key 最多 20 个，config 只保存 keyword/sort/filters/hiddenColumns/pageSize（禁止 selectedKeys/page/activeRunId/rows），is_default 同维度互斥
  - 新增 `backend/app/models/table_view_preset.py`（UserTableViewPreset ORM）、`backend/app/schemas/table_view_preset.py`（Pydantic schemas，extra="forbid" 白名单校验）、`backend/app/api/me_table_view_presets.py`（4 个端点）、`backend/alembic/versions/059_user_table_view_presets.py`（migration 059）
  - 新增前端 `TablePresetMenu` 组件 + `StrategyDataTable` preset 集成（currentConfig/applyPresetConfig/默认 preset 自动应用 useRef 防重复）+ `useApi` 4 个 preset hooks + `endpoints` preset API 类型与函数；`tableId="screener"` + `strategyKey` 分离传递用于 preset 隔离
  - `global.scss` 补充 sticky 表头/选择列 z-index 层级：表头 z-index 4、sticky 列 z-index 3、角落单元格 z-index 5、选择列 sticky left:0、首列通过相邻兄弟选择器偏移 40px
  - 新增测试：后端 `test_table_view_presets_api.py`（**47 用例**：权限矩阵/CRUD/用户隔离/重名冲突/quota/非法 config/is_default 互斥/必填校验/user_id 注入/PATCH 空请求/迁移幂等/NULL strategy_key 唯一约束/config 深度校验）、前端 `columns.test.ts`（6 用例：change_pct 列）、`ScreenerPage.batch.test.ts`（6 用例：handleBatchAdd 修复）
  - 文档更新：04-frontend-ux（趋势选股页规则）、02-data-api-contracts（第 14 章 preset API 契约）、frontend-route-map、api-route-map、database-model-map、05-testing-acceptance（3.10 节回归门禁）、test-coverage-map
  - 未跑回补、未生成 coverage/html/screenshot/大日志、未增加磁盘占用；未删除受保护镜像 node:20-alpine
  - **Review Fix（PR #52 用户 review 反馈）**：
    - 唯一约束改为两个 partial unique index（解决 PostgreSQL NULL!=NULL 问题）：原普通 `UniqueConstraint(user_id, table_id, strategy_key, name)` 在 `strategy_key IS NULL` 时无法拦截重复；migration 059 改为 `uq_user_table_view_preset_strategy_not_null (user_id, table_id, strategy_key, name) WHERE strategy_key IS NOT NULL` + `uq_user_table_view_preset_strategy_null (user_id, table_id, name) WHERE strategy_key IS NULL`，model 同步用 `Index(..., postgresql_where=text(...))`
    - API IntegrityError 匹配更新为检查两个新索引名（create + update 两处）
    - config 深度校验加强：`_validate_config_keys` 补 filters 每项 dict + 含 key/op/value + op 白名单（contains/eq/gt/gte/lt/lte/between/empty/not_empty）、hiddenColumns 每项 string、sort.key 非空 string；`TableViewPresetConfig` 同时用 `model_validator(mode="after")` 双保险
    - 新增 10 个测试：4 个 NULL strategy_key 唯一约束场景（同 user+table_id+NULL+name 重复→409、不同 table_id 允许、不同 user 允许、PATCH 重命名冲突）+ 6 个 config 深度校验（filters 元素非 dict→422、缺 key→422、op="regex" 非白名单→422、9 个合法 op 全通过→201、hiddenColumns 含非 string→422、sort.key 空串→422）
    - 文档同步：CHANGE-20260709-002 补 partial unique index + config 深度校验描述 + 47 用例；02-data-api-contracts、database-model-map、test-coverage-map、05-testing-acceptance 同步

## 2026-07-09

- CHANGE-20260709-003: research matrix Phase 1 回补完成 + DSA direction 类型 hotfix
  - PR #51 squash merge commit=59d2ae7；migration 058 已在生产应用
  - 前台 A/B/C/D 验证全部通过：D full Jan rows=102603，failed_rate=2.90%，表大小 38MB
  - 后台 E 阶段串行完成 2026-02 到 2026-07 逐月回补，全量 7 个月共写入 621,769 行
  - 覆盖日期 2026-01-05 到 2026-07-08（122 个交易日），表总大小 223MB
  - 全部 9 个 run status=succeeded，failed_rate 最高 4.11%（2026-02）
  - 发现并修复 DSA direction 类型 bug（float→str）：PR #53 / commit=3c22a22
  - backend 镜像重建并重启：market-dev-backend:3c22a22
  - hindsight / Node Cluster 列全 NULL，符合 Phase 1 约束
  - 无 parquet/CSV/export/DB 备份，仅新增 research_feature_matrix_* 两张表
  - 临时 lock file / pid 文件已清理

- CHANGE-20260709-001: research feature matrix DB 主存储 + compute + writer + CLI + 5 个 Blocker 修复
  - registry 从 27 扩展到 33 字段（causal 16 + confirmed_delay 4 + hindsight 6 + label 7），新增 `FeatureSpec.db_column` 把 dotted key 映射为下划线列名（`causal.atr` → `causal_atr`）
  - 新增 `backend/app/models/research_feature_matrix.py`：`ResearchFeatureMatrixRun`（16 列，`run_key` 唯一）+ `ResearchFeatureMatrixRow`（39 列扁平宽表，`(instrument_id, trade_date)` 唯一）ORM
  - 新增 `backend/alembic/versions/058_research_feature_matrix.py`：创建两张表 + 索引（**本 PR 不应用 migration**，留到 PR merge 后）
  - 新增 `backend/app/research/feature_computer.py`：`compute_all_features(bars)` per-bar full series，复用 ATR/BB/SQZMOM/swing/DSA SSOT
  - 新增 `backend/app/research/research_matrix_writer.py`：三道硬阈值（磁盘 < 15GB / 单月 > 3GB / 失败率 > 5%）+ monthly run 生命周期 + 批量 upsert（`ON CONFLICT (instrument_id, trade_date) DO UPDATE`）
  - 重写 `backend/scripts/research_feature_matrix_backfill.py`：`--month YYYY-MM` 单月回补 + `--resume` 幂等 + `--export-parquet` 可选 debug 导出；instrument-first 架构；每 100 只 instrument commit 一次；tqdm 进度条
  - 移除 `--output` / `--include-hindsight` / `--include-labels`（始终计算全部 33 字段，DB 主存储）
  - 新增 5 个测试文件：`test_feature_computer.py`（~26 用例）+ `test_research_matrix_writer.py`（~32 用例，async DB savepoint）+ `test_research_feature_matrix_model.py`（model 自测）+ `test_research_feature_matrix_backfill.py`（~13 用例）+ 更新 `test_feature_causality_registry.py`（33 字段）；pytest 119 passed
  - 重写 `docs/current/06-research-feature-matrix.md` + `03-jobs` 2.4.2 节 + `05-testing` 3.8 节；更新 4 个 maps（backend-module / worker-job / database-model / test-coverage）
  - 生产 dry-run 验证：5293 股 × 20 交易日 = 105860 行，0.20GB 估算（远低于 3GB 阈值）
  - 与生产 `stock_feature_snapshots` 严格分离：不接入 watchlist_ready，不写生产 snapshot；不写大 JSONB/GIN 索引（扁平宽表）；不生成中间文件/DB 备份
  - **Blocker Fix（用户 review 反馈）**：
    - Blocker 1：DSA hindsight 不用 causal 近似冒充，`hindsight_dsa_finalized_*` 3 列 Phase 1 全 NULL，metadata 标记 `dsa_hindsight_status=not_implemented`
    - Blocker 2：Node Cluster 全 NULL，metadata 标记 `feature_version=phase1_no_node_cluster` + `node_cluster_status=not_implemented`，PR body 不得说已完成
    - Blocker 3：失败率统计改 `failed_rows / expected_rows`，`failed_count` 列存 `failed_rows`，`metadata_json.failed_instruments` 存股票级失败数
    - Blocker 4：`_process_instrument` upsert 异常 `await db.rollback()` 后继续下一只股票
    - Blocker 5：进程锁双保险 `pg_advisory_lock` + lock file，同 month/scope 拒绝重复启动
  - 新增 7 个 blocker 测试：DSA hindsight 全 NULL / 不等于 causal 近似 / Node Cluster 全 NULL / failed_rows==trade_dates_count / rollback 被调用 / 锁拒绝 / resume 不破坏已完成 run
  - 更新 `docs/current/06-research-feature-matrix.md`（§4.1/5.1/5.2 + §8/10.3/11/12/14 加 Blocker Fix 说明）
  - 更新 `docs/current/03-jobs-integrations-operations.md`（§2.4.2.2 失败率口径 + §2.4.2.3 进程锁 + §2.4.2.7 后台逐月回补 runbook）
  - 更新 `docs/current/05-testing-acceptance.md`（§3.8 加 Blocker Fix 测试 + §3.9 production staged validation A-E 阶段）
  - 分阶段验证延后到 PR merge + migration 应用：A(dry-run)→B(2 symbols)→C(100 stocks)→D(全市场 2026-01)→E(后台逐月回补 2026-02 到当前)

## 2026-07-08

- CHANGE-20260708-054: research feature matrix causality registry
  - 新增 `backend/app/research/feature_causality_registry.py`（`FeatureSpec` + `FeatureCausalityRegistry` + `build_default_registry`，登记 27 字段：causal 10 + confirmed_delay 4 + hindsight 6 + label 7）
  - 新增 `backend/scripts/research_feature_matrix_backfill.py`（CLI 骨架：`--start/--end/--symbols/--limit-instruments/--dry-run/--output/--include-hindsight/--include-labels`，默认 dry-run 不写 DB 不写文件，`--output` 必须配合 sample scope）
  - 新增 `backend/app/research/__init__.py`（包初始化）
  - 新增测试 41 个：`test_feature_causality_registry.py`（24 个）+ `test_research_feature_matrix_backfill.py`（17 个），pytest 41 passed、ruff/mypy/docs checks 通过
  - DSA 双轨：`causal.dsa_confirmed_*`（当时可知）vs `hindsight.dsa_finalized_*`（未来确认后回标注），registry 必须同时登记
  - Node Cluster 只 `hindsight.node_cluster_*`，不得进入 causal；`hindsight.*`/`label.*` 禁止进入回测 feature
  - `confirmed_delay.confirmed_swing_*` 只能在确认 bar 生效，不回填 anchor date
  - 新增 `docs/current/06-research-feature-matrix.md`，更新 MANIFEST/03-jobs/05-testing + 3 个 maps
  - 与生产 `stock_feature_snapshots` 严格分离：不接入 watchlist_ready，不修改 production snapshot，不新增数据库表
  - 未跑历史回补、未跑 production full backfill、未写大文件、未生成 coverage/截图/大日志、未备份数据库
  - 历史 full snapshot 回补仍 BLOCKED（PR #41 126min），研究回补改走 research matrix 小样本 + 默认 dry-run

- CHANGE-20260708-053: feature_snapshot_backfill 轻量 profile-summary 性能诊断模式
  - 新增 `--profile-summary` CLI 参数（默认 False，不启用），新增 `ProfileCollector` 类（record/merge/compute_stats/format_summary）
  - 单进程 `backfill_instrument_first`：传入 profile 时对 load_bars_ms / compute_ms / upsert_ms / total_ms_per_instrument 计时
  - 多进程 `_worker_process_instruments` + `backfill_instrument_first_parallel`：每 chunk 创建独立 ProfileCollector 传给 worker，worker 返回 (stats, profile) 元组，主进程 merge
  - 失败路径也计入 compute_ms（compute 失败时记录，upsert 失败时不重复记录）
  - dry-run 不收集 timing；每 50 instruments 输出进度摘要；结束时 stdout 输出 total/avg/p50/p95 聚合统计 + estimated_full_day_time
  - 不新增表、不写文件、不输出逐股票明细、不改变 success/failed/skipped 统计口径
  - 新增 8 个测试（参数解析 2 + ProfileCollector 3 + worker 2 + parallel merge 1 + dry-run 1 + 失败路径 1）：pytest 53 passed、ruff/mypy/docs checks 通过
  - 不重构公式、不跑历史回补；历史回补仍 BLOCKED，待生产 profile 验证后决定是否进入 compute-once-extract 优化

- CHANGE-20260708-052: 盘后流水线可视化面板（已部署验证）
  - 新增 admin-only 聚合 API：`GET /admin/after-close/pipeline/latest`、`GET /admin/after-close/pipeline?trade_date=`、`GET /admin/after-close/pipeline/runs?limit=`、`POST /admin/after-close/pipeline/run`
  - 新增 `backend/app/services/after_close_pipeline_service.py` 与 `backend/app/schemas/after_close_pipeline.py`，复用 `system_overview_service` 的 data_freshness 与 after_close_orchestrator 状态机
  - `watchlist_ready` 严格复用 `status='succeeded' AND published_at IS NOT NULL AND metadata_.scope='full'`，sample backfill 不显示为前台可读
  - 新增前端 `/admin/after-close` 详情页（顶部状态卡、8 步骤时间线、数据新鲜度、最近运行列表、事件日志抽屉）
  - 系统概览 `AfterClosePipelineCard` 改为摘要卡，提供进入 `/admin/after-close` 链接
  - running 状态 10 秒轮询，非 running 60 秒轮询，页面不可见暂停轮询
  - 后端 11 种场景测试、前端 5 种场景测试；ruff/mypy/docs checks 通过
  - 更新 `02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`、maps
  - forward-port PR #42 nginx 修复（resolver 127.0.0.11 + 变量 proxy_pass），避免 backend 容器重建后 502
  - 部署验证：PR #47 (commit 2b6bf71) 已合并部署，/health + 3 pipeline API + 2 前端页面均 200，backend/frontend 20m 无 5xx/502/timeout，PR #42 已关闭

- CHANGE-20260708-051: 修复 capture worker 偶发 502（page.goto networkidle 超时）导致 monitor 图片缺失
  - 根因：`stock_capture_service.capture_stock_chart` 使用 `page.goto(..., wait_until="networkidle")`，前端 capture 页面存在长连接/持续轮询时 `networkidle` 永不触发，30s 超时返回 502
  - 修复：`wait_until` 改为 `"load"`，保留 `wait_for_selector('[data-render-ready="true"]')` 等待 bars + indicators 就绪后再截图
  - 新增 `backend/tests/test_stock_capture_service.py`（2 用例）：验证 `page.goto` 使用 `wait_until="load"`、截图成功后写入缓存
  - 更新 `03-jobs-integrations-operations.md`、`worker-job-map.md`、ALIGN-039
  - 部署验证：PR #45 (commit 8c1f9c4) 部署后通过 `test_channel_latest_event` 触发单条图片链路，capture_jobs.succeeded、image_url 非空；Outbox delivery_type=image 已 processed；message_deliveries delivery_type=image status=success、image_upload_status=success、image_key 非空；delivery_worker 日志确认“飞书图片消息投递成功”；ALIGN-039 已关闭

- CHANGE-20260708-050: 修复 Monitor 与 Notification latest-event 图片 Capture Token Claims
  - 新增 `backend/app/constants/capture.py` 定义 `CAPTURE_SCOPE_STOCK_DETAIL`，避免服务层 import `app.core.deps` 导致循环依赖
  - `backend/app/core/deps.py` 改为从常量模块导入 `CAPTURE_SCOPE_STOCK_DETAIL`
  - 修复 `backend/app/services/monitor_batch_service.py::_send_chart_images_via_outbox()` 生成 capture token 时缺失 `scope/user_id/instrument_id` 的问题
  - 修复 `backend/app/services/notification_service.py::test_channel_latest_event()` 生成 capture token 时缺失 `scope/user_id/instrument_id` 的问题
  - `backend/app/services/stock_detail_feishu_service.py` 硬编码 `"stock_detail_capture"` 改为常量（业务逻辑不变）
  - 更新 `backend/app/core/security.py::create_capture_token` 文档：明确所有 capture worker 调用方必须传递 `scope/user_id/instrument_id/event_id`
  - 新增测试 `backend/tests/test_monitor_batch_capture_image.py`（5 用例）与 `backend/tests/test_notification_latest_event_capture.py`（2 用例）
  - 更新 `03-jobs-integrations-operations.md`、`05-testing-acceptance.md`、notification-flow-map、worker-job-map、test-coverage-map
  - 新增 ALIGN-038：monitor 文字成功但图片缺失待部署后 smoke 验证
  - 部署验证完成：构造 `[SMOKE_IMAGE]` 单标的事件，capture_jobs.succeeded、image outbox processed、image delivery success、飞书图片投递成功，ALIGN-038 已关闭
  - 不修改 MDAS、前端 K线、monitor 触发、文字通知、outbox_relay、delivery_worker、feishu adapter

## 2026-07-07

- CHANGE-20260707-049: Backfill Multiprocessing 优化
  - `feature_snapshot_backfill.py` 新增 `--workers N` 参数（默认 1 单进程，>1 启用 multiprocessing）
  - 新增 `_worker_process_instruments()`：top-level 可 pickle worker 函数，独立 `async_engine`（pool_size=1/max_overflow=0/pool_pre_ping=True）
  - 新增 `backfill_instrument_first_parallel()`：ProcessPoolExecutor + `asyncio.gather(return_exceptions=True)` 编排（按 chunk 顺序映射，避免 Python 3.12 `as_completed` wrapper future 不可回溯）
  - worker 循环重构为三阶段（load_bars / compute / commit 分离），load 失败时正确标 failed
  - **[Blocker Fix v2]** per-instrument commit 改为 per-date commit（`upsert → db.commit() → success++`，异常 `rollback + failed++`，commit 失败不计 success）；worker future 异常整个 chunk 计 failed（避免 worker 崩溃仍 finalized succeeded）；pool_size 5→1, max_overflow 10→0；`--workers < 1` 拒绝、`> cpu_count` 自动 cap
  - 测试：73 passed（v1 9 + v2 8 Blocker Fix + 56 原有），ruff clean，mypy 0 新增错误
  - 文档：03-jobs / 05-testing / backend-module-map / worker-job-map / test-coverage-map 随 PR 更新
  - 部署边界：未执行生产部署，需部署后 `--workers 2 --dry-run` + 小样本 `--symbols` 验证，再扩大到 `--workers 4`
- CHANGE-20260707-048: Snapshot Run Gate + Instrument-first Backfill
  - 新增 `stock_feature_snapshot_runs` 表（partial unique index 仅约束 `status='running'`，3 btree 索引）
  - 新增 `backend/app/models/stock_feature_snapshot_run.py` + migration `057_stock_feature_snapshot_runs`
  - `feature_snapshot_service` 新增 `create_snapshot_run` / `finish_snapshot_run` run lifecycle（running → succeeded/failed）
  - `after_close_orchestrator` feature_snapshot 步骤前后写 run lifecycle（独立 session 保证 run 记录持久化，snapshot rollback 不影响）
  - `watchlist` 新增 `_has_succeeded_snapshot_run` helper，只读 `status='succeeded'`（且 `published_at` 非空）的 snapshot
  - `feature_snapshot_backfill` 重构为 instrument-first（每只股票每周期只调用一次 `load_instrument_bars`，内存中按 `trade_date` slice）
  - backfill 新增 `--symbols` / `--limit-instruments` 小样本参数；run gate：每个 trade_date 创建 `succeeded`/`failed` run
  - `backend/Dockerfile` 新增 `COPY scripts ./scripts`
  - 测试：49 passed（21 backfill + 11 orchestrator + 11 watchlist + 6 run service），ruff clean，mypy 0 新增错误
  - 部署边界：未执行生产库 migration、未全量 backfill、未 merge/部署；test DB 已验证 alembic upgrade/downgrade/upgrade 链路
- CHANGE-20260707-047: Feature Snapshot 持久化（自选股监控指标从实时计算切换为盘后快照）
  - 新增 `stock_feature_snapshots` 表（JSONB payload + 唯一约束 + 3 btree 索引，无 GIN 索引）
  - 新增 `backend/app/services/feature_snapshot_service.py`：复用 `_compute_all_factors_for_bars` / `_compute_relation` / `_compute_daily_context` / `_compute_m15_response` / `_compute_derived_relation` / `bollinger()` 不复制公式；point-in-time 截断 `index.date <= trade_date`；upsert 幂等；单股失败写 `degraded_reasons` 不阻断批次；`compute_for_trade_date` 不内部 commit，caller 控制 commit/rollback
  - 修改 `backend/app/services/after_close_orchestrator.py` 状态机：`quality_gate → feature_snapshot → publishing`，断点恢复路径更新；feature_snapshot 失败显式 rollback 不进入 publishing
  - 修改 `backend/app/api/watchlist.py::get_watchlist_monitor_status`：metrics 唯一来自 `summary_payload`，新增 `calculation_status` 三态（SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT），`_resolve_expected_snapshot_trade_date`（async）复用 `calendar_service`，删除 `MonitorSnapshotService` 实时 fallback 与 `MonitorState.payload` fallback
  - 新增 `backend/scripts/feature_snapshot_backfill.py` 历史回补 CLI 脚本（核心计算复用 service，脚本只做 CLI/dry-run/resume 真正跳过/批量调用/per-date 事务）
  - 删除 `backend/tests/test_watchlist_monitor_status_fallback.py`（278 行旧 fallback 测试），新增 4 个新测试文件（service + backfill + API 契约 + orchestrator 调整）
  - **PR #38 Review Blocker 修复**：6 个 blocker（`_resolve_expected_snapshot_trade_date` 规则、半成品 rollback、backfill resume 真正跳过、`structural_payload.relation` 字段、PR/docs 历史表述更正、test DB 验证）详见 `records/CHANGE-20260707-047.md` Blocker 修复章节
  - 部署边界：未执行生产库 migration、未全量 backfill、未 merge/部署；test DB 已验证 alembic upgrade/downgrade/upgrade 链路
- CHANGE-20260707-046: 修复 pytdx_adapter 对 aware 1m start/end 的比较异常
  - 根因：PR #35 后 `MarketDataAggregationService` 传入 aware `Asia/Shanghai` start/end，但 `pytdx_adapter.get_minute_bars` 内部 pytdx 数据 `datetime` 列为 naive，比较时触发 `Invalid comparison between dtype=datetime64[us] and Timestamp`
  - 修复：`get_minute_bars` 过滤前将 aware start/end 按 `Asia/Shanghai` 解释后转为 naive
  - 新增测试：`test_pytdx_adapter_minute_aware.py`（aware 过滤 + naive 兼容）
  - 更新 `02-data-api-contracts.md`、`05-testing-acceptance.md`、`test-coverage-map.md`、ALIGN-037
  - 后端 8/8 测试通过，ruff 零错误
- CHANGE-20260707-045: 修复 MDAS live 1m 时区不一致导致 monitor 无事件
  - 根因：`MarketDataAggregationService` 构造 `live_start` 为 naive datetime、`live_end` 为 aware Asia/Shanghai datetime，传入 `pytdx_adapter.get_minute_bars` 后触发 `can't subtract offset-naive and offset-aware datetimes`
  - 修复：两处实时 1m 拉取统一使用 aware `Asia/Shanghai` `live_start`/`live_end`
  - 新增测试：`test_partial_daily_fetch_minute_bars_uses_aware_datetime`、`test_intraday_1m_fetch_minute_bars_uses_aware_datetime`、`test_monitor_cycle_1m_uses_include_realtime`
  - 更新 `02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`05-testing-acceptance.md`、maps、ALIGN-037
  - 后端 6/6 测试通过，ruff 零错误
- CHANGE-20260707-044: DSA visual_segments 时间格式按 timeframe 序列化
  - 修 PR #33 遗留：15m/1h DSA 开关可打开但 canvas 看不到线
  - 根因：`_make_segment` / `compute_dsa_bundle.anchor` / `compute_indicators.time` 写死 `strftime("%Y-%m-%d")`，15m/1h segment time 丢失时间信息，`normalizeChartTime('15m'/'1h')` 返回 null，renderer matched=0
  - 新增 `format_dsa_time(x)`：1d/1w/1mo（无时间部分）→ `strftime("%Y-%m-%d")`；15m/1h（含时间部分）→ `isoformat()`
  - 替换 4 处 strftime：`_make_segment` / `_show_segments` / `compute_dsa_bundle.anchor` / `compute_indicators.time`
  - 前端新增 `frontend/src/utils/dsaSegmentMatch.ts::computeDsaSegmentMatchStats` 纯函数，renderDsaPolyline 在 `?debugIndicatorAlignment=1` 时输出 segment matched 诊断（total/matched/ratio/degradedReason/first-last segment time/first-last display time）
  - 不改 DSA 数学公式（`dsa_vwap` / `dsa_dir` / `regime_id` / `visual_segments.direction` / `points.value` 不变）
  - 后端测试 9/9 通过（`test_dsa_visual_segments_time_format.py`），既有 63 个 DSA 测试无回归
  - 前端 contract 39/39 通过（`dsaSourceAlignment.test.ts`，原 32 + 新增 7 个 PR #34 测试）
- CHANGE-20260707-043: Indicator Overlay Frontend Hardcode Cleanup
  - 修 PR #32 遗留：StrategyChart 仍有 4 处 1d-only / 1w-1mo skip 硬编码
  - L2226 `if (groupId === 'dsa' && timeframe !== '1d') return` → `shouldToggleDsa(groupId, isCaptureMode, captureLayers)`
  - L1661 `if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return` → `shouldRenderDsaLayer(layerId, layers, dsaSourceMismatch, timeframe)`
  - L1666 `if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return` → `shouldRenderBbLayer(layerId, layers, timeframe)`
  - L1503 `if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')` → `shouldIncludeDsaInPriceRange(layerId, layers, timeframe)`
  - 新增 5 个纯函数到 `dsaOverlayPolicy.ts`：`shouldAllowBbOverlay` / `shouldRenderDsaLayer` / `shouldRenderBbLayer` / `shouldToggleDsa` / `shouldIncludeDsaInPriceRange`
  - DSA toggle 全周期可切换（非 capture 模式），DSA/BB 渲染不再按 timeframe 跳过，DSA 全周期参与 y-axis range
  - 保留 source mismatch 保护（shouldRenderDsaLayer 在 mismatch=true 时全周期 false）
  - 保留 capture 锁定（shouldToggleDsa 在 capture 模式锁定 DSA 不可关闭）
  - 前端新增 14 个 contract 测试（dsaSourceAlignment.test.ts 第 5 节），后端 42 测试不变（PR #32 修复仍有效）
  - 不改 DSA/BB 数学公式，不改后端 API 契约，不改 cache version（仍 v5）
- CHANGE-20260707-042: Indicator Overlay All Timeframes
  - 修复 PR #31 的两个错误规则：DSA 1d-only 误禁用 + 1w/1mo BB 字段被直接 pop
  - DSA overlay 全周期支持（1d/15m/1h/1w/1mo），不再 1d-only by design
  - `shouldAllowDsaOverlay` / `shouldCheckDsaMismatch` 全周期返回 true，全部需校验 source 对齐（不绕过 mismatch 保护）
  - DSA toggle 全周期可点击，`DSA_TITLE_HINT(timeframe)` 按周期返回 title（1d="日线结构锚"，非 1d="当前周期验证图层"）
  - 后端 `MarketDataContext.bars_daily=macd_bars` + `daily_time_list=macd_bars.index`，DSA 在所有周期用当前 timeframe bars 计算
  - 后端 `_adapt_watchlist_bb` 1w/1mo 合并到 15m/1h 路径，统一用 `compute_bollinger(macd_bars)` 计算 BB（不再 pop BB 字段）
  - 后端 `chart_layers` 循环删除 1w/1mo BB `continue` 跳过逻辑，1w/1mo BB 图层正常进入 renderer
  - `indicator_cache.ALGORITHM_VERSION` v4→v5，旧 v4 缓存 key 不匹配，强制重算（避免旧缓存返回 1d-only DSA + 1w/1mo 无 BB）
  - 后端新增/修订 6 个测试（cache v5 + BB 1w/1mo + DSA 全周期），前端重写第 4 节 4 个 contract 测试
- CHANGE-20260707-041: Indicator Overlay Final Alignment
  - 修复 DSA VWAP 15m/1h 误禁用根因：Redis cache `ALGORITHM_VERSION` 未 bump（v3→v4），旧缓存命中返回旧格式 source_bar_times + 日线阶梯线 BB
  - 修复 15m/1h BB 图层错位根因：`_adapt_watchlist_bb` 15m/1h 用 `_map_daily_to_intraday` 映射日线 BB（阶梯线），改用 `compute_bollinger(macd_bars)` 重新计算当前周期 BB
  - DSA overlay 周期策略：DSA 是日线级别结构锚，仅 1d 渲染；15m/1h DSA 按钮 disabled + 提示 "DSA VWAP 当前仅支持日线结构锚；15m/1h 请使用 Swing、BB、SQZMOM。"
  - `shouldCheckDsaMismatch(timeframe)` 仅 1d 返回 true，15m/1h 不校验 mismatch，避免误报 "DSA 数据源不一致"
  - 新增 `?debugIndicatorAlignment=1` 诊断工具：console.table 输出 bars/dsa_mismatch/layers 对齐信息
  - 新增 `frontend/src/utils/dsaOverlayPolicy.ts` 纯 .ts 模块（DSA_DISABLED_HINT + shouldCheckDsaMismatch）
  - 后端新增 5 个测试（cache schema 2 + BB overlay 3），前端新增 4 个 DSA overlay policy contract 测试
- CHANGE-20260707-040: DSA Overlay Source Alignment
  - 修复 15m/1h 图表误报 "DSA 数据源不一致，已暂停渲染" 根因（source_bar_times 永远用日线日期格式）
  - 修复 15m 图顶部显示 2026-07-07 03:00 时区错误根因（trade_time 返回 naive datetime 被前端时区误判）
  - 后端 `_df_to_responses` 对 15m/1h 返回 aware datetime（Asia/Shanghai tzinfo，`+08:00`），1d 仍为 date 对象
  - 后端 `compute_source_bar_times/hash` 新增 `timeframe` 参数（15m/1h 含时间，1d 仍日期）
  - 后端 `indicator_service` 15m/1h 改用 `macd_bars` 计算 source 字段，与 chart bars 同源
  - 前端 `normalizeChartTime`/`timeTicks` 迁移到纯 .ts 模块 `chartTime.ts`，便于 Node 测试
  - 新增 14 个前端 contract 测试 + 12 个后端测试（chart_bars_service 6 + indicator_service 3 + bars_vectorization 3）
- CHANGE-20260707-039: Developing Swing Current State（V1.10）
  - 新增 developing swing 字段（14 个），反映"当前正在发生的回落/反弹结构"
  - 修复 active swing 仍不代表当前状态的问题（000100 active_low=4.45 是大段起点，developing_low 应为 6.26 回落后的当前 low）
  - swing_position 三层语义：confirmed pivot + active major leg + developing swing
  - 前端 Swing 摘要卡改用 developing 字段（active/confirmed 移到明细 JSON）
  - Temporal derived_relation 改用 developing swing，不回退 active/confirmed raw
  - 5 种计算场景：major up 回落 / major up 创新高 / major down 反弹 / major down 创新低 / fallback

## 2026-07-06
- CHANGE-20260706-038: Swing Active State + Capture 布局 + Publish Auto-trigger
  - 新增 active swing 字段（clip [0,1]），修复 confirmed raw >1 问题
  - temporal derived_relation 改用 active swing
  - DSA age 统一为 +1 口径
  - capture 模式隐藏按钮和侧列
  - worker.py DSA 完成后自动触发 after_close_orchestrator
  - 生产补偿发布 2026-07-06 DSA run（job_run_id=90683e3e, published_at=2026-07-06 23:54:17）

## 2026-07-06: 前端不覆盖后端 1d partial bar

- 修复 `StockDetailPage.tsx` 在交易时段后端已返回 1d partial bar 时仍调用 `mergeRealtimeQuoteIntoBars` 覆盖 K线的问题：仅当 `timeframe==='1d' && barsQuery.data?.is_partial !== true` 时才允许 quote 合并，否则 `displayBars` 直接使用 `baseBars`。
- 修复 `frontend/src/utils/chart.ts::mergeRealtimeQuoteIntoBars` 无条件合并 quote 的问题：新增 `backendIsPartial` 参数，后端已返回 partial bar 时直接返回原 bars。
- 新增前端测试 2 个：`1d 后端已返回 partial bar 时 quote 不覆盖`、`1d 后端未返回 partial bar 时 quote 可兜底追加`。
- 更新 `docs/current/02-data-api-contracts.md`：明确 `mergeRealtimeQuoteIntoBars()` 当且仅当后端未返回 `is_partial=true` 时才允许合并；补充 `12.2` 的 `last_live_bar_time` 与 `is_partial` 事实源说明；把“后端未返回 partial”写入 `12.3` 合并条件首位。
- 更新 `docs/maps/frontend-route-map.md`、`docs/maps/test-coverage-map.md`。
- 新增 CHANGE-20260706-037。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-06: Monitor 投递与 live bar 后续修复

- 修复 `delivery_worker.py` 对 `monitor_event`/`strategy_event`/`monitor_chart` 仍走普通资格导致 admin 自动监控被排除的问题：投递前调用 `is_user_eligible_for_monitor` 复核，active admin 与 active member + 有效 subscription 放行，disabled admin / 无订阅普通用户标记 dead/USER_INELIGIBLE；`stock_detail_share` 仍跳过资格，`beta_application_admin` 仍跳过 subscription。
- 修复 `monitor_batch_service.py` 盘中监控 1m 输入仍用 `include_realtime=False` 的问题：1m 改为 `include_realtime=True` 并剔除最后一根未完成 bar，日线/15m 输入保持 `include_realtime=False`；`MonitorCycleResult` 新增 `last_minute_is_partial`，cycle done 与单标的日志输出 `instrument/symbol/source_bar_time/minute_data_source/minute_is_partial/events_detected/events_written`。
- 修复 `market_data_aggregation_service.py` 1d 交易时段无 partial daily bar 的问题：`timeframe=1d && include_realtime=true && MORNING_SESSION/AFTERNOON_SESSION` 时，用当日已完成 1m bar 合成 partial daily bar 追加到响应末尾，返回 `data_source=hybrid`、`is_partial=true`、`last_live_bar_time`；非交易时段、收盘后、`include_realtime=false` 时不合成；不写库。
- 修复 `/quote` 时区：`backend/app/api/bars.py` 与 `backend/app/core/pytdx_adapter.py` 对 naive datetime 和 `+00:00` 字符串统一按 Asia/Shanghai 解释，确保前端显示上海时间。
- 修复 Architecture Rules `duplicate-plan-feature-list`：`outbox_relay.py` 与 `delivery_worker.py` 中的 `_MONITOR_SOURCE_TYPES` 提取为 `app/constants/monitor_source_types.py` 单点真源。
- 新增 `AGENTS.md` `### 13. 个股详情 K线实时契约`，把 `/bars?timeframe=1d&include_realtime=true` 固化为个股详情 K线实时的唯一后端契约，明确 `/quote` 实时 ≠ K线实时、`mergeRealtimeQuoteIntoBars()` 只能兜底视觉增强。
- 新增后端测试 4 个文件 6 个用例：`test_delivery_worker_monitor_eligible.py`、`test_monitor_batch_live_minute.py`、`test_market_data_aggregation_partial_daily.py`、`test_quote_timezone.py`。
- 更新 `AGENTS.md`；更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`（新增 K线实时契约 blocking 门禁）、`code-doc-alignment.md`；更新 `docs/maps/api-route-map.md`、`backend-module-map.md`、`frontend-route-map.md`、`notification-flow-map.md`、`test-coverage-map.md`。
- 新增/更新 ALIGN-036（delivery_worker monitor 资格修复待生产验证）、ALIGN-037（1d partial daily bar 与 live 1m monitor 待生产验证）。
- 新增 CHANGE-20260706-036（含根因：8c991e3d 统一 MDAS 后旧 `/bars` 1d 实时语义未完整迁移；PR #25 修 quote 可信化但未恢复 1d partial bar）。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-05: Admin 监控资格修复 + 个股详情实时行情可信化

- 修复 admin 自选股被监控过滤：新增 `eligible_user_service.filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`，active admin 与 active member + 有效 subscription 进入监控，disabled admin / 无订阅普通用户排除；`monitor_batch_service`/`event_recipient_service`/`outbox_relay` 三处统一口径。
- 修复个股详情实时行情伪实时：`/api/v1/instruments/{id}/quote` 返回 `source`/`is_realtime`/`update_time`/`freshness_seconds`/`degraded`/`degraded_reason`；pytdx 成功才标实时，非交易时段 fallback 不降级，交易时段 pytdx 失败才降级并记录原因；`mergeRealtimeQuoteIntoBars` 仅当 `quote.is_realtime && source==="pytdx" && freshness_seconds<=60` 才合并；`StockDetailPage` 显示行情状态徽章与 K 线状态条，不再固定显示“实时行情”；删除 1m 配置；午休统一复用 `market_status_service.compute_market_session`；quote 10s、bars/indicators 30s 轮询，页面 hidden 停止后台轮询；pytdx 单例+线程锁+Redis 10s 缓存，带断线重连与超时保护。
- 新增后端测试 10 个（`test_monitor_eligible.py` 5 + `test_quote_trustworthy.py` 5）、前端 chart 测试 8 个、本地 ASGI 验证脚本 `scripts/verify_quote_trustworthy.py`。
- 更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md`、`MANIFEST.md`、`code-doc-alignment.md`；更新 `docs/maps/api-route-map.md`、`backend-module-map.md`、`frontend-route-map.md`。
- 新增 ALIGN-034（admin monitor 资格待生产验证）、ALIGN-035（quote 可信化与 pytdx 连接保护待生产验证）。
- 新增 CHANGE-20260705-034。
- 本次不部署生产，待用户确认 diff、测试结果与验证证据后授权 build/restart。

## 2026-07-05: 时序特征 V1 + 个股详情页结构状态面板隐藏开关

- 后端新增 `app.services.temporal_feature_service.compute_temporal_features`：双周期（1d+15m）时序特征，补变化量/持续度/派生关系；daily_context 9 字段 + m15_response 9 字段 + derived_relation 3 字段；复用 V1.8 `compute_structural_factors` 获取 primary/secondary factors；point-in-time 重算 SQZMOM/BB bandwidth/volume_percentile，无未来函数；V1 只支持 `as_of=latest`；组级异常隔离（daily/m15/derived 独立 try/except，单组失败返回 null dict + degraded_reasons）。
- 后端新增 API `GET /api/v1/instruments/{id}/temporal-features`，无认证要求，参数 `primary_timeframe`/`secondary_timeframe`/`adj`/`as_of`；非法参数返回 400（含 `as_of != "latest"`）；不存在 instrument 返回 200 + degraded_reasons。
- 前端 `StockDetailPage.tsx` 结构状态面板默认隐藏 + 用户开关 + localStorage 持久化；`?hideStructuralState=1` / `?capture=1` / `?capture=feishu` 强制隐藏且禁用开关；截图模式默认只渲染 K 线和基础信息；toggle 按钮移入 `tv-chart-column` 内部（`position: relative`）确保定位稳定。
- 新增后端测试 26 个（服务 20 + API 6）、前端 contract test 8 个。
- 更新 `docs/current/02-data-api-contracts.md`（新增第 11 节，含 `as_of!=latest` 返回 400 与组级异常隔离描述）、`04-frontend-ux.md`、`05-testing-acceptance.md`、`docs/maps/api-route-map.md`、`frontend-route-map.md`、`test-coverage-map.md`。
- 新增 CHANGE-20260705-033。

## 2026-07-05: 结构状态因子面板升级至 V1.8（补齐 50 字段 + 客观 relation）

- 后端 `structural_factor_service.py` 扩展 V1.8 字段：dsa_segment 新增 current/prev 段收益、斜率、效率、段级成交量、段间对比；swing 新增 swing_range/price_position/retracement/rebound/bars_since；cost 新增 price_vs_poc_atr/value_area_position/nearest_node_*/distance_to_node_*_atr/node_*_strength；volatility 新增 distance_to_bb_*_atr/sqz_on/sqz_off/sqzmom_abs_percentile；participation 共享段级成交量；relation 移除 momentum_alignment，改为 primary_dir/secondary_dir/trend_alignment/primary_swing_position/secondary_swing_position/primary_slope_atr/secondary_slope_atr/secondary_vs_primary_position_delta。
- 段收益/斜率/效率一律基于 close，不再用 dsa_vwap 替代（修复 V1.7 bug）。
- 前端 `StockStructuralStatePanel.tsx` CARDS 扩展为 V1.8 完整字段，新增 `fmtBool` 格式化器；Relation 区块重写为客观关系字段。
- 前端 `endpoints.ts` `StructuralFactorResponse.relation` 类型同步更新。
- 后端新增 10 个 V1.8 测试（双周期差异、无未来函数、sqz_on/sqz_off、Relation primary_dir、段收益、Swing position、Node degraded、SQZMOM abs percentile 等），共 44/44 passed。
- 前端契约测试新增 V1.8 字段存在性断言（v18Keys 33 项 + v18RelationKeys 7 项），共 10/10 passed。
- 更新 `docs/current/02-data-api-contracts.md`（第 10 节 V1.8 完整字段表）、`04-frontend-ux.md`、`05-testing-acceptance.md`、`docs/maps/api-route-map.md`、`frontend-route-map.md`、`test-coverage-map.md`。
- 新增 CHANGE-20260705-032。

## 2026-07-05: 个股详情页新增结构状态因子面板（V1.7）

- 后端新增 ATR SSOT `app.strategy_assets.algorithms.features.atr_utils.compute_atr`（Pine RMA 等价）。
- 后端新增 `app.services.structural_factor_service.compute_structural_factors`：双周期（1d+15m）5 组结构因子（DSA 段/Swing/成本节点/动量波动/成交参与），每组独立 try/except 异常隔离。
- 后端新增 API `GET /api/v1/instruments/{id}/structural-factors`，无认证要求，250-500 bar lookback，15m 仅已完成 bar，Swing 无未来函数。
- 前端新增 `StockStructuralStatePanel.tsx`（5 卡片 + 双周期 tabs + 降级提示 + 明细折叠），`StockDetailPage` 改为双列布局（1fr + 340px），截图模式和窄屏（≤1250px）隐藏面板。
- 前端只渲染后端 DTO，禁止重新计算因子。
- 新增后端测试 34 个（ATR SSOT 9 + 服务 20 + API 5）、前端 contract test 8 个；后端 34/34 passed，前端 71/71 contract test passed。
- 更新 `docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps。
- 新增 CHANGE-20260705-031。

## 2026-07-05: 个股详情页新增 SQZMOM_LB 指标图层

- 后端新增 `app.strategy_assets.algorithms.features.sqzmom_lb`，逐行复刻 TradingView Pine `SQZMOM_LB`。
- `indicator_service.compute_all_indicators` 注入 `sqzmom_lb` 数据与图层；`/api/v1/instruments/{instrument_id}/indicators` 响应新增 `data.sqzmom_lb`。
- 前端 `StrategyChart.tsx` 新增 SQZMOM_LB 图层开关（默认关闭）和独立副图渲染；前端只消费后端 DTO，不重新计算指标。
- 新增后端测试 21 个、前端 contract test 5 个；后端 49/49 passed，前端 63/63 contract test passed。
- 更新 `docs/current/02-data-api-contracts.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps。
- 新增 CHANGE-20260704-030。

## 2026-07-04 Phase I: 趋势选股 result_id 未回填修复 + 生产验证

- PR #15 部署后发现 succeeded 行 `result_id` 全部为 None（PR #14 batch service 未回填）
- 修复 `query_run_items_with_results`：改用 `(run_id, instrument_id)` 关联 `strategy_results`（非 `result_id`）
- 修复 `_apply_run_item_filters` metric_filter 子查询：JOIN `strategy_results` + `strategy_result_metrics`
- 修复 sort LEFT JOIN：通过 `instrument_id` 关联（非 `result_id`）
- 生产验证通过：run_id=f0c15e1c, source_total=5293, succeeded 行正确显示 35 个 DSA 指标
- ALIGN-032 关闭（全量 universe 展示已验证）
- 新增 ALIGN-033（batch service 未回填 result_id，P2）
- 新增历史债务分级审计 AUDIT-20260704

## 2026-07-04 Phase H: 趋势选股页全量 Universe 展示

- 修复趋势选股页只显示 804 命中/4391 失败的问题：根因为 `/strategy-runs/{run_id}/results` 以 `strategy_results` 为主表（仅 succeeded 行）
- 后端改为以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed）
- 新增 `item_status`/`reason_code`/`error_message` 字段，skipped/failed 行 `id`/`payload` 为 null
- 前端 ScreenerPage 行 key 改用 `instrumentId`（不依赖 `result_id`），"命中"改名"筛选结果"
- 前端 adapter 支持 null id/payload 降级（`resultId=''`、`payload={}`）
- AGENTS.md 写入 node:20-alpine 保护规则（第 12 条）
- 新增 4 个后端测试 + 4 个前端 adapter 测试
- 新增 CHANGE-20260704-028，新增 ALIGN-032

## 2026-07-04 Phase G: DSA Run 总超时与 Computable Universe 口径修复

- 修复 DSA-only 运行后 1881 只 failed（全部 reason_code=timeout）：run 级总超时从 600s 改为 7200s（可配置 STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS），与 after_close_orchestrator 对齐
- 新增 _classify_computable_universe：历史日线 < 60 根标的在 create_batch_run 时标记 skipped/insufficient_history，不进入计算循环
- 修复 execute_run 覆盖 skipped_count：初始化 skipped = run.skipped_count or 0，保留预置的 insufficient_history 数量
- run 级总超时耗尽后剩余 pending 项标记 failed/run_timeout_budget_exhausted，与单股 timeout 区分
- 新增 8 个测试用例，21 passed
- 新增 CHANGE-20260704-027，新增 ALIGN-031，更新 ALIGN-030

## 2026-07-04 Phase F: PR #11 部署后热修 bars/indicators page_size 上限

- 生产验证发现 15m/1h 个股详情请求触发 422：`/api/v1/instruments/{id}/bars` page_size 上限 1000，`/api/v1/instruments/{id}/indicators` bars 上限 500
- 将 bars page_size 上限提升至 4000，indicators bars 上限提升至 4000，与 Node Cluster 15m=4000、1h=1200 契约对齐
- 顺手修复 `backend/app/api/bars.py` Ruff 错误（未使用导入、缺失 `get_redis` 导入）
- 新增 CHANGE-20260704-023，更新 `docs/maps/api-route-map.md`

## 2026-07-04 Phase E: 修复 4 个生产功能缺陷

- 修复 DSA-only 覆盖率 0% 与系统概览 98% 口径不一致：新增 `BarsCoverageService` 统一三处重复 SQL，DSA-only 端点 fallback 到最新可用交易日
- 修复个股详情 K 线图未合并实时行情：前端新增 `mergeRealtimeQuoteIntoBars`，区分 baseBars（指标用）与 displayBars（图表用）
- 修复自选股监控列表空值：无 `MonitorState` 时通过 `MonitorSnapshotService` 只读 fallback 计算指标
- 修复飞书消息时间显示 UTC/+0：统一使用 `format_shanghai_datetime` 输出 Asia/Shanghai 时区
- 新增 5 个测试文件覆盖上述修复
- 更新 `docs/current/02-data-api-contracts.md`、`03-jobs-integrations-operations.md`、`04-frontend-ux.md`、`05-testing-acceptance.md` 及相关 maps

## 2026-07-02 Phase D: 剩余 Alignment 缺口修复

- 修复 ALIGN-019：`publish_run` 仅允许 `completed` 发布，拒绝 `partial_failed`
- monitor 行情统一走 `MarketDataAggregationService`，支持 `1m` 周期
- 修正 `monitor_batch_service.py` 陈旧注释（3600 → 4000 = 250×16）
- CI 改为三层 Ruff 门禁：`Ruff New Files` 阻断新增文件错误；`Ruff Baseline Regression` 阻断历史债务新增/增加；`Ruff Full Repository Report` 非阻断上传报告
- CI 改为三层 Mypy 门禁：`Mypy New Files` 阻断新增 backend/app 生产文件错误；`Mypy Baseline Regression` 阻断历史债务新增/增加/总数超基线；`Mypy Full Repository Report` 非阻断上传报告；基线 commit `64ed75c`、诊断总数 242（mypy 2.1.0 + numpy<2.5.0）、当前 241；`backend/pyproject.toml` 固定 mypy==2.1.0，并将 `numpy` 上限收紧为 `<2.5.0`；修复 mypy 报告步骤因历史错误提前失败的问题
- 修复本次新增 mypy 错误：`app/api/stock_detail_feishu.py` 自测代码使用 `getattr(route, "path", None)`；`app/repositories/bar_repository.py` 删除重复 `_query_minute_bars` 定义
- 修正文档 Commit 自引用：代码实现 Commit 与文档 Commit 分离，记录 `implementation_base_commit` / `verified_implementation_commit`
- ALIGN-014 在 GitHub Actions Run #36（最终 HEAD `a053d0c`）全部 blocking jobs 成功后关闭；ALIGN-018 同步关闭
- 测试：1106 passed（后端全量）；frontend tsc/lint/build 通过；frontend contract 52 passed

## 2026-07-02 Phase C: Platform App only + Capture 专用链路

- 永久删除 feishu_webhook_adapter，统一 feishu_platform_app（CHANGE-20260702-009）
- 新增 Capture 专用链路：`/capture/stock/:symbol` + `/api/v1/capture/stocks/{id}/snapshot`
- Capture Token 隔离：type=capture + scope=stock_detail_capture，普通 API 拒绝
- 状态机统一：截图失败返回 partial_failed + failed_step/error_code/error_message
- migration 055：CHECK 约束禁止 feishu_webhook
- 测试：1106 passed（新增 33 个飞书/Capture 相关测试）

| Change ID | 日期 | 标题 | 状态 | 分支 | Base Code Commit | Head/Merge Commit | 影响文档 |
|---|---|---|---|---|---|---|---|
| CHANGE-20260702-001 | 2026-07-02 | 建立并校正多维度当前设计基线 | ready_for_import | `docs/current-design-baseline` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | 导入提交后填写 | 全部 current 文档 |
| CHANGE-20260702-002 | 2026-07-02 | 导入当前设计文档基线到修复分支 | committed | `fix/release-feishu-marketdata-dsa` | `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822` | `a7b9ca91eba567b3ed3dbc4bb2884c4779471da2` | 全部 current 文档、AGENTS.md、.gitignore |
| CHANGE-20260702-003 | 2026-07-02 | 修复行情聚合服务 Redis 缓存开关未生效导致测试污染 | committed | `fix/release-feishu-marketdata-dsa` | `af3f55696a1abe0afe771a804528ff02b0f31a33` | `c22940d12addd61a4ff5fadca61dc69a7f8d9df4` | `backend/app/services/market_data_aggregation_service.py` |
| CHANGE-20260702-004 | 2026-07-02 | DSA 选股计算性能基准测试（350 只代表性股票） | committed | `fix/release-feishu-marketdata-dsa` | `9b842347e2d571b2b5acca309b7d95d853ce2da1` | `09f344b2633b45ac0431f480d9b6bf3a906657f8` | `backend/reports/dsa_benchmark_20260702.md` |
| CHANGE-20260702-005 | 2026-07-02 | Phase 6 文档对齐与旧术语清理 | committed | `fix/release-feishu-marketdata-dsa` | `a331a406ddf2e7b787a43788f4372436425c6d1` | `dc88c47625b22ca8a95f30d97036c6155e9a2cc4` | `docs/current/03-business-rules.md`、`10-permissions-security.md`、`11-jobs-integrations.md`、`12-strategy-indicator-contracts.md`、`18-code-doc-alignment.md` |
| CHANGE-20260702-006 | 2026-07-02 | Phase 7 全量测试与构建链路验证，修复测试旧术语断言 | committed | `fix/release-feishu-marketdata-dsa` | `ed476a050b1c562a994f82e23540d9c0492850c6` | `3dfeaca8c4fd7ed3cf6f14373aeedb98f9c6b8b2` | `backend/tests/test_me_entitlements.py`、`docs/changes/records/CHANGE-20260702-006.md` |
| CHANGE-20260702-007 | 2026-07-02 | 文档单一事实源治理与 AGENTS 项目硬规则 | committed | `chore/docs-governance-single-source` | `31f5776a247715f15713549211652dbb5a27d855` | `e6e8897` | `docs/数据结构.md`（删除）、`docs/操作手册.md`（删除）、`docs/指标参数基线.md`（删除）、`tools/update_docs.py`、`AGENTS.md`、`docs/current/*` |
| CHANGE-20260702-008 | 2026-07-02 | 恢复 Node Cluster 250×16 契约 | committed | `fix/node-cluster-250x16-contract` | `e6e8897` | `1ffb992` | `backend/app/constants/indicator_contract.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/strategy_assets/algorithms/features/unified_volume_profile.py`、`backend/tests/*`、`docs/current/12-strategy-indicator-contracts.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260702-009 | 2026-07-02 | Phase C - Platform App only + Capture 专用链路 | committed | `fix/feishu-platform-only-capture` | `1ffb992` | `64ed75c` | `backend/app/services/feishu_webhook_adapter.py`（删除）、`backend/app/api/capture.py`（新增）、`backend/app/core/security.py`、`backend/app/core/deps.py`、`backend/alembic/versions/055_feishu_platform_app_only.py`、`frontend/src/App.tsx`、`frontend/src/pages/CaptureStockPage.tsx`、`docs/current/09-api-contracts.md`、`docs/current/10-permissions-security.md`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260702-010 | 2026-07-02 | Phase D - 剩余 Alignment 缺口修复 + Ruff 三层增量阻断策略 + 文档自引用修正 | committed | `fix/release-remaining-alignment-gaps` | `64ed75c` | `ed8bcef` | `backend/app/services/strategy_batch_service.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/repositories/bar_repository.py`、`.github/workflows/ci.yml`、`backend/tests/*`、`tools/quality_baselines/ruff.json`、`tools/compare_ruff_baseline.py`、`tools/check_architecture.py`、`tools/check_test_allowlist.py`、`AGENTS.md`、`docs/current/14-deployment-operations.md`、`docs/current/15-testing-acceptance.md`、`docs/current/18-code-doc-alignment.md`、`docs/changes/records/CHANGE-20260702-010.md` |
| CHANGE-20260702-011 | 2026-07-02 | Phase C/D - 真正接通 Capture Snapshot 链路与补齐图文状态机 + Mypy 增量阻断策略 | committed | `fix/release-remaining-alignment-gaps` | `8752f20` | `a053d0c` | `frontend/src/pages/CaptureStockPage.tsx`、`frontend/src/api/endpoints.ts`、`frontend/scripts/contract-tests/capture-stock-page.test.ts`、`backend/app/services/stock_capture_service.py`、`backend/app/services/stock_detail_feishu_service.py`、`backend/app/api/stock_detail_feishu.py`、`backend/app/repositories/bar_repository.py`、`backend/pyproject.toml`、`backend/tests/test_capture_snapshot.py`、`backend/tests/test_capture_token_isolation.py`、`backend/tests/test_state_machine.py`、`backend/tests/test_stock_detail_feishu_status.py`、`.github/workflows/ci.yml`、`tools/check_mypy_new_files.py`、`tools/compare_mypy_baseline.py`、`tools/generate_mypy_baseline.py`、`tools/quality_baselines/mypy.json`、`AGENTS.md`、`advice.md`、`docs/current/14-deployment-operations.md`、`docs/current/15-testing-acceptance.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-013 | 2026-07-03 | 修复 release candidate 新增 backend/app 文件 mypy 错误，解除 Mypy New Files CI 阻断 | committed | `release/docs-aligned-candidate-v3` | `82e4afd` | `d5f69d1` | `backend/app/services/subscription_service.py`、`backend/app/services/market_data_aggregation_service.py`、`backend/app/models/access_audit_log.py`、`backend/app/scripts/fix_instruments_remove_indices.py`、`backend/app/api/capture.py`、`backend/app/api/admin_subscription.py`、`docs/current/15-testing-acceptance.md` |
| CHANGE-20260703-014 | 2026-07-03 | 删除独立管理员飞书渠道配置，管理员通知复用管理员用户自己的 feishu_platform_app NotificationChannel | committed | `fix/admin-notification-use-admin-channel` | `5cf0426` | `5cf0426` | `backend/app/constants/system_channel.py`（删除）、`backend/app/services/outbox_relay.py`、`backend/app/services/beta_application_notifier.py`、`backend/app/services/beta_application_service.py`、`backend/app/services/delivery_worker.py`、`backend/app/services/feishu_card_builder.py`、`docker-compose.prod.yml`、`tools/pre_deploy_check.py`、`backend/tests/*`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-015 | 2026-07-03 | outbox target_channel_id 跳过 eligible_user_service（ddca659 hotfix 治理闭环） | committed | `chore/governance-baseline-repair-v2` | `ddca659b8c9d64b6a414da0b4bbd6f80f704aef1` | `bbf6215` | `backend/tests/test_outbox_target_channel_id.py`、`docs/current/18-code-doc-alignment.md`、`tools/check_docs_consistency.py`、`tools/tests/test_check_docs_consistency.py`、`docs/current/*.md`、`docs/README.md` |
| CHANGE-20260703-016 | 2026-07-03 | 修复 worker_heartbeats 僵尸 running 记录清理机制 | committed | `fix/worker-heartbeat-stale-cleanup` | `40dd2287f0962910d2e272c468b3e5054abddaaf` | `095c4ad` | `backend/app/worker.py`、`backend/tests/test_worker_heartbeat_stale_cleanup.py`、`docs/current/11-jobs-integrations.md`、`docs/current/18-code-doc-alignment.md` |
| CHANGE-20260703-017 | 2026-07-03 | docs 信息架构重构为 v2 system map（current + maps + onboarding + restore checklist） | merged | `docs/restructure-system-map-v2` | `40dd2287f0962910d2e272c468b3e5054abddaaf` | `cafbdc4` | `docs/current/*`（旧 00-18 归档至 `docs/archive/current-legacy-20260703/`）、`docs/maps/*`、`docs/AI-ONBOARDING.md`、`docs/RESTORE-CHECKLIST.md`、`docs/MAINTENANCE.md`、`docs/MIGRATION-MAP.md`、`docs/TRAE-APPLY-INSTRUCTION.md`、`docs/SOURCE-SNAPSHOT.md`、`docs/README.md`、`docs/changes/records/CHANGE-20260703-017.md`、`docs/changes/CHANGELOG.md`、`tools/check_docs_consistency.py`、`tools/tests/test_check_docs_consistency.py`、`tools/update_docs.py`、`tools/check_architecture.py` |
| CHANGE-20260704-018 | 2026-07-04 | v2 docs 治理收口 + v2 结构检查加强（8 map 全检 + 测试） | committed | `chore/docs-v2-governance-finalize` | `cafbdc4217301d8bf00ff9d42aeabbef43eb58fb` | 待合并后填写 | `docs/current/code-doc-alignment.md`、`docs/current/MANIFEST.md`、`docs/changes/records/CHANGE-20260703-017.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-018.md`、`tools/check_architecture.py`、`tools/tests/test_check_architecture.py` |
| CHANGE-20260704-019 | 2026-07-04 | 新增生产 worker-watchdog 服务让 _recovery_watchdog_loop 在生产运行 | merged | `fix/worker-watchdog-production-service` | `b4b5918c23df2b21a1f54e0e81aaa323f287e150` | `67105c2` | `docker-compose.prod.yml`、`docs/current/03-jobs-integrations-operations.md`、`docs/maps/worker-job-map.md`、`docs/maps/deployment-runtime-map.md`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-019.md` |
| CHANGE-20260704-020 | 2026-07-04 | 关闭 ALIGN-023：worker-watchdog 生产验证 stale running 清零 | merged | `chore/close-align-023-worker-watchdog` | `67105c2` | `30ddc8a` | `docs/current/code-doc-alignment.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-020.md` |
| CHANGE-20260704-021 | 2026-07-04 | worker/notification/capture 边界审计 + 后续小 PR 拆分计划 | committed | `chore/boundary-audit-worker-notification-capture` | `30ddc8a` | 待合并后填写 | `docs/architecture-audits/AUDIT-20260704-worker-notification-capture-boundaries.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-021.md`、`docs/maps/worker-job-map.md`、`docs/maps/notification-flow-map.md`、`docs/maps/test-coverage-map.md`、`docs/current/code-doc-alignment.md` |
| CHANGE-20260704-022 | 2026-07-04 | 修复 4 个生产功能缺陷：DSA-only 覆盖率口径、K 线实时行情合并、自选股监控 fallback、飞书消息中国时区；残留修复：覆盖率门禁使用 `coverage_raw`、watchlist fallback 条件扩展、1d K 线日期语义 | committed | `fix/market-data-dsa-watchlist-feishu-timezone` | `4af271d` | 待提交后填写 | `backend/app/services/bars_coverage_service.py`、`backend/app/core/time.py`、`backend/app/api/admin_after_close.py`、`backend/app/api/watchlist.py`、`backend/app/services/after_close_orchestrator.py`、`backend/app/services/bars_scheduler_service.py`、`backend/app/services/message_builder.py`、`backend/app/services/monitor_batch_service.py`、`backend/app/services/notification_service.py`、`backend/app/services/stock_detail_feishu_service.py`、`backend/app/services/system_overview_service.py`、`frontend/src/utils/chart.ts`、`frontend/src/pages/StockDetailPage.tsx`、测试文件、docs |
| CHANGE-20260704-023 | 2026-07-04 | PR #11 部署后热修：bars / indicators page_size、bars 上限与 Node Cluster 15m/1h 契约对齐 | in_validation | `fix/bars-indicators-page-size-15m` | `0f29e5e` | 待合并后填写 | `backend/app/api/bars.py`、`backend/app/api/indicators.py`、`docs/maps/api-route-map.md`、`docs/changes/CHANGELOG.md` |
| CHANGE-20260704-024 | 2026-07-04 | 自选监控页 UI 调整、AGENTS 无备份部署规则、TCL 科技单标历史回补 | committed | `fix/bars-indicators-page-size-15m` | `43e2334` | 待合并后填写 | `frontend/src/features/watchlist-monitor/*`、`frontend/src/pages/WatchlistPage.tsx`、`frontend/src/styles/global.scss`、`frontend/package.json`、`backend/tools/backfill_single_instrument.py`、`AGENTS.md`、`docs/current/02-data-api-contracts.md`、`docs/current/04-frontend-ux.md`、`docs/current/code-doc-alignment.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/test-coverage-map.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-024.md` |
| CHANGE-20260704-025 | 2026-07-04 | Admin Jobs 可观察性补齐 - Worker 心跳 Tab + 只读 admin API | committed | `feat/admin-jobs-observability` | `0f29e5e` | 待合并后填写 | `backend/app/schemas/worker_heartbeat.py`、`backend/app/api/admin_subscription.py`、`backend/tests/test_admin_worker_heartbeats_api.py`、`frontend/src/api/endpoints.ts`、`frontend/src/hooks/useApi.ts`、`frontend/src/pages/AdminJobsPage.tsx`、`docs/current/02-data-api-contracts.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/current/04-frontend-ux.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/worker-job-map.md`、`docs/maps/test-coverage-map.md`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-025.md` |
| CHANGE-20260704-027 | 2026-07-04 | DSA Run 总超时与 Computable Universe 口径修复 | committed | `fix/dsa-run-timeout-and-computable-universe` | 待填写 | 待合并后填写 | `backend/app/services/strategy_batch_service.py`、`backend/tests/test_strategy_batch_service.py`、`docker-compose.prod.yml`、`docs/current/02-data-api-contracts.md`、`docs/current/03-jobs-integrations-operations.md`、`docs/current/code-doc-alignment.md`、`docs/maps/*`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-027.md` |
| CHANGE-20260704-028 | 2026-07-04 | 趋势选股页全量 universe 展示：主表改 strategy_run_items LEFT JOIN strategy_results，行 key 改 instrumentId，"命中"改名"筛选结果"，AGENTS 写入 node:20-alpine 保护规则 | merged | `fix/screener-full-universe-results` | `d47bb46` | `44d37fd` | `backend/app/repositories/strategy_result_repository.py`、`backend/app/services/selector_query_service.py`、`backend/app/schemas/strategy_run.py`、`backend/app/api/strategy_runs.py`、`backend/app/models/strategy_run.py`、`backend/tests/test_strategy_results_universe.py`、`backend/tests/test_business_integration.py`、`backend/tests/test_selector_query_integration.py`、`frontend/src/api/endpoints.ts`、`frontend/src/features/trend-selection/adapters.ts`、`frontend/src/features/trend-selection/__tests__/adapter.test.ts`、`frontend/src/pages/ScreenerPage.tsx`、`AGENTS.md`、`docs/AI-ONBOARDING.md`、`docs/current/02-data-api-contracts.md`、`docs/current/04-frontend-ux.md`、`docs/current/code-doc-alignment.md`、`docs/maps/api-route-map.md`、`docs/maps/frontend-route-map.md`、`docs/maps/deployment-runtime-map.md`、`docs/maps/test-coverage-map.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-028.md` |
| CHANGE-20260704-029 | 2026-07-04 | 趋势选股 result_id 未回填修复：改用 (run_id, instrument_id) 关联 strategy_results + 历史债务审计 | in_validation | `fix/screener-result-join-by-instrument` | `44d37fd` | 待合并后填写 | `backend/app/repositories/strategy_result_repository.py`、`docs/current/code-doc-alignment.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260704-029.md`、`docs/architecture-audits/AUDIT-20260704-ruff-mypy-debt-triage.md` |
| CHANGE-20260716-006 | 2026-07-16 | AFC 详情页终审修正（originScope 唯一来源+confirmed_swing_position 产品观察+recentChanges 仅最新交易日+grid 布局） | completed | `feat/next-phase-20260716` | `01266d1` | `18049da` | `backend/app/api/stock_context.py`、`backend/app/schemas/atomic_fact_contract.py`、`backend/app/services/atomic_fact_contract_service.py`、`backend/app/contracts/atomic_fact_product_observations_v1.json`（新增）、`backend/tests/test_atomic_fact_contract_service.py`、`backend/tests/test_stock_context_atomic_facts.py`、`frontend/src/api/endpoints.ts`、`frontend/src/components/StrategyChart.tsx`、`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`、`frontend/src/features/research-context/AtomicFactsPanel.tsx`、`frontend/src/features/research-context/__tests__/atomic-facts.test.ts`、`frontend/src/features/stock-research/StockResearchWorkspace.tsx`、`frontend/src/features/stock-research/detailSourceContext.ts`、`frontend/src/features/stock-research/useStockDetailActions.ts`、`frontend/src/features/stock-research/stockDetailNavigation.ts`（新增）、`frontend/src/features/stock-research/__tests__/stockDetailNavigation.test.ts`（新增）、`frontend/src/pages/StockDetailPage.tsx`、`frontend/src/pages/__tests__/detailNavigation.test.ts`、`frontend/src/pages/detailNavigation.ts`、`frontend/src/styles/global.scss`、`docs/current/02-data-api-contracts.md`、`docs/current/04-frontend-ux.md`、`docs/current/05-testing-acceptance.md`、`docs/current/07-atomic-fact-contract-v1.md`、`docs/current/code-doc-alignment.md`、`AGENTS.md`、`docs/changes/CHANGELOG.md`、`docs/changes/records/CHANGE-20260716-006.md` |

## 规则

- 当前设计直接写现在确认的状态；
- 历史前后差异写入 CHANGE；
- 编码前建立记录，完成后补全真实分支、Commit、测试和遗留事项；
- 纯样式、测试、配置、性能、依赖和死代码清理同样需要记录；
- 未产生 Head Commit 时可以写“导入提交后填写”，但合并前必须补全。
