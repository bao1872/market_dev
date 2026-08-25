-- S2 Step 4: member-accurate Transition flows per (scope, trade_date).
-- Optimized: unnest PIT members to rows, join state via index (no = ANY(array) full scan).
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
),
active_ver AS (
  SELECT b.id AS board_id, b.type AS board_type, d.td,
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
-- expand PIT members to rows
pm AS (
  SELECT board_id, board_type, td, unnest(pit_members) AS instrument_id FROM pit
),
-- valid state per (scope, day)
st AS MATERIALIZED (
  SELECT pm.board_id, pm.board_type, pm.td AS trade_date, pm.instrument_id,
         (s.state_payload->>'regime_value')::int AS regime_value,
         (s.state_payload->>'swing_bias')::int AS swing_bias,
         (s.state_payload->>'internal_bias')::int AS internal_bias,
         s.state_payload->>'momentum_direction' AS momentum_direction
  FROM pm
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = pm.instrument_id AND s.trade_date = pm.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
),
prev_day AS (
  SELECT board_id, board_type, td AS trade_date,
         LAG(td) OVER (PARTITION BY board_id, board_type ORDER BY td) AS t_minus1
  FROM pit
),
paired AS (
  SELECT cur.board_id, cur.board_type, cur.trade_date, pd.t_minus1 AS t_minus1,
         cur.instrument_id, cur.regime_value AS rv_t, pr.regime_value AS rv_t1,
         cur.swing_bias AS sb_t, pr.swing_bias AS sb_t1,
         cur.internal_bias AS ib_t, pr.internal_bias AS ib_t1,
         cur.momentum_direction AS md_t, pr.momentum_direction AS md_t1
  FROM st cur
  JOIN prev_day pd ON pd.board_id = cur.board_id AND pd.board_type = cur.board_type AND pd.trade_date = cur.trade_date
  JOIN st pr ON pr.board_id = cur.board_id AND pr.board_type = cur.board_type
            AND pr.trade_date = pd.t_minus1 AND pr.instrument_id = cur.instrument_id
  WHERE pd.t_minus1 IS NOT NULL
)
SELECT board_id, board_type, trade_date, t_minus1 AS prev_trade_date,
  count(*) AS transition_denominator,
  count(*) FILTER (WHERE rv_t1 = 0 AND rv_t > 0) AS regime_neutral_to_up,
  count(*) FILTER (WHERE rv_t1 = 0 AND rv_t < 0) AS regime_neutral_to_down,
  count(*) FILTER (WHERE rv_t1 > 0 AND rv_t = 0) AS regime_up_to_neutral,
  count(*) FILTER (WHERE rv_t1 < 0 AND rv_t = 0) AS regime_down_to_neutral,
  count(*) FILTER (WHERE rv_t1 > 0 AND rv_t < 0) AS regime_up_to_down,
  count(*) FILTER (WHERE rv_t1 < 0 AND rv_t > 0) AS regime_down_to_up,
  count(*) FILTER (WHERE sb_t <> sb_t1) AS swing_transition_count,
  count(*) FILTER (WHERE ib_t <> ib_t1) AS internal_transition_count,
  count(*) FILTER (WHERE md_t <> md_t1) AS momentum_transition_count
FROM paired
GROUP BY board_id, board_type, trade_date, t_minus1
ORDER BY board_type, board_id, trade_date;
