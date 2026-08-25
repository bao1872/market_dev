-- S2 market control individual contribution facts (EXACT-T1; top contributors per day).
-- EXACT canonical T-1: explicit T -> canonical_T1 mapping + two bars joins. NO LAG(close); no fallback.
-- PRICE / AMOUNT universes separated with independent scope-level counts (no COALESCE fallback).
-- Four independent ranks over the FULL universe; output keeps rows with any rank <= 10.
-- Full-universe signed validation DB-native (equal_weight_return_mean / sum_signed / delta).
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
),
canonical_pairs(t, t1) AS (
  VALUES
    ('2026-08-03'::date, '2026-07-31'::date),
    ('2026-08-04'::date, '2026-08-03'::date),
    ('2026-08-05'::date, '2026-08-04'::date),
    ('2026-08-06'::date, '2026-08-05'::date),
    ('2026-08-07'::date, '2026-08-06'::date),
    ('2026-08-10'::date, '2026-08-07'::date)
),
valid_members AS (
  SELECT s.instrument_id, s.trade_date
  FROM first_pyramid_history_daily_state s
  WHERE (s.state_payload->>'valid_for_market_aggregation') = 'true'
    AND s.trade_date IN (SELECT td FROM days)
),
mem_bar AS MATERIALIZED (
  SELECT vm.trade_date, vm.instrument_id,
         bt.amount,
         bt.close / bp.close - 1 AS member_return_1d
  FROM valid_members vm
  JOIN canonical_pairs cp ON cp.t = vm.trade_date
  JOIN bars_daily bt ON bt.instrument_id = vm.instrument_id AND bt.trade_date = vm.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = vm.instrument_id AND bp.trade_date = cp.t1
),
price_universe AS (
  SELECT trade_date, instrument_id, member_return_1d FROM mem_bar WHERE member_return_1d IS NOT NULL
),
amount_universe AS (
  SELECT trade_date, instrument_id, amount FROM mem_bar WHERE amount IS NOT NULL
),
price_tot AS MATERIALIZED (
  SELECT trade_date,
         count(*) AS price_valid_count,
         sum(abs(member_return_1d)) AS total_abs_ret,
         avg(member_return_1d) AS equal_weight_return_mean,
         sum(member_return_1d) / count(*) AS sum_signed_return_contribution,
         1.0 AS sum_abs_price_change_share
  FROM price_universe GROUP BY trade_date
),
amount_tot AS MATERIALIZED (
  SELECT trade_date, count(*) AS amount_valid_count, sum(amount) AS total_amount,
         1.0 AS sum_amount_share
  FROM amount_universe GROUP BY trade_date
),
cand_tot AS MATERIALIZED (
  SELECT trade_date, count(*) AS price_candidate_count FROM mem_bar GROUP BY trade_date
),
full_contrib AS MATERIALIZED (
  SELECT mb.trade_date, mb.instrument_id,
         mb.member_return_1d,
         mb.member_return_1d / pt.price_valid_count AS signed_return_contribution,
         abs(mb.member_return_1d) / pt.total_abs_ret AS abs_price_change_share,
         mb.amount / at.total_amount AS amount_share,
         pt.price_valid_count, pt.total_abs_ret,
         at.amount_valid_count, at.total_amount,
         ct.price_candidate_count,
         (ct.price_candidate_count - pt.price_valid_count) AS missing_exact_t1_count,
         pt.equal_weight_return_mean,
         pt.sum_signed_return_contribution,
         (pt.sum_signed_return_contribution - pt.equal_weight_return_mean) AS signed_contribution_delta,
         pt.sum_abs_price_change_share,
         at.sum_amount_share
  FROM mem_bar mb
  JOIN price_tot pt ON pt.trade_date = mb.trade_date
  LEFT JOIN amount_tot at ON at.trade_date = mb.trade_date
  JOIN cand_tot ct ON ct.trade_date = mb.trade_date
  WHERE mb.member_return_1d IS NOT NULL
)
SELECT * FROM (
  SELECT 'FULL_MARKET' AS board_id, '全A横截面' AS board_name, 'market' AS board_type, fc.trade_date,
         fc.instrument_id::text, i.symbol, i.name,
         fc.member_return_1d, fc.signed_return_contribution, fc.abs_price_change_share, fc.amount_share,
         fc.price_valid_count, fc.total_abs_ret,
         fc.amount_valid_count, fc.total_amount,
         fc.price_candidate_count, fc.missing_exact_t1_count,
         fc.equal_weight_return_mean, fc.sum_signed_return_contribution, fc.signed_contribution_delta,
         fc.sum_abs_price_change_share, fc.sum_amount_share,
         row_number() OVER (PARTITION BY fc.trade_date
                            ORDER BY fc.signed_return_contribution DESC) AS positive_rank,
         row_number() OVER (PARTITION BY fc.trade_date
                            ORDER BY fc.signed_return_contribution ASC) AS negative_rank,
         row_number() OVER (PARTITION BY fc.trade_date
                            ORDER BY fc.abs_price_change_share DESC) AS abs_price_rank,
         row_number() OVER (PARTITION BY fc.trade_date
                            ORDER BY fc.amount_share DESC NULLS LAST) AS amount_rank
  FROM full_contrib fc
  LEFT JOIN instruments i ON i.id = fc.instrument_id
) r
WHERE r.positive_rank <= 10 OR r.negative_rank <= 10 OR r.abs_price_rank <= 10 OR r.amount_rank <= 10
ORDER BY r.trade_date, r.positive_rank;
