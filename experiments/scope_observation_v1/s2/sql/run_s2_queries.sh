#!/usr/bin/env bash
# S2 read-only DB queries (CORRECTED). Resource contract:
#   work_mem <= 64MB, statement_timeout <= 300s, max_parallel_workers_per_gather = 0
# Read-only SELECT only. No writes to production DB.
#
# Usage: sql/run_s2_queries.sh
# Remote: panji-prod -> docker exec trading-postgres psql -U bz -d bz_stock
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/out"
SERV="/tmp/s2_sql_$$"
mkdir -p "$SERV"
ssh panji-prod "mkdir -p '$SERV'"

PSQL="docker exec trading-postgres psql -U bz -d bz_stock -q -P pager=off"
# Resource contract: work_mem<=64MB, statement_timeout<=300s, no parallel gather.
# Prepend SET statements to the query file (avoid nested-quote issues in -c).

run_one() {
  local local_sql="$1" out_csv="$3"
  local fname
  fname="$(basename "$local_sql")"
  {
    echo "SET work_mem TO '64MB';"
    echo "SET statement_timeout = 300000;"
    echo "SET max_parallel_workers_per_gather = 0;"
    cat "$local_sql"
  } | ssh panji-prod "docker exec -i trading-postgres psql -U bz -d bz_stock -q -v ON_ERROR_STOP=1 --csv -P pager=off" > "$out_csv" 2> "$out_csv.err" || {
    echo "FAILED: $fname (see $out_csv.err)" >&2
    tail -5 "$out_csv.err" >&2
    return 1
  }
  echo "OK: $fname -> $(basename "$out_csv") ($(wc -l < "$out_csv") lines)"
}

# board axis (corrected regime_strength)
run_one "$ROOT/sql/s2_scope_day_axis.sql"        "$SERV/axis.sql"        "$OUT/_axis.csv"
run_one "$ROOT/sql/s2_scope_day_market.sql"      "$SERV/axis_mkt.sql"    "$OUT/_axis_market.csv"

# price HHI (corrected: 08-03 uses 07-31)
run_one "$ROOT/sql/s2_scope_day_price_hhi.sql"   "$SERV/phhi.sql"        "$OUT/_price_hhi.csv"
run_one "$ROOT/sql/s2_scope_day_market_hhi.sql"  "$SERV/phhi_mkt.sql"    "$OUT/_price_hhi_market.csv"

# price facts (return level/distribution/breadth) — NEW
run_one "$ROOT/sql/s2_scope_day_price_facts.sql"      "$SERV/pfacts.sql"       "$OUT/_price_facts.csv"
run_one "$ROOT/sql/s2_scope_day_market_price_facts.sql" "$SERV/pfacts_mkt.sql" "$OUT/_price_facts_market.csv"

# individual contribution (top contributors) — NEW, process-only (gitignored)
run_one "$ROOT/sql/s2_scope_day_contribution.sql"      "$SERV/contrib.sql"      "$OUT/_contribution.csv"
run_one "$ROOT/sql/s2_scope_day_market_contribution.sql" "$SERV/contrib_mkt.sql" "$OUT/_contribution_market.csv"

# share-validation (sum ~= 1 over all members) — NEW, compact
run_one "$ROOT/sql/s2_scope_day_share_validation.sql" "$SERV/val.sql" "$OUT/_share_validation.csv"

ssh panji-prod "rm -rf '$SERV'"
echo "ALL SQL DONE"
