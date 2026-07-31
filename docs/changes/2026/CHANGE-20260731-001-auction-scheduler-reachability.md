# CHANGE-20260731-001：Auction Scheduler 真实可达 + 09:25 数据源审计

状态：**仍 BLOCKED**（AUCTION_DATA_SOURCE_BLOCKED + BLOCKED_NO_STAGING + CI 终态未确认）
日期：2026-07-31
类型：behavior + architecture + data
领域：竞价分析 / 盘后编排 / 生产运行

## 1. 背景

上一轮（CHANGE-20260730-018）实现了竞价分析完整链路，但 `worker.py` 中 `WORKER_TYPE=auction_scheduler` 分支无人启动，`docker-compose.prod.yml` 未配置该 Worker 类型，导致 09:25 和 10:00 任务部署后仍不可达。

本轮按指令修复 P0：将 Auction Scheduler 接入现有 `after_close_orchestrator` Worker 进程，并审计 09:25 数据源，判断是否具备 staging 条件。

## 2. 修改内容

### 2.1 Scheduler Co-process 接入（`backend/app/worker.py`）

**修改前**：`run_after_close_orchestrator_worker()` 主循环中，Auction 轮询仅在 core/chip 未领取到任务时执行（`if not claimed: await _auction_scheduler_poll_once()`），可能被 core/chip 阻塞错过 09:25/10:00 窗口。`WORKER_TYPE=auction_scheduler` 分支存在但生产无人启动。

**修改后**：
- `run_after_close_orchestrator_worker()` 启动时通过 `asyncio.create_task(_run_auction_scheduler_co_process())` 启动同进程 Auction co-process
- `_run_auction_scheduler_co_process()` 独立循环：
  1. 启动时调用 `recover_stale_scheduler_job_runs(db)` 清理崩溃残留
  2. 每 `AUCTION_SCHEDULER_POLL_INTERVAL`（30s）独立轮询 09:25/10:00 触发窗口和 queued auction jobs
  3. 异常 try/except 隔离，不影响主 Worker
  4. 共享全局 `_shutdown` 标志，SIGTERM 时退出
- 主 Worker `finally` 块 `await` co-process 退出（超时 35s 后 cancel）
- **不新增容器**：复用 `worker-after-close`（`WORKER_TYPE=after_close_orchestrator`）
- `WORKER_TYPE=auction_scheduler` 分支保留仅用于本地调试

### 2.2 目标测试（`backend/tests/test_auction_scheduler_worker.py`）

新增 29 个纯单元测试，覆盖：
1. 架构守护：Compose `worker-after-close` 使用 `WORKER_TYPE=after_close_orchestrator`
2. 09:25 窗口仅创建一个 `auction_final` 任务
3. 10:00 窗口仅创建一个 `auction_open_confirmation` 任务
4. 同一分钟多次 poll 不重复创建
5. Worker 错过精确时间但补偿窗口内可创建
6. 非交易日不创建
7. Worker 重启后 succeeded/running 任务不重复
8. 过期租约 fencing 恢复
9. Auction 轮询异常不终止主 Worker
10. SIGTERM 结束 co-process Task

### 2.3 Mypy 基线对齐（`auction_scan_service.py` / `board_analysis_service.py`）

- `auction_scan_service.py`：`cast(_MockBar, list[BarDaily])` 解决 `list-item`
- `board_analysis_service.py`：拆分 `pl.get("trend_strength")` 链式调用解决 `union-attr`
- 无业务行为变更

## 3. 09:25 数据源审计（生产只读，2026-07-31）

| 检查项 | 结果 |
|---|---|
| `bars_minute` 总行数 | **0**（空表） |
| `bars_15min` 最早 trade_time | 09:45:00 |
| `auction_*` 表 | 不存在（生产 c56d991 未迁移 077） |
| 最近 20 交易日 `bars_daily` | 5190-5193 股/日，close/volume/amount 100% 非空 |
| 活跃 A 股 | 5196 只（SH 2300 + SZ 2896） |

**结论**：`AUCTION_DATA_SOURCE_BLOCKED`。生产无 09:25 最终竞价数据源，所有 auction_final 扫描会返回 coverage=0。

**下一阶段方案**（不在本轮伪造）：
- 建立 `auction_final_quotes` 表（trade_date + instrument_id + 09:25 close/volume/amount + `is_final_auction` 标记）
- 接入现有行情 Provider 的最终竞价 DTO（Capture Worker 09:25:05 后写入）
- 禁止用 09:30 第一根分钟线替代 09:25

## 4. CI 终态

- gh CLI 未认证，无法读取 GitHub Actions 日志
- 本地已通过：ruff、mypy baseline（无新增）、29 个 scheduler worker 单测、tsc 0 错误
- 推送 SHA：b89a3d3
- 待用户认证 `gh auth login` 后确认 CI 全绿

## 5. Staging 环境

- 生产服务器仅有单套容器（trading-*，运行 c56d991）
- 无隔离 dev/staging 容器、独立数据库或独立域名
- 标记 `BLOCKED_NO_STAGING`

## 6. 最终状态

**BLOCKED_AUCTION_DATA**（同时存在 BLOCKED_NO_STAGING + CI 未确认）

未达到 `READY_FOR_STAGING`。阻塞项：
1. 生产无 09:25 数据源（根本阻塞）
2. 无隔离 staging 环境
3. CI 终态未确认（gh 未认证）

## 7. 关联文档

- `docs/maps/75-auction-analysis.md` §3.4.1、§7
- `docs/runbooks/auction-analysis.md` §1.1、§1.2、§2.4、§2.5
- `docs/maps/80-system-runtime.md` §7
- `rules/70-trae-cn.md` §macOS 内存规则（已对齐，无 Swap 百分比门禁）

## 8. 下一阶段 Canary 计划

1. 用户认证 `gh auth login`，确认 CI 全绿
2. 建立 `auction_final_quotes` 数据合同或扩展 `bars_minute` 写入 09:25 bar
3. 创建隔离 staging 环境（独立数据库 + 独立 Worker 容器 + 独立域名）
4. 应用 Migration 077/078 到 staging
5. Canary：5-10 只股票 + 1 行业 + 1 概念，验证锚点→扫描→聚合→前端全链路
6. Canary 通过后全量回填
