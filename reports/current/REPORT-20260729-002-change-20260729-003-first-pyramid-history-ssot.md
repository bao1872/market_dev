# REPORT-20260729-002 — CHANGE-20260729-003 第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦

---

## 0. Report Metadata

- Report ID: REPORT-20260729-002-change-20260729-003-first-pyramid-history-ssot
- Status: PARTIAL
- Report Type: architecture + behavior + bugfix
- Environment: TRAE CN (Local Native)
- Created At: 2026-07-29 (America/New_York)
- Branch: dev
- Upstream/Base: origin/dev
- Base SHA: e5eb40e9b5dc0d50a45b812dfb000d4c14ce447c
- Implementation SHA: 20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08
- Report Published Through SHA: 20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08
- CHANGE: CHANGE-20260729-003
- Related Task: 用户指令"第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦"
- Previous Report: REPORT-20260729-001-change-20260728-010-p0-fix
- Supersedes: 无

---

## 1. User Request

执行 `ref/instruction.md` "第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦"。只在当前 `dev` 分支修改，完成后一个功能提交并 push `origin/dev`；禁止 main、PR、腾讯云部署、生产库、数据库备份、全市场历史回补和筛选器上线。

核心目标：
1. 修复派生逻辑：DSA 量能 mean/mean、Rope 段内 expanding 占比、SMC OB 三事件生命周期、SQZ_RELEASE 方向与前置挤压量、regime_strength 读取错误。
2. 增加筛选器所需个股原子输出：trend_transition、SMC 每日状态、动量状态、聚合有效性（严禁依赖筹码）。
3. 历史 SSOT：新增 `compute_first_pyramid_history`，单股一次计算多日输出。
4. 核心与筹码彻底解耦：拆分 first_pyramid_service；feature_snapshot_service 新增 review core 路径；after_close_orchestrator 关键路径改为 core 发布门禁；chip 独立 job 不阻塞主 run。
5. 纯单元测试（`PURE_UNIT_TEST=1`），禁止连接正式库或持久测试库。
6. 文档：PRD/Maps/Changes/Rules 更新；不新建重复文档。

---

## 2. Scope

### Included

- DSA 派生逻辑修复：mean/mean ratio、Rope 段内 expanding 占比、`trend_transition` 枚举输出。
- SMC OB 生命周期：`OB_CREATED` / `OB_ENTERED` / `OB_MITIGATED` 三状态不可变事件；可选 `state_timeline`；顶层 `swing_bias` / `internal_bias` / `active_*_ob_count`。
- SQZMOM：`build_momentum_history` 输出 `daily_state` / `sqz_release_events` / `momentum_zero_cross_events`；SQZ_RELEASE 方向按当日值；释放量比计算前置挤压区间均量。
- 第一金字塔拆分：`compute_first_pyramid_core_snapshot` / `compute_chip_consensus_snapshot` / `assemble_first_pyramid_view` / `compute_first_pyramid_history`；保留 `compute_first_pyramid_snapshot` 兼容包装。
- feature_snapshot_service 新增 `compute_review_core_for_trade_date`：daily-core only，禁止 Node Cluster / 15m。
- `after_close_chip_consensus_service`：独立 job 接口/状态合同；执行实现为下一阶段 blocker。
- `structural_factor_service`：消费 DSA 权威字段，不再 `visual_segments` 复制计算。
- 新增 26 项纯单元测试 `test_change_20260729_003.py`；修正 3 项既有测试（`test_first_pyramid_contract.py` / `test_dsa_bundle_consistency.py`）。
- 新增 CHANGE-20260729-003 文档；更新 `docs/changes/INDEX.md`；`rules/20-market-data-indicators.md` 补长期硬规则。

### Excluded

- chip 持久化 migration（列为下一阶段唯一 blocker）。
- `after_close_orchestrator` 关键路径实际切换到 `compute_review_core_for_trade_date`（等待 chip migration 完成后统一切换）。
- 全市场历史数据读取/回填；筛选器上线。
- CI / main PR / 腾讯云部署；浏览器真实链路验收（待用户手工）。
- 数据库备份、生产库写入、持久测试库创建。

---

## 3. Starting State

- 当前分支：`dev`
- 当前 HEAD：`e5eb40e`（与 origin/dev 一致，FF_OK）
- 工作区状态：clean（本轮所有修改为本轮新增）
- 前置依赖：CHANGE-20260728-010 P0 修复已合入

