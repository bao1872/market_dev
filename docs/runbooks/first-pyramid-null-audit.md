# 第一金字塔 99 字段空值审计 Runbook

本 Runbook 描述对最新正式 `stock_core` run 及最近 N 个交易日的三层（DB raw → flatten → API）99 字段空值审计流程。输出字段级 `空值率 / status / reason分布 / 样本symbol / 发生层` 表格；禁止用 0 / 空字符串 / 旧值填补。

对应 PRD：`docs/prd/40-market-stock-experience.md` §MX-63（空值语义合同）。

## 前置条件

1. 通过 `scripts/ops/panji-prod-preflight` 校验生产 SSH 入口；
2. 在 `panji-prod` 上操作，DB 只读事务：`SET TRANSACTION READ ONLY`。禁止任何写入；
3. 本地账本：执行前记录 `memory_pressure`、磁盘、`.git/.pytest_cache` 大小到 `/tmp/trae_market_review_closure.md`。（本地仅保存分析报告，不直接运行重查询）；
4. 先确认最近正式 stock_core publication：`factor_publications.scope = 'stock_core' WHERE is_published = true ORDER BY business_date DESC LIMIT 1`。

## 1. 审计范围与分层

**交易日范围**：`latest_published_trade_date`（记为 T）及前 5 个交易日：`[T-5, T-4, T-3, T-2, T-1, T]` 共 6 天。

**三层空值**：

| 层 | 数据来源 | 审计字段名前缀 | 说明 |
|---|---|---|---|
| A. DB raw | `stock_feature_snapshots.summary_payload::jsonb -> 'first_pyramid'` | 原始 99 字段 | 写库层；可能含子结构嵌套 |
| B. flatten | `flatten_first_pyramid(first_pyramid_json)` 输出 | `fp_*` 扁平 99 字段 | flatten 层；确认未丢失 A 层存在的字段 |
| C. API 响应 | `GET /api/v1/market/stocks?page_size=1000` 中 `items[].first_pyramid` | `fp_*` 扁平 99 字段 | 与 B 层应一一对应 |

空值分类（必须填入审计表格第 8 列）：

| code | 含义 | 例子 |
|---|---|---|
| `conditional_null` | 合理条件空值 | 无 prev 段 → 段涨跌空；无事件 → event_count = 0 但 event_type = null；chip skipped/failed → chip_* 全空；五元组不匹配 → chip_consensus null |
| `insufficient_history` | 数据/历史不足 | 上市 < 60 交易日 → trend_momentum 空；15m K < 20 根 → volatility null |
| `compute_failed` | 计算失败 | 上游抛异常 / NaN 拒绝写入 / worker 子任务 failed 且不可重试 |
| `mapping_lost` | flatten/API 映射丢失 | A 层存在 value 但 B/C 层字段为 null；B 层非空但 C 层 null |

## 2. 实际执行（panji-prod 只读容器）

### 2.1 Step 1: 确认最新正式 stock_core run

```bash
docker exec trading-postgres psql -U bz -d bz_stock <<'SQL'
-- 获取最近 6 个交易日 stock_core publication
SET TRANSACTION READ ONLY;
SELECT p.business_date, p.run_id, p.algorithm_version, p.coverage
  FROM factor_publications p
 WHERE p.is_published = true
   AND p.scope = 'stock_core'
 ORDER BY p.business_date DESC
 LIMIT 6;
SQL
```

→ 保存 6 行：`business_date / run_id / algo_ver / coverage`。若 coverage < 0.95 单独记录（可能导致系统性高空值）。

### 2.2 Step 2: A 层空值率统计（summary_payload.first_pyramid）

上传 SQL 脚本到 `/tmp/fp_null_audit.sql`，容器内以只读事务执行：

```bash
docker cp /tmp/fp_null_audit.sql trading-postgres:/tmp/fp_null_audit.sql
docker exec -t trading-postgres \
  psql -U bz -d bz_stock \
    -v ON_ERROR_STOP=1 \
    -f /tmp/fp_null_audit.sql \
    > /tmp/fp_null_audit_A_output.tsv 2>&1
```

`fp_null_audit.sql` 参考结构（对 6 天 × 99 字段展开，每字段 1 行）：

