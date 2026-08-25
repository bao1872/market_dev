-- S2 Step 1: per (scope, trade_date) axis aggregation (DB-native).
-- Returns ~thousands of scope-day rows. No full-market raw pull.
-- PIT resolver semantics mirrored from board_membership_service.resolve_board_membership_at.
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
),
-- active definition version per board per day (latest effective_from wins)
active_ver AS (
  SELECT b.id AS board_id, b.name AS board_name, b.type AS board_type, d.td,
         (SELECT v.id FROM board_definition_versions v
           WHERE v.board_id = b.id
             AND v.effective_from <= d.td
             AND (v.effective_to IS NULL OR v.effective_to > d.td)
           ORDER BY v.effective_from DESC LIMIT 1) AS version_id,
         (SELECT v.effective_from FROM board_definition_versions v
           WHERE v.board_id = b.id
             AND v.effective_from <= d.td
             AND (v.effective_to IS NULL OR v.effective_to > d.td)
           ORDER BY v.effective_from DESC LIMIT 1) AS def_from
  FROM market_boards b, days d
  WHERE b.type IN ('industry','concept')
),
-- PIT members for that version
pit AS (
  SELECT av.board_id, av.board_name, av.board_type, av.td, av.def_from, av.version_id,
         COALESCE(array_agg(m.instrument_id) FILTER (WHERE m.instrument_id IS NOT NULL), ARRAY[]::uuid[]) AS pit_members
  FROM active_ver av
  LEFT JOIN board_membership_history m ON m.board_definition_version_id = av.version_id
  GROUP BY av.board_id, av.board_name, av.board_type, av.td, av.def_from, av.version_id
),
-- state rows for PIT members on that day (valid_for_market_aggregation only)
mem_state AS (
  SELECT p.board_id, p.board_name, p.board_type, p.td, p.def_from, p.version_id,
         p.pit_members,
         s.instrument_id,
         (s.state_payload->>'regime_value')::int AS regime_value,
         (s.state_payload->>'swing_bias')::int AS swing_bias,
         (s.state_payload->>'internal_bias')::int AS internal_bias,
         s.state_payload->>'momentum_direction' AS momentum_direction,
         s.state_payload->>'structure_alignment' AS structure_alignment,
         (s.state_payload->>'review_volume_ratio20')::float AS review_volume_ratio20,
         (s.state_payload->>'review_amount_ratio20')::float AS review_amount_ratio20,
         (s.state_payload->>'review_volume_percentile20')::float AS review_volume_percentile20,
         (s.state_payload->>'review_amount_percentile200')::float AS review_amount_percentile200,
         (s.state_payload->>'fp_segment_volume_ratio')::float AS fp_segment_volume_ratio,
         (s.state_payload->>'regime_strength')::float AS regime_strength
  FROM pit p
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = ANY(p.pit_members)
   AND s.trade_date = p.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
)
SELECT
  board_id, board_name, board_type, td AS trade_date, def_from AS definition_effective_from,
  version_id::text AS membership_version,
  cardinality(pit_members) AS pit_member_count,
  count(*) AS fp_row_count,
  -- TREND regime distribution
  count(*) FILTER (WHERE regime_value > 0) AS regime_up,
  count(*) FILTER (WHERE regime_value = 0) AS regime_neutral,
  count(*) FILTER (WHERE regime_value < 0) AS regime_down,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY regime_strength) AS regime_strength_median,
  -- STRUCTURE swing / internal / alignment
  count(*) FILTER (WHERE swing_bias > 0) AS swing_up,
  count(*) FILTER (WHERE swing_bias = 0) AS swing_neutral,
  count(*) FILTER (WHERE swing_bias < 0) AS swing_down,
  count(*) FILTER (WHERE internal_bias > 0) AS internal_up,
  count(*) FILTER (WHERE internal_bias = 0) AS internal_neutral,
  count(*) FILTER (WHERE internal_bias < 0) AS internal_down,
  count(*) FILTER (WHERE structure_alignment = '共振') AS alignment_resonance,
  count(*) FILTER (WHERE structure_alignment = '背离') AS alignment_divergence,
  -- MOMENTUM
  count(*) FILTER (WHERE momentum_direction = 'expanding') AS momentum_expanding,
  count(*) FILTER (WHERE momentum_direction = 'flat') AS momentum_flat,
  count(*) FILTER (WHERE momentum_direction = 'contracting') AS momentum_contracting,
  -- PARTICIPATION (threshold-free distribution)
  percentile_cont(0.25) WITHIN GROUP (ORDER BY review_volume_ratio20) AS vol_ratio20_p25,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY review_volume_ratio20) AS vol_ratio20_p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY review_volume_ratio20) AS vol_ratio20_p75,
  percentile_cont(0.25) WITHIN GROUP (ORDER BY review_amount_ratio20) AS amt_ratio20_p25,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY review_amount_ratio20) AS amt_ratio20_p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY review_amount_ratio20) AS amt_ratio20_p75,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY review_volume_percentile20) AS vol_pct20_median,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY review_amount_percentile200) AS amt_pct200_median,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY fp_segment_volume_ratio) AS seg_vol_ratio_median
FROM mem_state
GROUP BY board_id, board_name, board_type, td, def_from, version_id, pit_members
ORDER BY board_type, board_name, td;