---

## 4. Actions Performed

1. **一次性 checkpoint**：读取 AGENTS.md、rules/20/40/50/70/80、相关 PRD/Map/最近 CHANGE；确认仓库路径、dev HEAD、git status、Python/Node 工具、Backend/Frontend/Capture/Tunnel 进程端口；记录 df -h、du -sh、vm_stat、memory_pressure。
2. **建立修改矩阵**：核对 dsa_selector → structural_factor_service → smc_pine_core / sqzmom_lb → first_pyramid_service / schema → feature_snapshot_service → after_close_orchestrator 真实调用链。
3. **DSA 派生修复**（`dsa_selector.py`）：
   - 量能：`current_segment_volume_mean` / `prev_segment_volume_mean` / `current_vs_prev_volume_mean_ratio`（volume+amount 同口径）。
   - Rope 方向占比：`rope_dir1_pct` 段内 expanding 计数/段长。
   - `trend_transition` 字符串枚举输出。
4. **SMC OB 生命周期**（`smc_pine_core.py`）：保留 Pine 公式与 BOS/CHoCH 语义；新增 `emit_timeline=True`；输出 `ob_lifecycle_events` 三状态不可变事件；顶层 `swing_bias` / `internal_bias` / `active_*_ob_count`。
5. **SQZMOM 修复**（`sqzmom_lb.py`）：`build_momentum_history` 输出 `daily_state` / `sqz_release_events` / `momentum_zero_cross_events`；direction 按当日 SQZMOM 值映射 up/down/null；释放量比从 t-1 向前取连续 sqzOn 区间均量。
6. **regime_strength 修复**（`first_pyramid_service.py`）：读取 `regime_strength`（DSA SSOT 输出），不再误读 `trend_strength`。
7. **第一金字塔拆分**（`first_pyramid_service.py` + `schemas/first_pyramid.py`）：
   - `compute_first_pyramid_core_snapshot`：core 专用，禁止 Node / 15m；`_FIRST_PYRAMID_CORE_PARAMS` 排除 Node 参数；`FIRST_PYRAMID_CORE_ALGORITHM_VERSION` 独立版本。
   - `compute_chip_consensus_snapshot`：独立 chipHash、`CHIP_CONSENSUS_ALGORITHM_VERSION` 独立版本。
   - `assemble_first_pyramid_view`：组合 core + chip。
   - `compute_first_pyramid_history`：一次计算 DSA/SMC/BB/SQZMOM/VC，输出最近 N 日 state + events（默认 N=250，include_chip=False）。
   - 保留 `compute_first_pyramid_snapshot` 兼容包装。
8. **feature_snapshot_service**（`feature_snapshot_service.py`）：新增 `compute_review_core_for_trade_date`，daily-core only；`summary_payload._review_core = True`；`node_cluster.availability = "review_core_no_chip"`。
9. **after_close_chip_consensus_service**（`after_close_chip_consensus_service.py` 新建）：`create_after_close_chip_consensus_job` 幂等创建；`execute_after_close_chip_consensus` 接口合同已定义，`raise NotImplementedError` 标记下一阶段 blocker。
10. **structural_factor_service**（`structural_factor_service.py`）：消费 DSA `factor_per_bar` 权威字段，删除 `visual_segments` 复制计算。
11. **测试**：`test_change_20260729_003.py` 26 项纯单元测试全过；`test_first_pyramid_contract.py` 字段重命名修正；`test_dsa_bundle_consistency.py` 新增 `string_keys` 处理 `trend_transition`。
12. **Ruff**：修改文件全部通过 `ruff check --no-cache`。
13. **文档**：新增 CHANGE-20260729-003；更新 `docs/changes/INDEX.md`；`rules/20` 补长期硬规则（历史点时安全、anchor/confirmed/event 分离、review core 不得依赖 Node/15m、chip 不得阻塞发布）。
14. **清理 + 提交 + push**：清理 `.pytest_cache` / `.ruff_cache` / 目标 `__pycache__`；精确 `git add`；单 commit `20fff88`；fast-forward push origin/dev 成功。

---

## 5. Files Changed

