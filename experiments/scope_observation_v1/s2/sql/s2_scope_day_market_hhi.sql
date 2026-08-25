-- S2 market control HHI (price + amount) over full-A valid cross-section. (EXACT-T1)
-- EXACT canonical T-1 (BLOCKER #1 fixed): explicit T -> canonical_T1 mapping + two bars joins.
-- PRICE universe = valid FP with exact canonical T-1 return; AMOUNT universe = valid FP with amount non-null.
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
mem_bar AS (
  SELECT s.trade_date, s.instrument_id,
         bt.amount,
         bt.close / bp.close - 1 AS ret
  FROM first_pyramid_history_daily_state s
  JOIN canonical_pairs cp ON cp.t = s.trade_date
  JOIN bars_daily bt ON bt.instrument_id = s.instrument_id AND bt.trade_date = s.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = s.instrument_id AND bp.trade_date = cp.t1
  WHERE (s.state_payload->>'valid_for_market_aggregation') = 'true'
    AND s.trade_date IN (SELECT td FROM days)
),
price_universe AS (
  SELECT trade_date, ret FROM mem_bar WHERE ret IS NOT NULL
),
amount_universe AS (
  SELECT trade_date, amount FROM mem_bar WHERE amount IS NOT NULL
),
cand_agg AS (
  SELECT trade_date, count(*) AS price_candidate_count FROM mem_bar GROUP BY trade_date
),
price_agg AS (
  SELECT trade_date, count(*) AS price_valid_count, sum(abs(ret)) AS total_abs_ret,
         sum(abs(ret)*abs(ret)) AS sum_abs_ret_sq
  FROM price_universe GROUP BY trade_date
),
amount_agg AS (
  SELECT trade_date, count(*) AS amount_valid_count, sum(amount) AS total_amount,
         sum(amount*amount) AS sum_amount_sq
  FROM amount_universe GROUP BY trade_date
)
SELECT 'FULL_MARKET' AS board_id, 'market' AS board_type, p.trade_date,
       c.price_candidate_count, p.price_valid_count,
       p.price_valid_count - c.price_candidate_count AS missing_exact_t1_count,
       a.amount_valid_count,
       CASE WHEN p.total_abs_ret>0 THEN p.sum_abs_ret_sq/(p.total_abs_ret*p.total_abs_ret) ELSE NULL END AS price_contribution_hhi,
       CASE WHEN p.total_abs_ret>0 AND p.price_valid_count>1
            THEN (p.sum_abs_ret_sq/(p.total_abs_ret*p.total_abs_ret) - 1.0/p.price_valid_count)/(1.0-1.0/p.price_valid_count)
            ELSE NULL END AS price_contribution_hhi_normalized,
       CASE WHEN a.total_amount>0 THEN a.sum_amount_sq/(a.total_amount*a.total_amount) ELSE NULL END AS amount_contribution_hhi,
       CASE WHEN a.total_amount>0 AND a.amount_valid_count>1
            THEN (a.sum_amount_sq/(a.total_amount*a.total_amount) - 1.0/a.amount_valid_count)/(1.0-1.0/a.amount_valid_count)
            ELSE NULL END AS amount_contribution_hhi_normalized
FROM price_agg p
JOIN amount_agg a ON a.trade_date = p.trade_date
JOIN cand_agg c ON c.trade_date = p.trade_date
ORDER BY p.trade_date;
