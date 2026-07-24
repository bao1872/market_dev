# Unified Feature Computation V1 — Checkpoint

> 上下文压缩后只读本文件 + `git status/log`，禁止从头搜索和复述。

## 分支与基线

- branch: `refactor/unified-feature-computation-v1`
- base: `origin/main@9ba3fa8` (ab87c61 的后继)
- Phase 4 HEAD: `a0a11d8`
- Phase 5 HEAD: `db61601`
- Phase 6 HEAD: `212c88a`
- Phase 7 基线（文档对齐源头）: `ebf1eb9`

## 已完成 Phase 及 commit

| Phase | commit | 内容 |
|-------|--------|------|
| 1-2 | `cf06490` | CHANGE 骨架 + event_freshness_service 纯函数 (SMC 18项/Node方向/BB方向) + precomputed_dsa_bundle |
| 3 | `66907be` | MarketFeatureComputationService + batch_latest_events + DSA call-count 验证 |
| 4 | `a0a11d8` | migration 068 + event_freshness_payload JSONB + _SCHEMA_VERSION=5 + 9 项定向测试 |
| 5 | `db61601` | computing_features 状态机收敛 + MFCS 接入盘后编排 + compute-once + 批次事件预取 + 组合质量门禁 + 空壳修正 + 26 项定向测试 + orchestrator 测试适配 |
| 6 | `212c88a` | 7 项缺口修正：MDAS 1d 去重（precomputed_daily_bars）+ payload 空壳/unavailable reason 校验 + 1161 行真实 DB 验证测试 |
| 7 | 待提交（单一文档 commit） | 文档/合同/记忆对齐：current 6 文档 + maps 5 文档 + contracts 2（新增 feature-event-freshness v1 + 升级 after-close-recovery v3）+ INDEX + CHANGELOG + CHANGE 记录「文档更新」节 + checkpoint 执行纪律；未触碰生产代码/测试/migration/前端/门户/权限/飞书/Capture |

## Phase 5 交付摘要

**状态机收敛**: `queued → refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → succeeded`
- 旧 4 步 (creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot) 收敛为 `computing_features`
- 旧 enum 保留用于历史 run 兼容读取
- 新 run 不生成旧步骤名

**compute-once**: 每股 MDAS 1d=1, 15m=1, DSA=1, SMC=1, Node=1
- 同一结果供 StrategyResult + continuous snapshot + event freshness
- scheduled DSA inline claim（防止 worker 领取）
- manual DSA 继续走原 worker 路径

**批次事件预取**: `prefetch_monitor_events` 整批 SQL=1

**组合质量门禁**: DSA + continuous + event freshness 任一失败不 publish
- continuous: failure_rate > threshold → RuntimeError
- event freshness: require_event_freshness=True → ValueError on None/空壳
- DSA: _check_quality_gates 检查 run.status

**空壳修正**: 正式 full scope 禁止 event_freshness_payload=None 或缺少定义键

## Phase 6 交付摘要

**7 项缺口修正**（直接证据，非 mock-only）：

1. **MDAS 1d 去重**: `NodeClusterInputProvider.get_inputs` 新增 `precomputed_daily_bars` 参数；MFCS `_compute_node_cluster` 传入 `_read_daily_bars` 结果，1d 读取 1→1（原 2 次），15m 仍 1 次。直接 spy `MarketDataAggregationService.get_bars` 验证。
2. **payload 空壳/unavailable reason 校验**: `_validate_event_freshness_payload` 增加 None 检查、smc/monitor_interaction 空骨架检查、unavailable 事件必须有 reason 检查。
3. **真实 SQL=1**: SQLAlchemy `before_cursor_execute` listener 计数，10 股票 + 多 StrategyEvent 验证 batch prefetch SQL=1。
4. **manual DSA 隔离**: 验证 manual StrategyRun 仍由 worker 领取，不被 after_close inline claim。
5. **admin 状态兼容**: 新 run 显示 computing_features；旧 creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot 可读取展示。
6. **interruption/resume 幂等**: 批次1成功→批次2中断→新 lease_epoch 恢复→批次1不重复计算/不重复写→旧 lease_epoch 被拒绝→最终 publish 一次。
7. **三类门禁**: DSA/continuous/event freshness 任一失败 → run 不 published、snapshot run 不 succeeded、parent after-close run 不 succeeded、published pointer 不变、error_code 区分来源。

**被修改旧测试语义保持**: `test_phase5_computing_features.py::test_validation_rejects_wrong_schema_version` 原 semantic = "schema_version 不匹配被拒绝"；Phase 6 增加空骨架检查后，原空壳 payload 会先被空骨架检查拦截而无法到达 schema_version 检查，故补非空 smc+monitor_interaction 使其仍到达 schema_version 检查，原断言 `match="schema_version 不匹配"` 保留不变。