| File | Action | Purpose |
|---|---|---|
| `backend/app/schemas/first_pyramid.py` | MODIFY | 新增 `FirstPyramidCoreSnapshot` / `ChipConsensusResult`；core/chip 独立算法版本常量 |
| `backend/app/services/after_close_chip_consensus_service.py` | CREATE | 独立 chip consensus job 接口/状态合同（执行为下一阶段 blocker） |
| `backend/app/services/feature_snapshot_service.py` | MODIFY | 新增 `compute_review_core_for_trade_date`（daily-core only） |
| `backend/app/services/first_pyramid_service.py` | MODIFY | 拆分 core/chip/history；修复 regime_strength 读取；新增 core/chip hash |
| `backend/app/services/structural_factor_service.py` | MODIFY | 消费 DSA 权威字段，删除 `visual_segments` 复制计算 |
| `backend/app/strategy/selectors/dsa_selector.py` | MODIFY | mean/mean ratio、Rope 段内 expanding 占比、`trend_transition` 枚举 |
| `backend/app/strategy_assets/algorithms/features/smc_pine_core.py` | MODIFY | OB 三事件生命周期、`state_timeline`、顶层 swing/internal bias |
| `backend/app/strategy_assets/algorithms/features/sqzmom_lb.py` | MODIFY | `build_momentum_history`、SQZ_RELEASE 方向、释放量比 |
| `backend/tests/test_change_20260729_003.py` | CREATE | 26 项纯单元测试 |
| `backend/tests/test_dsa_bundle_consistency.py` | MODIFY | 新增 `string_keys` 处理 `trend_transition` |
| `backend/tests/test_first_pyramid_contract.py` | MODIFY | 字段重命名修正 |
| `docs/changes/2026/CHANGE-20260729-003-first-pyramid-history-ssot-core-chip-decoupling.md` | CREATE | 变更说明 |
| `docs/changes/INDEX.md` | MODIFY | 新增 CHANGE-20260729-003 索引行 |
| `rules/20-market-data-indicators.md` | MODIFY | 新增历史 SSOT 与核心/筹码解耦硬规则章节 |

总计 14 文件，+2416 / -154。

---

## 6. Behavior Before and After

### Before

- DSA 量能用 sum/sum 口径，段间对比不准确；`structural_factor_service` 用 `visual_segments` 复制计算。
- Rope 方向占比用完整 group 统计回填段内历史，存在未来数据泄漏。
- SMC OB 仅有"活跃 OB = OB_ENTRY"派生，无 OB_CREATED/ENTERED/MITIGATED 三状态事件。
- SQZ_RELEASE 默认上涨方向，释放量能只查最后一根，不生成逐日事件。
- `first_pyramid_service` 错读 `trend_strength`（不存在），导致 `regime_strength` 静默 None。
- 第一金字塔无历史 SSOT，筛选器回测需循环调用 snapshot。
- core 与筹码耦合：feature_snapshot_service 的 Node Cluster 计算在盘后关键路径；无独立 chip job。
- 筛选器缺少 trend_transition、SMC 每日状态、动量状态、聚合有效性等原子输出。

### After

- DSA 量能统一为 mean/mean 口径；volume/amount 同口径；`structural_factor_service` 消费 DSA 权威字段。
- Rope 方向占比按段内 expanding 计数/段长计算，无未来泄漏（前缀不变性测试保护）。
- SMC OB 输出 `OB_CREATED` / `OB_ENTERED` / `OB_MITIGATED` 不可变事件；可选 `state_timeline`；顶层 `swing_bias` / `internal_bias` / `active_*_ob_count`。
- SQZ_RELEASE direction 按当日 SQZMOM 值映射 up/down/null；释放量比从 t-1 向前取连续 sqzOn 区间均量；生成逐日事件。
- `regime_strength` 正确读取（DSA SSOT 输出）。
- `compute_first_pyramid_history` 一次计算多日输出，禁止循环调用 snapshot。
- core/chip 拆分：`compute_first_pyramid_core_snapshot` 禁止 Node/15m；`compute_chip_consensus_snapshot` 独立 hash/version；`compute_review_core_for_trade_date` 为盘后提供 daily-core only 路径；`after_close_chip_consensus` 独立 job 接口/状态合同已定义（执行实现为下一阶段 blocker）。
- 筛选器原子输出齐全，有效性只依赖趋势/结构/动量和日线完整性，严禁依赖筹码。

---

## 7. Validation

