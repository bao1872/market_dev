-- S2 Step 2: per (scope, trade_date) member arrays for Transition / membership-change.
-- Returns ~thousands of rows (one per scope-day). Member arrays kept inline.
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
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
  SELECT av.board_id, av.board_name, av.board_type, av.td, av.version_id,
         COALESCE(array_agg(m.instrument_id) FILTER (WHERE m.instrument_id IS NOT NULL), ARRAY[]::uuid[]) AS pit_members
  FROM active_ver av
  LEFT JOIN board_membership_history m ON m.board_definition_version_id = av.version_id
  GROUP BY av.board_id, av.board_name, av.board_type, av.td, av.version_id
)
SELECT
  p.board_id, p.board_type, p.td AS trade_date,
  p.pit_members,
  COALESCE(array_agg(s.instrument_id) FILTER (WHERE (s.state_payload->>'valid_for_market_aggregation')='true'), ARRAY[]::uuid[]) AS valid_member_ids
FROM pit p
JOIN first_pyramid_history_daily_state s
  ON s.instrument_id = ANY(p.pit_members) AND s.trade_date = p.td
GROUP BY p.board_id, p.board_type, p.td, p.pit_members
ORDER BY p.board_type, p.board_id, p.td;