## Phase 7 交付摘要

**阶段定性**：文档/合同/记忆对齐，零代码改动。基线 `ebf1eb9`、working tree clean 进入；以单一文档 commit 收口。

**对齐范围**（依据"修改矩阵"前置确认，定向过期内容才改）：
1. **Current 系统事实**（6 文档）：MANIFEST（基线头 `ebf1eb9` + 规则 16 新鲜度）/ 01-system-architecture（四链消费 computing_features）/ 02-data-api-contracts（`_SCHEMA_VERSION` 4→5 + `event_freshness_payload` + `compute_for_trade_date_with_mfcs`）/ 03-jobs-integrations-operations（旧四阶段收敛为 computing_features + computing_features 内部 checkpoint + 旧 enum 兼容映射 + admin 时间线展示新状态序列 + syncing_boards 软失败 + wencai 同步定位修正）/ 05-testing-acceptance（函数名+测试引用）/ 07-atomic-fact-contract-v1（schema v5 + event freshness 不进 AFC Core 14）
2. **Maps 导航**（5 文档）：backend-module-map / test-coverage-map / frontend-route-map / worker-job-map / indicator-computation-map（盘后链 schema_version=3→5 + 新增 CHANGE-20260724-002 changelog 条目）
3. **Contracts 合同**（2）：
   - 新增 `feature-event-freshness.schema.json` v1（`event_freshness_payload` JSONB 结构 + 8 条 invariants + 不进 AFC Core 14 + 组合门禁）
   - 升级 `after-close-recovery.schema.json` v2→v3（`state_machine` 节点 + resume_path enum 约束 + 3 条新 invariants + sequence 列出新状态序列 + 增补 tests）
4. **索引与门禁**：INDEX 新增 feature-event-freshness 条目 + after-close-recovery 描述更新 v3；CHANGELOG 2026-07-24 增 CHANGE-20260724-002 索引项；ci-gates.md 扫描无相关过期内容（无需修改）
5. **CHANGE 记录 + checkpoint**：CHANGE-20260724-002.md「文档更新」节填充完整清单与未修改项确认；checkpoint 增 Phase 7 行 + 交付摘要 + 执行纪律 7 条

**未触碰边界**（严格遵守）：生产代码 / 测试代码 / 数据库 migration / 前端 / 门户 / 权限 / 飞书 / Capture；未构建镜像、未部署、未生产 migration、未 merge、未 push。

**未修改确认**（无相关过期内容）：
- `AGENTS.md`（schema_version bump 概念已存在）
- `docs/current/06-research-feature-matrix.md`（仅引用表名 `stock_feature_snapshots`，仍正确）
- `docs/current/08-indicator-calculation-contracts.md`（盘后链名 `feature_snapshot / after_close` 作为链路语义保留，非状态机步骤名）
- `docs/current/code-doc-alignment.md`（ALIGN-XXX 历史记录引用各 CHANGE 当时实现状态，属历史快照不修改）

**最小验证**（未运行 pytest）：
1. JSON schema 可被 `python -B -c "import json; json.load(open(...))"` 解析：`feature-event-freshness.schema.json` 与 `after-close-recovery.schema.json` 均通过
2. grep 确认新状态机 `queued → refreshing_daily → syncing_boards → checking_coverage → computing_features → publishing → succeeded` 已写入目标文档（03-jobs / worker-job-map / after-close-recovery.schema.json / CHANGE-20260724-002 / CHANGELOG / checkpoint / indicator-computation-map）
3. grep 确认 schema v5 已写入目标文档（02-data-api-contracts / 07-atomic-fact-contract / backend-module-map / indicator-computation-map / feature-event-freshness.schema.json / CHANGELOG / CHANGE 记录）
4. grep 确认 current/maps 中不再把旧 4 步（creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）写成当前主流程（仅作为历史 enum 兼容映射说明保留）
5. `git diff --check` 通过（无空白错误）
6. 修改文件列表精确匹配预期（14 文档 + 1 新建 schema = 15 文件）

## 已通过定向测试（Phase 2-6 + orchestrator 合并命令）

```bash
cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test \
  NODE_OPTIONS=--max-old-space-size=1536 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_event_freshness_service.py tests/test_market_feature_computation_service.py \
  tests/test_phase4_v5_snapshot_event_freshness.py tests/test_phase5_computing_features.py \
  tests/test_phase6_logic_verification.py tests/test_after_close_orchestrator.py --no-header -p no:cacheprovider -q
```
- Phase 2: 28 passed
- Phase 3: 11 passed
- Phase 4: 9 passed
- Phase 5: 26 passed
- Phase 6: 新增 test_phase6_logic_verification.py（7 类缺口验证）
- Orchestrator: 28 passed
- **第二轮合并（Phase 2-6）: 120 passed**
- **第一轮（Phase 6 + orchestrator）: 46 passed**

