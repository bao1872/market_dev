# Round 1 Summary — DB-Native / Query-on-Demand (Arch v2)

## 1. Crash evidence（服务器重启前一次 boot OOM 扫描）
```
OOM_CONFIRMED
```
- python/python3 docker memcg scope 多次 OOM kill（anon-rss ~3.9G）。
- 原因：旧 frozen dataset + full DataFrame 模式。

## 2. Architecture change
```text
old: full extraction (69 columns × 600k rows × pandas merge × parquet write)
new: DB-native / aggregate-on-demand
```
- No parquet, no full DataFrame, no giant VALUES (...), no full bars fetchall.

## 3. Lineage (Logical Window)
```text
DEV_BASE_SHA = 6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0
EXP_SHA      = 47e692bac0d0bb8856cb9061904211b40c25b13f
ARCHITECTURE = DB_NATIVE_QUERY_ON_DEMAND
DATA_SNAPSHOT_AT = 2026-08-12T04:53:17.883987+00:00

TRADE_DATE_START = 2026-02-09
TRADE_DATE_END   = 2026-08-10
TRADE_DATE_COUNT = 120 (TARGET=120)
TRADE_DATE_IS_EXACT_TARGET = True

ALGORITHM_VERSION       = 1.0.0-core-split
HISTORY_CONTRACT_VERSION = review-history-v2

ROW_COUNT                = 628459
DISTINCT_INSTRUMENT_COUNT= 5283
MIN_UPDATED_AT           = 2026-08-09T11:07:29.564940+08:00
MAX_UPDATED_AT           = 2026-08-11T04:24:10.803502+08:00
SOURCE_HISTORY_RUN_COUNT = 1
```

## 4. Integrity (Step 1)
- NO_DUPLICATE_ROWS
- row_summary total rows = 628459
- trade_date_count = 120
- distinct_instrument_count = 5283
- algo_match_count (should equal rows) = 628459
- hc_outer_match_count = 628459
- hc_payload_match_count = 628459

## 5. Coverage / Missingness (Step 2)
- daily rows = 120
- See coverage_missingness.json for per-readiness + core primitive counts.

## 6. Categorical primitives (Step 3)
- fields = regime_value, swing_bias, internal_bias, structure_alignment, volatility_phase, momentum_direction, momentum_change
- max rows per field = 120 × card(value) aggregate only, no raw rows.

## 7. Continuous primitives (Step 4)
- fields = regime_strength, dsa_dir_bars, dsa_vwap_dev_pct, sqzmom_val, sqzmom_delta, volume_ratio_20, review_volume_ratio20, review_amount_ratio20, review_volume_percentile20, review_amount_percentile200, price_position_120d
- Stat aggregates: count / avg / min / max / p05 / p50 / p95 (or PERCENTILE_DEFERRED).

## 8. Transition audit (Step 5)
- Uses PostgreSQL LAG(state) OVER (PARTITION BY instrument_id ORDER BY trade_date).
- Output: prev_state × current_state × count (≤ 500 rows per field).

## 9. Verdict
```text
PASS — DB-native integrity base OK
```

## 10. Interpretation
> Results are valid for the recorded logical window and database state observed at DATA_SNAPSHOT_AT (2026-08-12T04:53:17.883987+00:00). No byte-for-byte frozen reproducibility is claimed; this is intentional.
