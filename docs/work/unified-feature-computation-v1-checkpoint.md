# Unified Feature Computation V1 — Checkpoint

> 上下文压缩后只读本文件 + `git status/log`，禁止从头搜索和复述。

## 分支与基线

- branch: `refactor/unified-feature-computation-v1`
- base: `origin/main@9ba3fa8` (ab87c61 的后继)
- Phase 4 HEAD: `a0a11d8`
- Phase 5 HEAD: `db61601`
- Phase 6 HEAD: 待 commit（working tree 有 Phase 6 修正）

## 已完成 Phase 及 commit

| Phase | commit | 内容 |
|-------|--------|------|
| 1-2 | `cf06490` | CHANGE 骨架 + event_freshness_service 纯函数 (SMC 18项/Node方向/BB方向) + precomputed_dsa_bundle |
| 3 | `66907be` | MarketFeatureComputationService + batch_latest_events + DSA call-count 验证 |
| 4 | `a0a11d8` | migration 068 + event_freshness_payload JSONB + _SCHEMA_VERSION=5 + 9 项定向测试 |
| 5 | `db61601` | computing_features 状态机收敛 + MFCS 接入盘后编排 + compute-once + 批次事件预取 + 组合质量门禁 + 空壳修正 + 26 项定向测试 + orchestrator 测试适配 |
| 6 | 待 commit | 7 项缺口修正：MDAS 1d 去重（precomputed_daily_bars）+ payload 空壳/unavailable reason 校验 + 1161 行真实 DB 验证测试 |

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
根盘 free: 45.1 GiB (threshold > 20) — 已清理 .mypy_cache/.pytest_cache/.ruff_cache + /tmp/panji-feature-v1，恢复 Phase 5 开始基线
Swap used: 4 MiB
```

## 禁止项提醒

- 禁止 git add . / -A / -u
- 禁止 docker *prune -a / volume prune
- 禁止构建镜像/部署/生产 migration/merge
- 禁止全仓 pytest / Playwright / 前端全套 / 飞书 E2E
- 禁止模块自测/import 检查当成 Phase 完成证据