## 质量检查

- ruff: Phase 6 修改文件 + 新测试文件 → All checks passed
- mypy: 3 个生产文件 → 0 新增错误（1 个历史债务 redis_client.py:70 aclose）

## 关键路径（已确认，勿重复搜索）

- migration 目录: `backend/alembic/versions/`（不是 `backend/migrations/versions/`）
- 测试库 URL: `postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test`
- 测试库容器: `trading-postgres-test` (port 5433, user=bz, db=bz_stock_test)
- 生产库容器: `trading-postgres` (port 5432)
- 临时目录: `/tmp/panji-feature-v1`
- 环境变量: `APP_ENV=test COMPOSE_PARALLEL_LIMIT=1 NODE_OPTIONS=--max-old-space-size=1536 PYTHONDONTWRITEBYTECODE=1`

## Phase 5 修改文件

**生产代码**:
- `backend/app/services/after_close_orchestrator.py` — computing_features 状态机 + inline DSA claim + 组合质量门禁
- `backend/app/services/feature_snapshot_service.py` — compute_for_trade_date_with_mfcs + _validate_event_freshness_payload + _build_and_collect_strategy_result
- `backend/app/services/market_feature_computation_service.py` — prefetch_monitor_events + node_input + monitoring_event_context

**测试**:
- `backend/tests/test_phase5_computing_features.py` — 26 项定向测试（新增）
- `backend/tests/test_after_close_orchestrator.py` — mock 目标从 compute_for_trade_date 改为 compute_for_trade_date_with_mfcs + 步骤断言改为 computing_features
- `backend/tests/test_market_feature_computation_service.py` — node_input 字段适配

## Phase 6 修改文件

**生产代码**:
- `backend/app/services/node_cluster_input_provider.py` — 新增 `precomputed_daily_bars` 参数，复用 MFCS 已读日线 bars，避免 1d MDAS 重复读取；重算 source_bar_hash / adj_factor_hash 保证四链一致
- `backend/app/services/market_feature_computation_service.py` — `_compute_node_cluster` 传入 `bars_daily` 给 NodeClusterInputProvider
- `backend/app/services/feature_snapshot_service.py` — `_validate_event_freshness_payload` 增加 None / 空骨架 / unavailable reason 校验

**测试**:
- `backend/tests/test_phase6_logic_verification.py` — 1161 行，7 类缺口验证（直接 MDAS spy / 真实 PG 往返 / SQLAlchemy listener / manual DSA / admin 兼容 / interruption-resume / 三类门禁）
- `backend/tests/test_phase5_computing_features.py` — `test_validation_rejects_wrong_schema_version` 补非空 smc+monitor_interaction 以到达 schema_version 检查

## 资源基线 (Phase 6 结束)

```
MemAvailable: 3666 MiB (threshold > 3072)
根盘 free: 45.1 GiB (threshold > 20) — 已清理本任务确认生成的 mypy/pytest/ruff 缓存和 /tmp/panji-feature-v1；最终根盘空间恢复至约 45.1GiB、接近阶段基线。此前全部空间波动未逐项完整量化。
Swap used: 4 MiB
```

## 执行纪律（Phase 6 经验总结，后续 Phase 必须遵守）

1. **资源基线命令只执行一次**：`free -m` / `df -B1 /` / `docker system df` / `git count-objects -vH` 一组命令只在阶段开始执行一次；只有阶段结束或首次失败时才能重复。
2. **写测试前先读合同**：必须先读取目标函数签名、相关模型字段、现有 fixture，再写测试；不得一次生成超大综合测试文件后反复修补。
3. **禁止超大综合测试文件**：按"计算次数、持久化、SQL、恢复、门禁"等主题拆分或至少使用清晰 fixture/helper，避免重复 setup。
4. **编辑后立即确认**：编辑后必须立即用精确 grep/diff 确认；发现修改未生效时先读取目标行，不得反复盲目替换。
5. **checkpoint 不记录自身未知 SHA**：checkpoint 不记录其自身尚未知的 commit SHA；实际 SHA 放在最终报告中，避免为补 SHA 产生第二个微型 commit。
6. **mock 不能替代真实验证**：mock 调用次数不能替代真实数据库、真实 MDAS 入口或真实持久化验证。
7. **链路证据不足**：模块自测、import 成功、字符串透传和 ContextVar 存在都不能单独作为业务链路完成证据。

## 禁止项提醒

- 禁止 git add . / -A / -u
- 禁止 docker *prune -a / volume prune
- 禁止构建镜像/部署/生产 migration/merge
- 禁止全仓 pytest / Playwright / 前端全套 / 飞书 E2E
- 禁止模块自测/import 检查当成 Phase 完成证据
