# 行情质量扫描与修复 PRD

状态：已确认（CHANGE-20260730-014）
最后确认日期：2026-07-30
对应 Map：`../maps/10-market-data.md`（行情质量章节）+ `../maps/30-after-close.md` §11.5（增量检查点）
对应 Runbook：`../runbooks/market-data-quality-scan-repair.md`
需求所有权：全市场行情数据缺口扫描、修复、验证和 resume 合同

> 本文件是行情质量扫描与修复的权威合同。`market_data_quality_cli.py`、`market_data_quality_service.py`、migration 075 表与 admin API 必须遵循本合同。

## 1. 背景

A 股全市场扫描需要发现以下问题：

- 跨数月跳 Bar（内部断层，MDAS 仅尾部补齐无法发现）；
- 上游有数据但 DB 缺失（DB_MISSING）；
- 复权因子异常（FACTOR_MISSING / FACTOR_ANOMALY）；
- 量额不一致（OHLCV 校验失败）；
- 时间排序错误；
- 重复记录。

migration 075 创建 `market_data_quality_runs` 与 `market_data_quality_items` 表，承载扫描/修复/验证的持久化记录。

## 2. 四阶段合同

### MQ-01 dry-run（零持久写）

`--dry-run` 必须满足：

- **零持久写**：不创建 `market_data_quality_runs` 记录、不创建 `market_data_quality_items` 记录、不修改任何 `bars_daily` / `bars_15min` / `adj_factor` 表。
- **解析 symbols**：解析 `--symbols` / `--canary` / `--limit` 参数为最终的目标股票列表，但只打印不查询数据库写入。
- **输出**：stdout 输出目标股票列表、扫描计划（timeframe、start、end、batch_size）、预计 item 数量；不输出 item 级别详情（除非显式 `--verbose`）。
- **用途**：用户在执行 `--scan` 或 `--repair` 前验证参数解析正确，避免误操作全市场。
- **禁止**：`--dry-run` 不得与 `--resume` 同时使用（resume 必须基于已存在的 run）。

### MQ-02 scan（写审计 run/items，不改 bars）

`--scan` 必须满足：

- **持久化 run**：创建 `market_data_quality_runs` 记录，`status=running`，`mode=scan`；扫描完成后 `status=succeeded`（或 `partial_failed`）。
- **持久化 items**：每个股票×timeframe 创建一条 `market_data_quality_items` 记录，包含 `classification`（OK / DB_MISSING / SOURCE_MISSING / FACTOR_MISSING / FACTOR_ANOMALY / SUSPENDED / NOT_LISTED / DELISTED / DUPLICATE / TIME_ORDER_ERROR / OHLCV_INCONSISTENT）、`gap_details`、`source_evidence`、`db_evidence`。
- **不改 bars**：`--scan` 严禁修改 `bars_daily` / `bars_15min` / `adj_factor` 表；只读校验，所有发现的缺口写入 items 表供后续 `--repair` 使用。
- **可 resume**：`--scan` 中断后可通过 `--resume --run-id <RUN_ID>` 续跑，只处理 `pending` 或 `failed` 的 items；`succeeded` items 不重扫。
- **batch**：`--batch-size` 控制 SQL 查询批量大小（默认 50 只/批），不控制 run/items 写入边界（每只股票独立 item）。

### MQ-03 repair（写 raw OHLCV）

`--repair` 必须满足：

