-- S2 share-validation (EXACT-T1): per scope-day confirm the per-member
-- amount_share and abs_price_change_share sums are ~= 1 over ALL valid PIT members.
-- EXACT canonical T-1: explicit T -> canonical_T1 mapping + two bars joins. NO LAG(close); no fallback.
--   amount_share over AMOUNT-valid members (no T-1 return required) -> sum == 1
--   abs_price_change_share over PRICE-valid members (exact canonical T-1 return) -> sum == 1
--   price_candidate_count / price_valid_count / missing_exact_t1_count also reported.
-- DB-native aggregation (member share = value / group-total via window, then sum). Compact.
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
valid_members AS (
  SELECT p.board_id, p.board_type, p.td AS trade_date, s.instrument_id
  FROM pit p
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = ANY(p.pit_members) AND s.trade_date = p.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
),
mem_bar AS (
  SELECT vm.board_id, vm.board_type, vm.trade_date, vm.instrument_id,
         bt.amount,
         bt.close / bp.close - 1 AS ret
  FROM valid_members vm
  JOIN canonical_pairs cp ON cp.t = vm.trade_date
  JOIN bars_daily bt ON bt.instrument_id = vm.instrument_id AND bt.trade_date = vm.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = vm.instrument_id AND bp.trade_date = cp.t1
),
-- price-valid members with per-member abs-price share (value / group-total via window)
price_shares AS (
  SELECT mb.board_id, mb.board_type, mb.trade_date,
         abs(mb.ret) / sum(abs(mb.ret)) OVER (PARTITION BY mb.board_id, mb.board_type, mb.trade_date) AS price_share
  FROM mem_bar mb WHERE mb.ret IS NOT NULL
),
-- amount-valid members with per-member amount share
amount_shares AS (
  SELECT mb.board_id, mb.board_type, mb.trade_date,
         mb.amount / sum(mb.amount) OVER (PARTITION BY mb.board_id, mb.board_type, mb.trade_date) AS amount_share
  FROM mem_bar mb WHERE mb.amount IS NOT NULL
),
price_check AS (
  SELECT board_id, board_type, trade_date, count(*) AS price_valid_count,
         sum(price_share) AS sum_price_change_share
  FROM price_shares GROUP BY board_id, board_type, trade_date
),
amount_check AS (
  SELECT board_id, board_type, trade_date, count(*) AS amount_valid_count,
         sum(amount_share) AS sum_amount_share
  FROM amount_shares GROUP BY board_id, board_type, trade_date
),
cand_check AS (
  SELECT board_id, board_type, trade_date, count(*) AS price_candidate_count
  FROM mem_bar GROUP BY board_id, board_type, trade_date
)
SELECT COALESCE(p.board_id, a.board_id) AS board_id,
       COALESCE(p.board_type, a.board_type) AS board_type,
       COALESCE(p.trade_date, a.trade_date) AS trade_date,
       COALESCE(c.price_candidate_count, 0) AS price_candidate_count,
       COALESCE(p.price_valid_count, 0) AS price_valid_count,
       COALESCE(c.price_candidate_count, 0) - COALESCE(p.price_valid_count, 0) AS missing_exact_t1_count,
       COALESCE(a.amount_valid_count, 0) AS amount_valid_count,
       COALESCE(a.sum_amount_share, 0) AS sum_amount_share,
       COALESCE(p.sum_price_change_share, 0) AS sum_price_change_share
FROM price_check p
FULL OUTER JOIN amount_check a
  ON a.board_id = p.board_id AND a.board_type = p.board_type AND a.trade_date = p.trade_date
FULL OUTER JOIN cand_check c
  ON c.board_id = COALESCE(p.board_id, a.board_id) AND c.board_type = COALESCE(p.board_type, a.board_type)
 AND c.trade_date = COALESCE(p.trade_date, a.trade_date)
ORDER BY COALESCE(p.board_type, a.board_type), COALESCE(p.board_id, a.board_id), COALESCE(p.trade_date, a.trade_date);
