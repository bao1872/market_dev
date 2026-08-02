# 完整盘后生产运行 Runbook

本 Runbook 描述如何在腾讯云生产环境启动一次完整盘后任务并进行有限前置监控。

## 前置条件

- 本地 dev 已合并到 main，main CI 全绿，自动部署完成。
- 生产 runtime SHA 等于 main HEAD（通过 `curl /api/v1/health` 或 `git -C /srv/panji-live rev-parse HEAD` 核验）。
- 当前时间在 A 股交易日 15:30 之后（盘后）。
- 最近完成交易日的日线数据已 ready（覆盖率 ≥ 90%）。

## 1. 只读前置确认

通过 SSH 登录生产服务器，执行只读检查：

```bash
# 1.1 确认最近完成交易日
scripts/ops/panji-prod-ssh "docker exec trading-backend python -c \"
from datetime import date
from app.services.calendar_service import get_latest_trade_date
print('latest_trade_date:', get_latest_trade_date())
\""

# 1.2 确认无活跃盘后任务
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/api/admin/after-close-runs | python -m json.tool | head -50"

# 1.3 确认日线覆盖率
scripts/ops/panji-prod-ssh "docker exec trading-backend python -c \"
from app.services.bars_coverage_service import BarsCoverageService
cov = BarsCoverageService.compute_daily_coverage(date.today())
print('daily_coverage:', cov)
\""

# 1.4 确认 worker 心跳
scripts/ops/panji-prod-ssh "docker logs --tail 20 trading-worker 2>&1 | grep heartbeat"

# 1.5 确认资源
scripts/ops/panji-prod-ssh "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' && free -h | head -2"
```

## 2. 创建完整盘后任务

通过正常 admin API 创建一条 `full` 任务（不使用 `dsa_only`，不使用临时脚本）：

```bash
scripts/ops/panji-prod-ssh "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'Content-Type: application/json' http://localhost:8000/api/admin/after-close-runs -d '{}' | python -m json.tool"
```

记录返回的 `job_run_id`。

## 3. 有限前置监控

**最多 10 分钟或直到前 5 只股票完成**，记录：
- symbol、成功/失败、耗时
- StrategyResult 计数
- worker heartbeat
- 容器重启次数
- MemAvailable

```bash
# 3.1 轮询任务状态（每 30s 一次，最多 20 次）
scripts/ops/panji-prod-ssh "watch -n 30 'curl -s -H \"Authorization: Bearer <admin_token>\" http://localhost:8000/api/admin/after-close-runs/<job_run_id> | python -m json.tool | grep -E \"status|last_completed_step|progress\"'"

# 3.2 监控 worker 日志
scripts/ops/panji-prod-ssh "docker logs --tail 50 trading-worker 2>&1 | tail -20"

# 3.3 监控资源
scripts/ops/panji-prod-ssh "free -h | head -2 && docker stats --no-stream | head -10"
```

## 4. 停止条件

若出现以下任一情况，**停止继续操作并报告**：
- 首个致命异常（traceback）
- worker 容器重启
- MemAvailable < 2 GiB

否则，10 分钟或前 5 只股票完成后，**停止前台轮询**，让独立 worker 在后台继续。**不能关闭或重启 worker**。

## 5. 交接

报告以下信息：
- 任务当前阶段（如 `refreshing_daily` / `syncing_boards` / `computing_features` / `publishing` / `succeeded`）
- `job_run_id`
- 前 5 只股票进度（symbol、成功/失败、耗时、StrategyResult 计数）
- 资源状态（MemAvailable、容器重启次数）
- 任何异常

后续状态由管理页 `/admin/after-close` 或正式 API 查看，**不启动 nohup 临时脚本**。

## 6. 从 DSA 阶段重算（可选）

若需要跳过日线刷新，从 DSA 阶段重算（仍执行完整后续链路）：

```bash
scripts/ops/panji-prod-ssh "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'Content-Type: application/json' 'http://localhost:8000/api/admin/after-close-runs/force?restart_from=daily_ready' -d '{}' | python -m json.tool"
```

**前提**：日线覆盖率 ≥ 90%。仅 admin 可用。

## 安全边界

- 禁止在容器内临时拼 Python 创建任务。
- 禁止直接修改生产数据库任务 metadata。
- 禁止 DELETE 历史 `dsa_only` 记录；通过正式 cancel/interrupted/retry 服务处理。
- 禁止关闭或重启 worker 容器以"重置"任务。
- 禁止启动 nohup 临时脚本轮询任务状态。

## 增量发布 canary / resume 命令（CHANGE-20260729-008）

### 1. Migration 前置检查