- **前置 run**：必须基于一个已 `succeeded` 或 `partial_failed` 的 scan run；若没有显式 `--run-id`，则自动选取最近一个 `mode=scan` 的 succeeded run。
- **持久化新 run**：创建新的 `market_data_quality_runs` 记录，`status=running`，`mode=repair`；与原 scan run 通过 `parent_run_id` 关联。
- **修复范围**：只修复 `classification=DB_MISSING` 的 items；`SOURCE_MISSING` / `SUSPENDED` / `NOT_LISTED` / `DELISTED` 不修复，标记为 `skipped` 并记录原因。
- **写 raw OHLCV**：从上游拉取数据后，幂等 upsert 到 `bars_daily` / `bars_15min`（原始未复权 OHLCV）；禁止把 qfq 价格写入原始表。
- **重算 adj_factor**：修复 raw OHLCV 后，按现有除权除息 SSOT 重算 `adj_factor`；不得直接覆盖 `adj_factor` 表的非目标日期记录。
- **可 resume**：`--repair` 中断后可通过 `--resume --run-id <REPAIR_RUN_ID>` 续跑。
- **禁止跨 mode resume**：`--resume` 时 `--run-id` 必须与 `--mode` 一致（scan run 不能用于 repair resume）。

### MQ-04 verification scan（新 run，禁止复用旧 run）

`--verify` 必须满足：

- **新 run**：必须创建新的 `market_data_quality_runs` 记录，`mode=verification`；**禁止复用 scan run 或 repair run 的记录**。
- **目的**：验证 `--repair` 修复后，原 DB_MISSING 的 items 是否变为 OK；同时发现修复过程中是否引入新的缺口。
- **比对**：verification run 完成后，与原 repair run 通过 `parent_run_id` 关联；admin API 提供 diff 视图，列出 `before=DB_MISSING → after=OK` 的 items 和新增的 `after=DB_MISSING` items。
- **不改 bars**：与 `--scan` 相同，只读校验，不修改任何行情表。
- **可 resume**：`--resume --run-id <VERIFICATION_RUN_ID>` 续跑未完成的 items。
- **禁止跳过**：`--repair` 完成后必须执行 `--verify`；不得直接将 repair run 标记为"已验证"。

## 3. --resume 合同

### MQ-10 --resume 必须显式 --run-id

`--resume` 必须满足：

- **显式 --run-id**：`--resume` 必须搭配 `--run-id <RUN_ID>`；禁止 `--resume` 不带 `--run-id`（避免误 resume 任意历史 run）。
- **run 必须存在**：`--run-id` 指向的 run 必须存在于 `market_data_quality_runs` 表中；不存在则报错退出。
- **mode 一致**：`--run-id` 的 `mode` 必须与 CLI 当前 `--mode` 一致（scan / repair / verification）；不一致报错退出。
- **状态合法**：run 状态必须为 `running` / `partial_failed` / `interrupted`；`succeeded` 的 run 不允许 resume（避免重复执行）。
- **只处理 pending/failed**：resume 只处理 `status IN ('pending', 'failed')` 或 `lease_expires_at < NOW()` 的 running items；`succeeded` items 跳过且不重算。
- **lease_epoch fencing**：resume 时递增 `lease_epoch`，旧 worker 的写入被拒绝。
- **不创建新 run**：resume 不创建新 run 记录，只续跑原 run 的 items；与 `--repair` / `--verify` 创建新 run 的语义不同。

## 4. --canary 合同

### MQ-20 --canary 必须在查询前应用 symbols/limit

`--canary` 必须满足：

- **查询前应用**：`--canary` 必须在数据库查询、API 调用、run/items 创建之前完成 symbols 列表解析；禁止先创建 run 再应用 canary 限制。
- **固定 5 只**：`--canary` 默认选取 5 只代表性股票（含深科技 000021、贵州茅台 600519、平安银行 000001、神州高铁 000008、宁德时代 300750）；可通过 `--canary-symbols` 覆盖。
- **--limit 优先**：`--limit N` 优先于 `--canary`；同时指定时按 `--limit N` 取前 N 只。
- **--symbols 最高优先**：`--symbols 000001,000021` 优先于 `--canary` 和 `--limit`；同时指定时只处理 `--symbols` 列出的股票。
- **dry-run + canary**：`--dry-run --canary` 只打印 5 只股票列表和扫描计划，不创建 run。
- **scan + canary**：`--scan --canary` 创建 run，但只扫描 5 只股票的 items。
- **repair + canary**：`--repair --canary` 只修复 5 只股票的 DB_MISSING items；不修复其他股票。
- **审计**：canary run 的 `metadata_json` 必须记录 `canary=true` 和 `canary_symbols=[...]`，便于区分全量 run 和 canary run。

