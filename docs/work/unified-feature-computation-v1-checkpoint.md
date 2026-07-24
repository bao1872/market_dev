# Unified Feature Computation V1 — Checkpoint

> 上下文压缩后只读本文件 + `git status/log`，禁止从头搜索和复述。

## 分支与基线

- branch: `refactor/unified-feature-computation-v1`
- base: `origin/main@9ba3fa8` (ab87c61 的后继)
- Phase 4 HEAD: `a0a11d8`
- Phase 5 HEAD: 待 commit（working tree 有 Phase 5 修改）

## 已完成 Phase 及 commit

| Phase | commit | 内容 |
|-------|--------|------|
| 1-2 | `cf06490` | CHANGE 骨架 + event_freshness_service 纯函数 (SMC 18项/Node方向/BB方向) + precomputed_dsa_bundle |
| 3 | `66907be` | MarketFeatureComputationService + batch_latest_events + DSA call-count 验证 |
| 4 | `a0a11d8` | migration 068 + event_freshness_payload JSONB + _SCHEMA_VERSION=5 + 9 项定向测试 |
| 5 | 待 commit | computing_features 状态机收敛 + MFCS 接入盘后编排 + compute-once + 批次事件预取 + 组合质量门禁 + 空壳修正 + 26 项定向测试 + orchestrator 测试适配 |

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

## 已通过定向测试（Phase 2-5 + orchestrator 合并命令）

```bash
cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test \
  NODE_OPTIONS=--max-old-space-size=1536 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_event_freshness_service.py tests/test_market_feature_computation_service.py \
  tests/test_phase4_v5_snapshot_event_freshness.py tests/test_phase5_computing_features.py \
  tests/test_after_close_orchestrator.py --no-header -p no:cacheprovider -q
```
- Phase 2: 28 passed
- Phase 3: 11 passed
- Phase 4: 9 passed
- Phase 5: 26 passed
- Orchestrator: 28 passed
- **合计: 102 passed**

## 质量检查

- ruff: 5 个修改文件 + 1 个新测试文件 → All checks passed
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

## 资源基线 (Phase 5 结束)

```
MemAvailable: 3736 MiB (threshold > 3072)
根盘 free: 42 GiB (threshold > 20)
Swap used: 4 MiB
```

## 禁止项提醒

- 禁止 git add . / -A / -u
- 禁止 docker *prune -a / volume prune
- 禁止构建镜像/部署/生产 migration/merge
- 禁止全仓 pytest / Playwright / 前端全套 / 飞书 E2E
- 禁止模块自测/import 检查当成 Phase 完成证据