```bash
# migration 只能随 docs/runbooks/development-deployment.md 的唯一入口执行。
# 本处只读验证当前 revision 和业务表，不得手工 upgrade。
scripts/ops/panji-prod-ssh "docker exec trading-backend alembic current"
scripts/ops/panji-prod-ssh "docker exec trading-postgres psql -U bz -d bz_stock -tAc \
  \"SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN (
    'stock_chip_consensus_snapshots','stock_feature_snapshot_run_items',
    'first_pyramid_history_runs','first_pyramid_history_run_items','factor_publications'
  );\""
```

### 2. History 回补 canary（5 只含深科技）

```bash
# dry-run 验证
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.first_pyramid_history_backfill_cli --canary --dry-run"

# 执行 canary（5 只 × 250 日，DB-only，include_chip=false）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.first_pyramid_history_backfill_cli --canary"

# 验证 canary 结果
scripts/ops/panji-prod-ssh "docker exec trading-postgres psql -U bz -d bz_stock -tAc \
  \"SELECT status, succeeded_count, failed_count, expected_count FROM first_pyramid_history_runs ORDER BY started_at DESC LIMIT 1;\""
```

### 3. History 回补扩大（25 只）

```bash
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.first_pyramid_history_backfill_cli --limit 25"
```

### 4. History 全市场回补（持久化运行）

```bash
# 后台运行（nohup 确保持久化，SSH 断开不中断）
scripts/ops/panji-prod-ssh 'docker exec -d trading-backend bash -c "nohup python -m scripts.first_pyramid_history_backfill_cli --all --batch-size 25 > /tmp/history-fullmarket.log 2>&1"'

# 查询进度
scripts/ops/panji-prod-ssh "docker exec trading-postgres psql -U bz -d bz_stock -tAc \
  \"SELECT status, succeeded_count, failed_count, skipped_count, expected_count,
    ROUND(100.0 * succeeded_count / NULLIF(expected_count, 0), 1) AS pct
   FROM first_pyramid_history_runs ORDER BY started_at DESC LIMIT 1;\""

# 查询 item 级进度
scripts/ops/panji-prod-ssh "docker exec trading-postgres psql -U bz -d bz_stock -tAc \
  \"SELECT status, COUNT(*) FROM first_pyramid_history_run_items
   WHERE history_run_id='<RUN_ID>' GROUP BY status;\""

# 查看日志
scripts/ops/panji-prod-ssh "docker exec trading-backend tail -50 /tmp/history-fullmarket.log"
```

### 5. History resume（续跑未完成的 run）

```bash
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.first_pyramid_history_backfill_cli --resume --history-run-id <RUN_ID>"
```

### 6. 指定股票回补

```bash
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.first_pyramid_history_backfill_cli --symbols 000001,000021,600519"
```

### 7. 增量发布状态查询（管理 API）

```bash
# 综合状态（需 admin token）
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/admin/incremental-publish/status | python -m json.tool"

# core run 进度
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/admin/incremental-publish/core/runs/<SNAPSHOT_RUN_ID>/progress | python -m json.tool"

# history run 进度
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/admin/incremental-publish/history/runs/<HISTORY_RUN_ID>/progress | python -m json.tool"

# pointer 列表
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/admin/incremental-publish/pointers | python -m json.tool"
```

### 8. 完整部署流程

> 部署只有一个入口（CHANGE-20260802-003）。migration 与健康核验均由部署脚本内部完成，
> 不需要、也**禁止**手工 sync / `git reset --hard` / 容器内 `alembic upgrade`。

```bash
# 本地执行（唯一入口，SHA 必须已在 origin/dev 上）
scripts/ops/panji-test-deploy <FULL_SHA> --dry-run   # 先看计划
scripts/ops/panji-test-deploy <FULL_SHA>             # 正式执行

# 部署脚本内部已完成：checkout → 同步 /opt/panji-live → migration（仅有新 migration 时）
#   → 重启 → /v1/health + /v1/health/ready + /v1/version 核验 → 失败自动回滚

# 独立验证（只读）
scripts/ops/panji-prod-ssh "curl -s http://127.0.0.1:8000/v1/version | python3 -m json.tool"
# 期望: runtime_git_sha=<FULL_SHA>, deployment_mode=live
```

部署失败时不得手工修补运行环境；按脚本输出的阶段/行号定位根因，修复后重新走同一入口。
详见 `docs/runbooks/development-deployment.md`。

### 9. 板块分析 V1 计算（CHANGE-20260730-011）

**前置条件**：已发布 `stock_core` pointer（盘后核心计算完成并发布）。

