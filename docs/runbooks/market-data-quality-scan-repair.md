# 行情质量扫描与修复 Runbook

本 Runbook 描述如何在腾讯云生产环境运行 `market_data_quality_cli.py` 完成 scan → repair → verification 三阶段流程，以及 canary 5 只股票的最小验证步骤。

对应 PRD：`../prd/50-market-data-quality.md`
对应 Map：`../maps/10-market-data.md`（行情质量章节）+ `../maps/30-after-close.md` §11.5

## 前置条件

- 生产服务器已部署包含 `market_data_quality_cli.py` 与 migration 075 的代码版本。
- `alembic current` 显示版本 >= `075_market_data_quality`。
- 已有 admin token（用于 API 查询；CLI 本身不需要 token，但通过 docker exec 执行）。
- 当前不在 A 股交易时段（避免与盘后任务冲突，建议 22:00 之后或周末执行）。

## 1. dry-run 验证参数解析

在任何 `--scan` / `--repair` 之前，先用 `--dry-run` 验证参数解析正确：

```bash
# 1.1 canary 5 只股票 dry-run
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --canary --dry-run"
# 期望输出：
#   mode=scan, timeframe=daily
#   canary=true
#   canary_symbols=['000001', '000008', '000021', '600519', '300750']
#   expected_items=5
#   不创建 run/items，不查询 bars_daily

# 1.2 指定股票 dry-run
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --symbols 000001,000021 --dry-run"

# 1.3 全市场 dry-run（不推荐首次执行，预计 5000+ items）
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --dry-run --verbose"
```

`--dry-run` 不创建任何 DB 记录，不修改任何行情表（PRD MQ-01）。

## 2. canary 5 只股票完整流程

### 2.1 阶段 1：scan（canary）

```bash
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --canary"
# 期望输出：
#   run_id=<SCAN_RUN_ID>
#   mode=scan, canary=true
#   succeeded=5, failed=0, skipped=0
#   classification 分布：OK / DB_MISSING / SOURCE_MISSING 等
```

记录返回的 `<SCAN_RUN_ID>`。

### 2.2 阶段 1 验证

```bash
# 查询 scan run 状态
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, status, expected_count, succeeded_count, failed_count, skipped_count, metadata_json
  FROM market_data_quality_runs
  WHERE id = '<SCAN_RUN_ID>';\""

# 查询 items 详情
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT symbol, timeframe, classification, status, gap_details
  FROM market_data_quality_items
  WHERE run_id = '<SCAN_RUN_ID>'
  ORDER BY symbol;\""

# 核验 bars_daily / bars_15min / adj_factor 未被修改（应无变化）
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT COUNT(*) FROM bars_daily WHERE updated_at > NOW() - INTERVAL '10 minutes';\""
# 期望：0（scan 不修改 bars）
```

### 2.3 阶段 2：repair（canary）

```bash
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode repair --run-id <SCAN_RUN_ID>"
# 期望输出：
#   run_id=<REPAIR_RUN_ID>
#   parent_run_id=<SCAN_RUN_ID>
#   mode=repair, canary=true
#   succeeded=N（修复的 DB_MISSING 数量）, skipped=M（非 DB_MISSING 跳过）
```

记录返回的 `<REPAIR_RUN_ID>`。

> 注意：`--repair` 必须搭配 `--run-id`（指向 scan run），CLI 会自动创建新的 repair run，并通过 `parent_run_id` 关联到 scan run（PRD MQ-03）。

### 2.4 阶段 2 验证

```bash
# 查询 repair run 状态
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, parent_run_id, status, succeeded_count, failed_count, skipped_count
  FROM market_data_quality_runs
  WHERE id = '<REPAIR_RUN_ID>';\""

# 查询修复后的 bars_daily 行数变化
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT symbol, COUNT(*) AS bars_count, MAX(trade_date) AS latest_date
  FROM bars_daily bd
  JOIN instruments i ON bd.instrument_id = i.id
  WHERE i.symbol IN ('000001', '000008', '000021', '600519', '300750')
  GROUP BY symbol
  ORDER BY symbol;\""

# 查询 adj_factor 重算情况
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT symbol, COUNT(*) AS factor_count, MAX(updated_at) AS last_updated
  FROM adj_factor af
  JOIN instruments i ON af.instrument_id = i.id
  WHERE i.symbol IN ('000001', '000008', '000021', '600519', '300750')
  GROUP BY symbol
  ORDER BY symbol;\""
```

