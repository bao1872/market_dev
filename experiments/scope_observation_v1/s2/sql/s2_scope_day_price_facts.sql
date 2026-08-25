-- S2 Price Facts (EXACT-T1): per (scope, trade_date) RETURN LEVEL / DISTRIBUTION / BREADTH.
-- EXACT canonical T-1 (BLOCKER #1 fixed):
--   explicit T -> canonical_T1 day-pair mapping (VALUES), then TWO bars joins:
--     bt = bar at T, bp = bar at exact canonical_T1.
--   member_return_1d = bt.close / bp.close - 1.
--   If the exact canonical_T1 bar does not exist for that instrument -> return UNAVAILABLE.
--   NO instrument-level LAG(close); NO fallback to an older bar (would fabricate a multi-day return).
-- PRICE valid universe:  PIT(T) ∩ valid(T) ∩ close(T) ∩ close(canonical T-1)  -> price_valid_count
-- PRICE candidate:       PIT(T) ∩ valid(T) ∩ close(T) (current close available)
-- missing_exact_t1_count = price_candidate_count - price_valid_count  (suspend / missing bar audit)
-- PRICE BREADTH (threshold-free): return>0=advance, <0=decline, ==0=unchanged. No ±threshold. No Price Score.
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
),
-- explicit canonical day pairs (observation day -> its exact previous trading day)
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
-- exact-T1 member return: current bar bt at T joined to previous bar bp at exact canonical_T1.
-- LEFT JOIN on bp: if the exact T-1 bar is missing for that instrument, ret stays NULL (UNAVAILABLE).
mem_ret AS (
  SELECT p.board_id, p.board_type, p.td AS trade_date, s.instrument_id,
         bt.close / bp.close - 1 AS ret
  FROM pit p
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = ANY(p.pit_members) AND s.trade_date = p.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
  JOIN bars_daily bt
    ON bt.instrument_id = s.instrument_id AND bt.trade_date = p.td
  JOIN canonical_pairs cp ON cp.t = p.td
  LEFT JOIN bars_daily bp
    ON bp.instrument_id = s.instrument_id AND bp.trade_date = cp.t1
)
SELECT board_id, board_type, trade_date,
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
GROUP BY board_id, board_type, trade_date
ORDER BY board_type, board_id, trade_date;
