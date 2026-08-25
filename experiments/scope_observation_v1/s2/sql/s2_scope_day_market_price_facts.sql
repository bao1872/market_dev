-- S2 market control PRICE FACTS (EXACT-T1): RETURN LEVEL / DISTRIBUTION / BREADTH over full-A valid cross-section.
-- EXACT canonical T-1 (BLOCKER #1 fixed): explicit T -> canonical_T1 mapping + two bars joins.
-- member_return_1d = bt.close / bp.close - 1 (bt at T, bp at exact canonical_T1).
-- If exact T-1 bar missing -> ret UNAVAILABLE. NO instrument-level LAG(close).
-- price_candidate_count = valid FP with current close; price_valid_count = candidate with exact T-1 close.
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
mem_ret AS (
  SELECT s.trade_date, s.instrument_id,
         bt.close / bp.close - 1 AS ret
  FROM first_pyramid_history_daily_state s
  JOIN canonical_pairs cp ON cp.t = s.trade_date
  JOIN bars_daily bt ON bt.instrument_id = s.instrument_id AND bt.trade_date = s.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = s.instrument_id AND bp.trade_date = cp.t1
  WHERE (s.state_payload->>'valid_for_market_aggregation') = 'true'
    AND s.trade_date IN (SELECT td FROM days)
)
SELECT 'FULL_MARKET' AS board_id, 'market' AS board_type, trade_date,
       count(*) AS price_candidate_count,
       count(*) FILTER (WHERE ret IS NOT NULL) AS price_valid_count,
       count(*) FILTER (WHERE ret IS NULL) AS missing_exact_t1_count,
       avg(ret) AS equal_weight_return_mean,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY ret) AS return_median,
       percentile_cont(0.25) WITHIN GROUP (ORDER BY ret) AS return_p25,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY ret) AS return_p50,
       percentile_cont(0.75) WITHIN GROUP (ORDER BY ret) AS return_p75,
       percentile_cont(0.10) WITHIN GROUP (ORDER BY ret) AS return_p10,
       percentile_cont(0.90) WITHIN GROUP (ORDER BY ret) AS return_p90,
       count(*) FILTER (WHERE ret > 0) AS advance_count,
       count(*) FILTER (WHERE ret < 0) AS decline_count,
       count(*) FILTER (WHERE ret = 0) AS unchanged_count
FROM mem_ret
GROUP BY trade_date
ORDER BY trade_date;