### 2.5 阶段 3：verification scan（canary）

```bash
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode verify --run-id <REPAIR_RUN_ID>"
# 期望输出：
#   run_id=<VERIFICATION_RUN_ID>
#   parent_run_id=<REPAIR_RUN_ID>
#   mode=verification, canary=true
#   succeeded=5, failed=0
#   before=DB_MISSING → after=OK 的 items 数量
```

记录返回的 `<VERIFICATION_RUN_ID>`。

> 注意：`--verify` 必须创建新 run，禁止复用 scan 或 repair run（PRD MQ-04）。

### 2.6 阶段 3 验证

```bash
# 查询 verification run 状态
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, parent_run_id, status, succeeded_count, failed_count
  FROM market_data_quality_runs
  WHERE id = '<VERIFICATION_RUN_ID>';\""

# 比对 repair 前后的 classification 变化
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT
    v.symbol,
    v.timeframe,
    i.classification AS before_classification,
    v.classification AS after_classification
  FROM market_data_quality_items v
  JOIN market_data_quality_runs vr ON v.run_id = vr.id
  JOIN market_data_quality_runs rr ON vr.parent_run_id = rr.id
  JOIN market_data_quality_items i ON i.run_id = rr.parent_run_id
    AND i.instrument_id = v.instrument_id
    AND i.timeframe = v.timeframe
  WHERE v.run_id = '<VERIFICATION_RUN_ID>'
  ORDER BY v.symbol;\""
# 期望：DB_MISSING → OK 的 items 数量与 repair succeeded_count 一致
```

## 3. 全市场 scan/repair/verification 标准流程

canary 通过后，执行全市场流程：

### 3.1 全市场 scan

```bash
# 后台运行（nohup 确保持久化，SSH 断开不中断）
ssh panji-prod 'docker exec -d trading-backend bash -c "nohup python -m scripts.market_data_quality_cli --mode scan > /tmp/market-data-scan-fullmarket.log 2>&1"'

# 查询最新 scan run 进度
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, status, expected_count, succeeded_count, failed_count, skipped_count,
    ROUND(100.0 * succeeded_count / NULLIF(expected_count, 0), 1) AS pct,
    heartbeat_at
  FROM market_data_quality_runs
  WHERE mode = 'scan'
  ORDER BY started_at DESC LIMIT 1;\""

# 查询 item 级进度（按 classification 汇总）
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT classification, status, COUNT(*) AS count
  FROM market_data_quality_items
  WHERE run_id = '<SCAN_RUN_ID>'
  GROUP BY classification, status
  ORDER BY classification, status;\""

# 查看日志
ssh panji-prod "docker exec trading-backend tail -50 /tmp/market-data-scan-fullmarket.log"
```

### 3.2 全市场 scan resume（中断后续跑）

```bash
# 续跑未完成的 items（必须显式 --run-id）
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --resume --run-id <SCAN_RUN_ID>"
```

> 注意：`--resume` 必须搭配 `--run-id`，否则报错退出（PRD MQ-10）。resume 不创建新 run，只续跑原 run 的 pending/failed items。

### 3.3 全市场 repair

```bash
# 基于 scan run 创建 repair run
ssh panji-prod 'docker exec -d trading-backend bash -c "nohup python -m scripts.market_data_quality_cli --mode repair --run-id <SCAN_RUN_ID> > /tmp/market-data-repair-fullmarket.log 2>&1"'

# 查询 repair run 进度
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, parent_run_id, status, succeeded_count, failed_count, skipped_count,
    ROUND(100.0 * succeeded_count / NULLIF(expected_count, 0), 1) AS pct,
    heartbeat_at
  FROM market_data_quality_runs
  WHERE mode = 'repair'
  ORDER BY started_at DESC LIMIT 1;\""
```

### 3.4 全市场 repair resume

```bash
# 续跑未完成的 repair items（必须显式 --run-id 指向 repair run）
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode repair --resume --run-id <REPAIR_RUN_ID>"
```

### 3.5 全市场 verification