```sql
-- [fp_null_audit.sql] 99字段 × 三层空值审计，按 A/B/C 层分类
-- 参数: :v_run_ids 数组 或 直接 IN (...)
SET TRANSACTION READ ONLY;

WITH
-- 6 个交易日的 run_id
recent_runs AS (
  SELECT business_date, run_id
    FROM factor_publications
   WHERE is_published = true
     AND scope = 'stock_core'
   ORDER BY business_date DESC
   LIMIT 6
),
-- 从 summary_payload 提取 first_pyramid raw (层 A)
raw_snaps AS (
  SELECT s.instrument_id, s.trade_date,
         s.summary_payload::jsonb -> 'first_pyramid' AS fp_raw,
         r.run_id
    FROM stock_feature_snapshots s
    JOIN recent_runs r
      ON s.trade_date = r.business_date
     AND s.stock_core_run_id = r.run_id
)
-- 后续 99 字段按类枚举 (trend / structure / momentum / volume / chip / event / meta 7 类)
-- 每类用 jsonb_extract_path_text 或 ->> 取值，统计 null 率。
-- 示例：趋势类
SELECT 'trend_direction'::text AS field, 'trend'::text AS category,
       count(*) AS total_rows,
       count(CASE WHEN (fp_raw ->> 'trend_direction') IS NOT NULL THEN 1 END) AS non_null,
       (1.0 - count(CASE WHEN (fp_raw ->> 'trend_direction') IS NOT NULL THEN 1 END)::float /
              NULLIF(count(*),0)) AS null_rate
  FROM raw_snaps;
```

→ 对 99 字段按 7 类（`trend / structure / momentum / volume / chip / event / meta`）分别 SELECT 输出列：`field / category / total_rows / non_null / null_rate`。

### 2.3 Step 3: B 层 flatten 输出

在 backend 容器内使用 `flatten_first_pyramid` 函数直接转换 A 层 jsonb，并统计相同 99 字段的 null 率。输出列同上，增加 `层=B`。

### 2.4 Step 4: C 层 API 响应

在同一网络内（panji-prod 内部）：

```bash
curl -sS -H "Authorization: Bearer <valid_token>" \
  "https://<host>/api/v1/market/stocks?scope=market&page=1&page_size=4000" \
  | jq -c '.items[] | {symbol: .symbol, fp: .first_pyramid}' \
  > /tmp/market_stocks_C_layer.jsonl
```

→ 用 `fp_null_audit.sql` 相同逻辑本地（或容器内）对 C 层 JSONL 输出 99 字段 null 率表格。

### 2.5 Step 5: 三层合并 + 空值发生层判定

对 A/B/C 三层的 99 字段 × 6 天结果合并，判定空值发生在哪一层：

| A 非空? | B 非空? | C 非空? | 发生层 | 分类 |
|---|---|---|---|---|
| ✅ | ✅ | ✅ | 无 | OK |
| ❌ | ❌ | ❌ | A | 根据 A 层数据判定（conditional / insufficient / compute_failed） |
| ✅ | ❌ | ❌ | B→flatten 映射丢 | mapping_lost |
| ✅ | ✅ | ❌ | C→API 序列化丢 | mapping_lost |
| ❌ | ✅ | ✅ | — | A 层误判（检查查询 SQL 字段路径） |

## 3. 输出表格

每字段输出：

| 列 | 含义 |
|---|---|
| field | 第一金字塔字段名（如 `fp_trend_direction`） |
| category | trend / structure / momentum / volume / chip / event / meta 中的一类 |
| total_rows | 审计快照行总数（≈ instrument 数 × 6 天） |
| non_null_A / non_null_B / non_null_C | 三层各自非空数 |
| null_rate_A / null_rate_B / null_rate_C | 三层各自空值率（0..1） |
| status_or_reason_top | 非空时 dominant reason；空值 dominant code；按 C 层优先 |
| sample_symbols | 空值样例 5 个 symbol（C 层为空的 symbol） |
| source_run_versions | 覆盖到的 run_id / algorithm_version 清单 |
| null_layer | 空值发生层：A / B / C / 无 |
| null_classification | conditional_null / insufficient_history / compute_failed / mapping_lost / OK |

## 4. 阻断项与修复阈值

- `空值率 > 80%`：**阻断级**。逐项给根因；若为 `compute_failed` → 必须修复或与用户确认合同；禁止当作"合理空值"跳过。
- `20% < 空值率 ≤ 80%`：**高优先级**：逐项给根因 + 样本 symbol。
- `10% < 空值率 ≤ 20%`：记录即可。
- `< 10%`：视为 OK，按分类抽查。

## 5. 证据留存

- SQL 脚本（含 run_id 清单）：`/tmp/fp_null_audit_TIMESTAMP.sql`
- A/B/C 三层 TSV 输出：归档到 `docs/changes/2026/CHANGE-20260801-001/evidence/` 或同等位置。
- 最终合并表 + 阻断级分析结论：写入 CHANGE 的"空值审计"段落。
