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
ssh panji-prod "docker exec trading-backend python -c \"
from datetime import date
from app.services.calendar_service import get_latest_trade_date
print('latest_trade_date:', get_latest_trade_date())
\""

# 1.2 确认无活跃盘后任务
ssh panji-prod "curl -s -H 'Authorization: Bearer <admin_token>' http://localhost:8000/api/admin/after-close-runs | python -m json.tool | head -50"

# 1.3 确认日线覆盖率
ssh panji-prod "docker exec trading-backend python -c \"
from app.services.bars_coverage_service import BarsCoverageService
cov = BarsCoverageService.compute_daily_coverage(date.today())
print('daily_coverage:', cov)
\""

# 1.4 确认 worker 心跳
ssh panji-prod "docker logs --tail 20 trading-worker 2>&1 | grep heartbeat"

# 1.5 确认资源
ssh panji-prod "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' && free -h | head -2"
```

## 2. 创建完整盘后任务

通过正常 admin API 创建一条 `full` 任务（不使用 `dsa_only`，不使用临时脚本）：

```bash
ssh panji-prod "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'Content-Type: application/json' http://localhost:8000/api/admin/after-close-runs -d '{}' | python -m json.tool"
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
ssh panji-prod "watch -n 30 'curl -s -H \"Authorization: Bearer <admin_token>\" http://localhost:8000/api/admin/after-close-runs/<job_run_id> | python -m json.tool | grep -E \"status|last_completed_step|progress\"'"

# 3.2 监控 worker 日志
ssh panji-prod "docker logs --tail 50 trading-worker 2>&1 | tail -20"

# 3.3 监控资源
ssh panji-prod "free -h | head -2 && docker stats --no-stream | head -10"
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
ssh panji-prod "curl -s -X POST -H 'Authorization: Bearer <admin_token>' -H 'Content-Type: application/json' 'http://localhost:8000/api/admin/after-close-runs/force?restart_from=daily_ready' -d '{}' | python -m json.tool"
```

**前提**：日线覆盖率 ≥ 90%。仅 admin 可用。

## 安全边界

- 禁止在容器内临时拼 Python 创建任务。
- 禁止直接修改生产数据库任务 metadata。
- 禁止 DELETE 历史 `dsa_only` 记录；通过正式 cancel/interrupted/retry 服务处理。
- 禁止关闭或重启 worker 容器以"重置"任务。
- 禁止启动 nohup 临时脚本轮询任务状态。