```bash
# 基于 repair run 创建 verification run
ssh panji-prod 'docker exec -d trading-backend bash -c "nohup python -m scripts.market_data_quality_cli --mode verify --run-id <REPAIR_RUN_ID> > /tmp/market-data-verify-fullmarket.log 2>&1"'

# 查询 verification run 进度
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, mode, parent_run_id, status, succeeded_count, failed_count, skipped_count,
    ROUND(100.0 * succeeded_count / NULLIF(expected_count, 0), 1) AS pct
  FROM market_data_quality_runs
  WHERE mode = 'verification'
  ORDER BY started_at DESC LIMIT 1;\""

# 比对全市场 repair 前后 classification 变化
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -c \"
  SELECT
    i.classification AS before_classification,
    v.classification AS after_classification,
    COUNT(*) AS count
  FROM market_data_quality_items v
  JOIN market_data_quality_runs vr ON v.run_id = vr.id
  JOIN market_data_quality_runs rr ON vr.parent_run_id = rr.id
  JOIN market_data_quality_items i ON i.run_id = rr.parent_run_id
    AND i.instrument_id = v.instrument_id
    AND i.timeframe = v.timeframe
  WHERE v.run_id = '<VERIFICATION_RUN_ID>'
  GROUP BY i.classification, v.classification
  ORDER BY count DESC;\""
# 期望：DB_MISSING → OK 的数量 ≈ repair succeeded_count
```

## 4. 指定股票池 scan/repair

```bash
# 指定 symbols scan
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --symbols 000001,000021,600519"

# 指定 symbols repair（基于上一步 scan run）
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode repair --run-id <SCAN_RUN_ID>"

# 指定 symbols verify
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode verify --run-id <REPAIR_RUN_ID>"
```

## 5. 限定时间范围

```bash
# 扫描 2026-01-01 至 2026-07-29 的缺口
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --start 2026-01-01 --end 2026-07-29"

# 默认时间范围：bars_daily.max(trade_date) - 365 天 到 bars_daily.max(trade_date)
```

## 6. 限定 timeframe

```bash
# 仅扫描日线
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --timeframe daily"

# 仅扫描 15 分钟线
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --timeframe 15min"

# 同时扫描日线和 15 分钟线（默认）
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --timeframe both"
```

## 安全边界

- 禁止在 A 股交易时段（09:15-15:30）执行 `--repair`，避免与盘中行情写入冲突。
- 禁止跳过 `--verify` 直接将 repair run 标记为"已验证"（PRD MQ-04）。
- 禁止 `--resume` 不带 `--run-id`（PRD MQ-10）。
- 禁止把 qfq 价格写入 `bars_daily` / `bars_15min` 原始表（PRD MQ-03）。
- 禁止直接修改 `market_data_quality_runs` / `market_data_quality_items` 表的 metadata。
- `--repair` 失败时通过 `--resume --run-id <REPAIR_RUN_ID>` 续跑，不得创建新 repair run 重复修复已 succeeded 的 items。
- 全市场 scan/repair/verification 必须使用 `nohup` 后台运行，避免 SSH 断开中断；中断后通过 `--resume` 续跑。

## 故障排查

### scan run 卡在 running

```bash
# 检查 heartbeat_at 是否最新
ssh panji-prod "docker exec trading-postgres psql -U bz -d bz_stock -tAc \"
  SELECT id, status, heartbeat_at, NOW() - heartbeat_at AS stale_for
  FROM market_data_quality_runs
  WHERE id = '<SCAN_RUN_ID>';\""

# 若 stale_for > 5 分钟，检查 worker 是否存活
ssh panji-prod "docker logs --tail 50 trading-backend 2>&1 | grep market_data_quality"

# worker 已死，通过 --resume 续跑
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --resume --run-id <SCAN_RUN_ID>"
```

### repair 引入新缺口

verification run 发现 `after=DB_MISSING` 但 `before=OK` 的 items：

1. 检查 repair run 日志，确认是否覆盖了非目标日期的 bars：
   ```bash
   ssh panji-prod "docker exec trading-backend grep -A5 'ERROR\|WARN' /tmp/market-data-repair-fullmarket.log | tail -50"
   ```
2. 若确认是 repair 误覆盖，从备份恢复 `bars_daily` 对应日期的记录（备份策略见 `docs/runbooks/production-deployment.md`）。
3. 重新执行 `--verify` 确认缺口已消除。

### canary symbols 与预期不一致

```bash
# 检查 canary_symbols 配置
ssh panji-prod "docker exec trading-backend python -c \"
from scripts.market_data_quality_cli import CANARY_SYMBOLS
print(CANARY_SYMBOLS)
\""

# 覆盖 canary symbols
ssh panji-prod "docker exec trading-backend python -m scripts.market_data_quality_cli --mode scan --canary --canary-symbols 000001,000021,600519,000008,300750"
```