```bash
# 9.1 Canary（每类型 5 个板块，行业 + 概念）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --canary --publish"
# 期望输出：succeeded=10, failed=0, published>=10 (若 coverage >= 0.95)

# 9.2 全量计算（行业 + 概念，所有板块）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --all --publish"
# 期望输出：succeeded=N (所有板块), failed=0

# 9.3 限定单一类型
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --all --type industry --publish"
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --all --type concept --publish"

# 9.4 指定交易日（默认从最新 stock_core pointer 推断）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --all --trade-date 2026-07-29 --publish"

# 9.5 Dry-run（只列出板块，不写入）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --canary --dry-run"

# 9.6 只计算不发布（coverage 不足时保存 partial 但不切 pointer）
scripts/ops/panji-prod-ssh "docker exec trading-backend python -m scripts.board_analysis_cli --all --no-publish"
```

**验证**（API + 数据库）：

```bash
# API 列表（需 admin token，任何登录用户可读）
scripts/ops/panji-prod-ssh "curl -s 'http://127.0.0.1:8000/api/v1/boards/analysis?type=industry&page=1&page_size=5' -H 'Authorization: Bearer <token>' | python -m json.tool"

# 数据库直查
scripts/ops/panji-prod-ssh 'docker exec trading-postgres psql -U bz_stock -d bz_stock -c "
  SELECT board_type, board_name, status, coverage_ratio, ready_count, eligible_count, missing_count
  FROM board_analysis_snapshots
  WHERE trade_date = (SELECT MAX(trade_date) FROM board_analysis_snapshots)
  ORDER BY board_type, coverage_ratio DESC
  LIMIT 10;"'

# 发布指针
scripts/ops/panji-prod-ssh 'docker exec trading-postgres psql -U bz_stock -d bz_stock -c "
  SELECT scope_key, trade_date, coverage_ratio, algorithm_version, published_at
  FROM factor_publications
  WHERE scope_type = '\''board'\'' AND publication_kind = '\''market_aggregation'\''
  ORDER BY trade_date DESC LIMIT 10;"'
```

**门禁**：
- `coverage_ratio >= 0.95` 才写入 `factor_publications` pointer
- 不足时保存 `partial` 结果但不发布（可重复计算，幂等）
- 退市股（`Instrument.status != 'active'`）不参与聚合，不进入 `eligible_count`

## Review canary / resume / publish

**状态：尚未实现（复盘模块待开发）**
对应 PRD：`../prd/70-review.md` §11/§12.6/§19.4；对应 Map：`../maps/70-review.md`

> 以下为复盘模块计划的操作步骤，当前 `/api/v1/admin/review/*` 路由不存在。待 Phase 1-2 实现后可用。

### 前置条件

- `stock_core` pointer 已发布（盘后核心计算完成）；
- `board_analysis` pointer 已发布（板块分析 V1 完成且 coverage >= 0.95）；
- review migration（建议 `075_market_review_workbench.py`）已应用。

### 1. Canary（小范围验证）

PRD §19.4 固定 canary 范围：全市场 + 2 个主要指数 + 2 个风格范围 + 5 个一级行业。

```bash
scripts/ops/panji-prod-ssh "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'Content-Type: application/json' -H 'X-Idempotency-Key: <unique_key>' http://localhost:8000/api/v1/admin/review/runs -d '{\"trade_date\":\"<YYYY-MM-DD>\",\"scope\":\"canary\"}' | python -m json.tool"
```

验证项（PRD §19.4）：

- P/Q/U/C/V 值可复算；
- 至少一条正向和一条风险信号；
- 下钻路径和成员归因一致；
- `/market` 与 `/stock` 跳转正确；
- 次日 tracking 状态可重复计算。

### 2. Resume（恢复未完成 run）

仅处理 pending / 可重试 failed / 过期 running 的 item，不重算已 succeeded 且 input_hash + version 一致的 item。

```bash
scripts/ops/panji-prod-ssh "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'X-Idempotency-Key: <unique_key>' http://localhost:8000/api/v1/admin/review/runs/<run_id>/resume | python -m json.tool"
```

### 3. Publish（发布 pointer）

原子切换 review pointer，不重算。发布前检查整套门禁（PRD §11.1）。

```bash
scripts/ops/panji-prod-ssh "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'X-Idempotency-Key: <unique_key>' http://localhost:8000/api/v1/admin/review/runs/<run_id>/publish | python -m json.tool"
```

### 4. 状态查询

```bash
scripts/ops/panji-prod-ssh "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/api/v1/admin/review/runs/<run_id>/status | python -m json.tool"
```

### 安全边界

- 所有写操作（create / resume / publish）必须携带幂等键（`X-Idempotency-Key`）；
- pointer 切换失败只重试发布，不重算；
- 不得直接修改 `market_review_runs` 或 `factor_publications` 表的 metadata；
- 不得绕过发布门禁强制发布 partial 结果。