| Command or Check | Result | Exit Code | Notes |
|---|---|---|---|
| `git fetch origin dev && git status --branch --short` | HEAD=origin/dev=e5eb40e，FF_OK | 0 | Gate 0 基线 |
| `cd backend && PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_change_20260729_003.py -v -p no:cacheprovider` | 26 passed | 0 | 目标纯单元测试（首次即过） |
| `cd backend && PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_first_pyramid_contract.py tests/test_dsa_bundle_consistency.py -v -p no:cacheprovider` | passed | 0 | 既有测试修正后复跑 |
| `cd backend && .venv/bin/ruff check --no-cache app/schemas/first_pyramid.py app/services/after_close_chip_consensus_service.py app/services/feature_snapshot_service.py app/services/first_pyramid_service.py app/services/structural_factor_service.py app/strategy/selectors/dsa_selector.py app/strategy_assets/algorithms/features/smc_pine_core.py app/strategy_assets/algorithms/features/sqzmom_lb.py tests/test_change_20260729_003.py tests/test_dsa_bundle_consistency.py tests/test_first_pyramid_contract.py` | passed | 0 | 修改文件全部通过 |
| `git diff --stat`（本轮修改） | 14 files +2416/-154 | 0 | 工作区检查 |

Notes:
- 浏览器真实链路验收：待用户手工（本轮未修改前端，按指令未跑前端全量 TSC/ESLint）。
- 涉及 DB 的集成测试只列为 CI 待验，本地未运行。

---

## 8. Git Operations

- Implementation Commit: 20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08
- Metadata Commit: 与本轮报告同一提交
- Push Target: origin/dev
- Push Result: 成功 fast-forward（e5eb40e..20fff88）
- origin/dev After Implementation Push: 20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08
- Force Push Used: NO

---

## 9. Deployment Status

- NOT_REQUESTED

本轮不部署腾讯云，不运行生产脚本。

---

## 10. Database and Migration

- Database Accessed: 否（纯单元测试，`PURE_UNIT_TEST=1`）
- Migration Created: NO
- Migration Executed: NO
- Backup Created: NO
- Volume Modified: NO

chip 持久化 migration 列为下一阶段唯一 blocker，本轮未实现。

---

## 11. Risks and Known Gaps

1. **chip 持久化 migration**（下一阶段唯一 blocker）：chip 结果持久化表/migration 未实现，本轮仅完成计算边界和接口合同；`execute_after_close_chip_consensus` `raise NotImplementedError`。
2. **`after_close_orchestrator` 关键路径未切换**：未实际切换到 `compute_review_core_for_trade_date`，等待 chip 持久化 migration 完成后统一切换。
3. **浏览器真实链路验收**：待用户手工（本轮未修改前端）。
4. **CI 未处理**：按用户指令本轮不处理。
5. **集成测试待 CI 验证**：涉及 DB 的集成测试本地未运行。

---

## 12. Blockers and User Decisions

- **chip 持久化 migration**：下一阶段唯一 blocker，需用户确认后实现独立 chip 持久化表 + migration，再切换 `after_close_orchestrator` 关键路径。
- 用户决策：是否继续处理 CI、main PR、腾讯云部署（按指令本轮不做）。

---

## 13. Next Recommended Action

1. 等待用户手工浏览器验收（可选，本轮未改前端）。
2. 下一阶段：实现 chip 持久化 migration → 切换 `after_close_orchestrator` 关键路径 → 实现 `execute_after_close_chip_consensus` 执行逻辑。

---

## 14. Final Summary

- 做了什么：修复 DSA 量能/Rope 占比/SMC OB 生命周期/SQZ_RELEASE 方向/regime_strength 五项派生逻辑；新增筛选器所需个股原子输出；新增 `compute_first_pyramid_history` 历史 SSOT；拆分 core/chip 并新增盘后 review core 路径与独立 chip job 接口；新增 26 项纯单元测试；更新 CHANGE/rules/INDEX 文档。
- 没做什么：chip 持久化 migration、`after_close_orchestrator` 关键路径实际切换、CI、main PR、腾讯云部署、浏览器真实链路验收。
- 验证结果：26 项纯单元测试全过；Ruff 全过；既有测试修正后复跑通过。
- commit：20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08
- push：成功 fast-forward（e5eb40e..20fff88）
- 下一步：等待 chip 持久化 migration 下一阶段决策。