## 5. 数据模型合同

### MQ-30 market_data_quality_runs

```
id UUID PK
trade_date DATE NOT NULL
mode VARCHAR NOT NULL          # scan / repair / verification
parent_run_id UUID NULL        # repair → scan; verification → repair
status VARCHAR NOT NULL        # running / succeeded / partial_failed / failed / interrupted
expected_count INTEGER NOT NULL
succeeded_count INTEGER NOT NULL
failed_count INTEGER NOT NULL
skipped_count INTEGER NOT NULL
lease_epoch INTEGER NOT NULL DEFAULT 0
heartbeat_at TIMESTAMPTZ NULL
started_at TIMESTAMPTZ NOT NULL
completed_at TIMESTAMPTZ NULL
metadata_json JSONB NOT NULL DEFAULT '{}'
created_at / updated_at
```

唯一约束：无（同一 trade_date 可有多个 run，通过 mode + parent_run_id 区分）。

### MQ-31 market_data_quality_items

```
id UUID PK
run_id UUID FK NOT NULL
instrument_id UUID NOT NULL
symbol VARCHAR NOT NULL
timeframe VARCHAR NOT NULL     # daily / 15min
classification VARCHAR NOT NULL # OK / DB_MISSING / SOURCE_MISSING / FACTOR_MISSING / FACTOR_ANOMALY / SUSPENDED / NOT_LISTED / DELISTED / DUPLICATE / TIME_ORDER_ERROR / OHLCV_INCONSISTENT
status VARCHAR NOT NULL        # pending / running / succeeded / failed / skipped
gap_details JSONB              # 缺口区间、缺失天数、首尾日期
source_evidence JSONB          # 上游数据快照
db_evidence JSONB              # DB 当前数据快照
lease_epoch INTEGER NOT NULL DEFAULT 0
lease_expires_at TIMESTAMPTZ NULL
last_error TEXT NULL
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
created_at / updated_at
```

唯一约束：`run_id + instrument_id + timeframe`。

## 6. CLI 合同

### MQ-40 命令行参数

```
python -m scripts.market_data_quality_cli \
  --mode {scan|repair|verify} \
  --timeframe {daily|15min|both} \
  --symbols SYMBOL1,SYMBOL2 \
  --canary \
  --canary-symbols SYMBOL1,SYMBOL2 \
  --limit N \
  --start YYYY-MM-DD \
  --end YYYY-MM-DD \
  --batch-size N \
  --dry-run \
  --resume --run-id <RUN_ID> \
  --verbose
```

约束：

- `--mode` 必填（scan / repair / verify）。
- `--resume` 必须搭配 `--run-id`（见 MQ-10）。
- `--dry-run` 不得与 `--resume` 同时使用。
- `--canary` / `--limit` / `--symbols` 优先级见 MQ-20。
- `--start` / `--end` 默认从 `bars_daily.max(trade_date) - 365` 到 `bars_daily.max(trade_date)`。

## 7. 验收标准

- `--dry-run` 不创建任何 DB 记录，不修改任何行情表。
- `--scan` 创建 run + items，但 `bars_daily` / `bars_15min` / `adj_factor` 表无任何变化。
- `--repair` 只修改 DB_MISSING items 对应的 bars 记录，adj_factor 按 SSOT 重算。
- `--verify` 必须创建新 run，不得复用 scan 或 repair run。
- `--resume` 不带 `--run-id` 直接报错退出。
- `--canary` 在查询前完成 symbols 解析，run metadata 记录 `canary=true`。
- canary 5 只股票操作步骤可在 Runbook 中复现（见 `../runbooks/market-data-quality-scan-repair.md`）。
