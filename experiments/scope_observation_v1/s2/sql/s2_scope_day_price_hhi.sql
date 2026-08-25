-- S2 Step 3 (EXACT-T1): Price/Amount Contribution HHI per (scope, trade_date).
-- EXACT canonical T-1 (BLOCKER #1 fixed): explicit T -> canonical_T1 mapping + two bars joins.
--   member_return_1d = bt.close / bp.close - 1 (bt at T, bp at exact canonical_T1).
--   If exact T-1 bar missing -> ret UNAVAILABLE. NO instrument-level LAG(close); NO fallback.
-- PRICE valid universe:  PIT(T) ∩ valid(T) ∩ close(T) ∩ close(canonical T-1)  -> price_valid_count
-- PRICE candidate:       PIT(T) ∩ valid(T) ∩ close(T)  -> price_candidate_count
-- missing_exact_t1_count = price_candidate_count - price_valid_count
-- AMOUNT valid universe: PIT(T) ∩ valid(T) ∩ amount(T) non-null (no T-1 return required)  -> amount_valid_count
-- Raw HHI is a fact; normalized HHI = (HHI - 1/N)/(1 - 1/N) for N>1 (N = that metric's valid count).
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
active_ver AS (
  SELECT b.id AS board_id, b.name AS board_name, b.type AS board_type, d.td,
         (SELECT v.id FROM board_definition_versions v
           WHERE v.board_id = b.id AND v.effective_from <= d.td
             AND (v.effective_to IS NULL OR v.effective_to > d.td)
           ORDER BY v.effective_from DESC LIMIT 1) AS version_id
  FROM market_boards b, days d
  WHERE b.type IN ('industry','concept')
),
pit AS (
  SELECT av.board_id, av.board_type, av.td, av.version_id,
         COALESCE(array_agg(m.instrument_id) FILTER (WHERE m.instrument_id IS NOT NULL), ARRAY[]::uuid[]) AS pit_members
  FROM active_ver av
  LEFT JOIN board_membership_history m ON m.board_definition_version_id = av.version_id
  GROUP BY av.board_id, av.board_type, av.td, av.version_id
),
-- valid PIT members per scope-day (independent of bars, for the amount universe)
valid_members AS (
  SELECT p.board_id, p.board_type, p.td AS trade_date, s.instrument_id
  FROM pit p
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = ANY(p.pit_members) AND s.trade_date = p.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
),
-- exact-T1 member facts: current bar at T (bt) with amount, previous bar at exact canonical_T1 (bp).
-- amount is carried from bt (same-day bar); ret requires bp (exact T-1 close).
mem_bar AS (
  SELECT vm.board_id, vm.board_type, vm.trade_date, vm.instrument_id,
         bt.amount,
         bt.close / bp.close - 1 AS ret
  FROM valid_members vm
  JOIN canonical_pairs cp ON cp.t = vm.trade_date
  JOIN bars_daily bt ON bt.instrument_id = vm.instrument_id AND bt.trade_date = vm.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = vm.instrument_id AND bp.trade_date = cp.t1
),
-- PRICE universe: candidate members (current close available) and valid (exact T-1 close available)
price_universe AS (
  SELECT board_id, board_type, trade_date, ret
  FROM mem_bar WHERE ret IS NOT NULL
),
-- AMOUNT universe: valid members with amount non-null (no return requirement)
amount_universe AS (
  SELECT board_id, board_type, trade_date, amount
  FROM mem_bar WHERE amount IS NOT NULL
),
-- price_candidate_count = ALL valid members with a current bar (mem_bar rows), independent of T-1 return
cand_agg AS (
  SELECT board_id, board_type, trade_date, count(*) AS price_candidate_count
  FROM mem_bar GROUP BY board_id, board_type, trade_date
),
price_agg AS (
  SELECT board_id, board_type, trade_date,
         count(*) AS price_valid_count,
         sum(abs(ret)) AS total_abs_ret,
         sum(abs(ret) * abs(ret)) AS sum_abs_ret_sq
  FROM price_universe GROUP BY board_id, board_type, trade_date
),
amount_agg AS (
  SELECT board_id, board_type, trade_date,
         count(*) AS amount_valid_count,
         sum(amount) AS total_amount,
         sum(amount * amount) AS sum_amount_sq
  FROM amount_universe GROUP BY board_id, board_type, trade_date
)
SELECT p.board_id, p.board_type, p.trade_date,
       c.price_candidate_count,
       p.price_valid_count,
       p.price_valid_count - c.price_candidate_count AS missing_exact_t1_count,
       a.amount_valid_count,
       CASE WHEN p.total_abs_ret > 0 THEN p.sum_abs_ret_sq / (p.total_abs_ret * p.total_abs_ret) ELSE NULL END AS price_contribution_hhi,
       CASE WHEN p.total_abs_ret > 0 AND p.price_valid_count > 1
            THEN (p.sum_abs_ret_sq / (p.total_abs_ret * p.total_abs_ret) - 1.0 / p.price_valid_count)
                 / (1.0 - 1.0 / p.price_valid_count) ELSE NULL END AS price_contribution_hhi_normalized,
       CASE WHEN a.total_amount > 0 THEN a.sum_amount_sq / (a.total_amount * a.total_amount) ELSE NULL END AS amount_contribution_hhi,
       CASE WHEN a.total_amount > 0 AND a.amount_valid_count > 1
            THEN (a.sum_amount_sq / (a.total_amount * a.total_amount) - 1.0 / a.amount_valid_count)
                 / (1.0 - 1.0 / a.amount_valid_count) ELSE NULL END AS amount_contribution_hhi_normalized
FROM price_agg p
JOIN amount_agg a
  ON a.board_id = p.board_id AND a.board_type = p.board_type AND a.trade_date = p.trade_date
JOIN cand_agg c
  ON c.board_id = p.board_id AND c.board_type = p.board_type AND c.trade_date = p.trade_date
ORDER BY p.board_type, p.board_id, p.trade_date;
