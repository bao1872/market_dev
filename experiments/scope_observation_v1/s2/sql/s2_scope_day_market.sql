-- S2 market control: full-A-share daily state cross-section.
-- SEMANTICS (FINAL AUDIT CLOSURE):
--   market_universe_count = ALL first_pyramid_history_daily_state rows that day (no valid filter)
--   fp_valid_count         = subset with valid_for_market_aggregation = true
-- Market has NO board PIT membership; pit_member_count is NOT meaningful here and is NOT set.
-- Valid-set change is recorded as market_valid_universe_changed (NOT membership_changed).
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
),
universe AS (
  SELECT s.trade_date,
         count(*) AS market_universe_count,
         count(*) FILTER (WHERE (s.state_payload->>'valid_for_market_aggregation') = 'true') AS fp_valid_count
  FROM first_pyramid_history_daily_state s
  WHERE s.trade_date IN (SELECT td FROM days)
  GROUP BY s.trade_date
),
ms AS (
  SELECT s.instrument_id, s.trade_date,
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
  FROM first_pyramid_history_daily_state s
  WHERE s.trade_date IN (SELECT td FROM days)
    AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
)
SELECT
  'FULL_MARKET' AS board_id, '全A横截面' AS board_name, 'market' AS board_type, ms.trade_date,
  ms.trade_date AS definition_effective_from, 'market_state_cross_section' AS membership_version,
  u.market_universe_count AS market_universe_count,
  u.fp_valid_count AS fp_valid_count,
  count(*) AS fp_row_count,
  count(*) FILTER (WHERE ms.regime_value > 0) AS regime_up,
  count(*) FILTER (WHERE ms.regime_value = 0) AS regime_neutral,
  count(*) FILTER (WHERE ms.regime_value < 0) AS regime_down,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY ms.regime_strength) AS regime_strength_median,
  count(*) FILTER (WHERE ms.swing_bias > 0) AS swing_up,
  count(*) FILTER (WHERE ms.swing_bias = 0) AS swing_neutral,
  count(*) FILTER (WHERE ms.swing_bias < 0) AS swing_down,
  count(*) FILTER (WHERE ms.internal_bias > 0) AS internal_up,
  count(*) FILTER (WHERE ms.internal_bias = 0) AS internal_neutral,
  count(*) FILTER (WHERE ms.internal_bias < 0) AS internal_down,
  count(*) FILTER (WHERE ms.structure_alignment = '共振') AS alignment_resonance,
  count(*) FILTER (WHERE ms.structure_alignment = '背离') AS alignment_divergence,
  count(*) FILTER (WHERE ms.momentum_direction = 'expanding') AS momentum_expanding,
  count(*) FILTER (WHERE ms.momentum_direction = 'flat') AS momentum_flat,
  count(*) FILTER (WHERE ms.momentum_direction = 'contracting') AS momentum_contracting,
  percentile_cont(0.25) WITHIN GROUP (ORDER BY ms.review_volume_ratio20) AS vol_ratio20_p25,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms.review_volume_ratio20) AS vol_ratio20_p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY ms.review_volume_ratio20) AS vol_ratio20_p75,
  percentile_cont(0.25) WITHIN GROUP (ORDER BY ms.review_amount_ratio20) AS amt_ratio20_p25,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms.review_amount_ratio20) AS amt_ratio20_p50,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY ms.review_amount_ratio20) AS amt_ratio20_p75,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms.review_volume_percentile20) AS vol_pct20_median,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms.review_amount_percentile200) AS amt_pct200_median,
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms.fp_segment_volume_ratio) AS seg_vol_ratio_median
FROM ms JOIN universe u ON u.trade_date = ms.trade_date
GROUP BY ms.trade_date, u.market_universe_count, u.fp_valid_count
ORDER BY ms.trade_date;
