-- S2 Step 4a: export valid PIT member state per (scope, trade_date).
-- Intermediate (process data, not final output). Scope-resolved (not full-market raw).
-- Consumer (Python) computes member-accurate Transition + T-1 logic.
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
pm AS (
  SELECT board_id, board_type, td, unnest(pit_members) AS instrument_id FROM pit
)
SELECT pm.board_id, pm.board_type, pm.td AS trade_date, pm.instrument_id::text,
       (s.state_payload->>'regime_value')::int AS regime_value,
       (s.state_payload->>'swing_bias')::int AS swing_bias,
       (s.state_payload->>'internal_bias')::int AS internal_bias,
       s.state_payload->>'momentum_direction' AS momentum_direction
FROM pm
JOIN first_pyramid_history_daily_state s
  ON s.instrument_id = pm.instrument_id AND s.trade_date = pm.td
 AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
ORDER BY board_type, board_id, trade_date, instrument_id;
