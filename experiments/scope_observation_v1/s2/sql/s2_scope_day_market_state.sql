-- S2 market control state export (valid full-A cross-section per day).
WITH days AS (
  SELECT unnest(ARRAY['2026-08-03','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-10'])::date AS td
)
SELECT 'FULL_MARKET' AS board_id, 'market' AS board_type, s.trade_date,
       s.instrument_id::text,
       (s.state_payload->>'regime_value')::int AS regime_value,
       (s.state_payload->>'swing_bias')::int AS swing_bias,
       (s.state_payload->>'internal_bias')::int AS internal_bias,
       s.state_payload->>'momentum_direction' AS momentum_direction
FROM first_pyramid_history_daily_state s
WHERE s.trade_date IN (SELECT td FROM days)
  AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
ORDER BY s.trade_date, s.instrument_id;
