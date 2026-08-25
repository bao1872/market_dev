-- S2 individual contribution facts (EXACT-T1; top contributors per scope-day).
-- EXACT canonical T-1 (BLOCKER #1 fixed): explicit T -> canonical_T1 mapping + two bars joins.
--   member_return_1d = bt.close / bp.close - 1 (bt at T, bp at exact canonical_T1). NO LAG(close); no fallback.
-- PRICE valid universe  = PIT(T) ∩ valid ∩ close(T) ∩ close(exact T-1)   -> price_valid_count
-- PRICE candidate       = PIT(T) ∩ valid ∩ close(T)                      -> price_candidate_count
-- AMOUNT valid universe = PIT(T) ∩ valid ∩ amount(T) non-null (no T-1)   -> amount_valid_count
-- Signed full-universe validation (BLOCKER #2 fixed) is computed DB-native over the FULL price-valid
-- universe (NOT the top-N subset):
--   equal_weight_return_mean       = AVG(member_return_1d)
--   sum_signed_return_contribution = SUM(member_return_1d / price_valid_count)
--   signed_contribution_delta      = sum_signed_return_contribution - equal_weight_return_mean
-- Four independent ranks over the FULL universe (BLOCKER #3 / contribution ranking fix):
--   positive_rank : signed_return_contribution > 0  ORDER BY signed DESC
--   negative_rank : signed_return_contribution < 0  ORDER BY signed ASC
--   abs_price_rank: ORDER BY abs_price_change_share DESC
--   amount_rank   : ORDER BY amount_share DESC
-- Output file keeps rows with any rank <= 10 (true global top per list, not abs-top-40 then re-sliced).
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
  SELECT av.board_id, av.board_name, av.board_type, av.td, av.version_id,
         COALESCE(array_agg(m.instrument_id) FILTER (WHERE m.instrument_id IS NOT NULL), ARRAY[]::uuid[]) AS pit_members
  FROM active_ver av
  LEFT JOIN board_membership_history m ON m.board_definition_version_id = av.version_id
  GROUP BY av.board_id, av.board_name, av.board_type, av.td, av.version_id
),
valid_members AS (
  SELECT p.board_id, p.board_name, p.board_type, p.td AS trade_date, s.instrument_id
  FROM pit p
  JOIN first_pyramid_history_daily_state s
    ON s.instrument_id = ANY(p.pit_members) AND s.trade_date = p.td
   AND (s.state_payload->>'valid_for_market_aggregation') = 'true'
),
-- exact-T1 member facts per (scope, instrument): amount at T, return from exact T-1 close.
-- MATERIALIZED so the dual bar-join runs ONCE and is reused by price/amount/cand/full_contrib.
mem_bar AS MATERIALIZED (
  SELECT vm.board_id, vm.board_name, vm.board_type, vm.trade_date, vm.instrument_id,
         bt.amount,
         bt.close / bp.close - 1 AS member_return_1d
  FROM valid_members vm
  JOIN canonical_pairs cp ON cp.t = vm.trade_date
  JOIN bars_daily bt ON bt.instrument_id = vm.instrument_id AND bt.trade_date = vm.trade_date
  LEFT JOIN bars_daily bp ON bp.instrument_id = vm.instrument_id AND bp.trade_date = cp.t1
),
price_universe AS (
  SELECT board_id, board_name, board_type, trade_date, instrument_id, member_return_1d
  FROM mem_bar WHERE member_return_1d IS NOT NULL
),
amount_universe AS (
  SELECT board_id, board_name, board_type, trade_date, instrument_id, amount
  FROM mem_bar WHERE amount IS NOT NULL
),
-- scope-level totals from the FULL universe (independent, never COALESCE each other's count)
-- Full-universe signed validation (BLOCKER #2): computed over ALL price-valid members, NOT the top-N.
--   signed_return_contribution_i = member_return_1d / price_valid_count, so
--   sum_signed_return_contribution = SUM(member_return_1d) / price_valid_count = equal_weight_return_mean
--   sum_abs_price_change_share = SUM(|ret|)/total_abs_ret = 1 over ALL price-valid members
price_tot AS MATERIALIZED (
  SELECT board_id, board_type, trade_date,
         count(*) AS price_valid_count,
         sum(abs(member_return_1d)) AS total_abs_ret,
         avg(member_return_1d) AS equal_weight_return_mean,
         sum(member_return_1d) / count(*) AS sum_signed_return_contribution,
         1.0 AS sum_abs_price_change_share
  FROM price_universe GROUP BY board_id, board_type, trade_date
),
amount_tot AS MATERIALIZED (
  SELECT board_id, board_type, trade_date,
         count(*) AS amount_valid_count,
         sum(amount) AS total_amount,
         1.0 AS sum_amount_share
  FROM amount_universe GROUP BY board_id, board_type, trade_date
),
cand_tot AS MATERIALIZED (
  SELECT board_id, board_type, trade_date, count(*) AS price_candidate_count
  FROM mem_bar GROUP BY board_id, board_type, trade_date
),
-- per-member contributor facts for the full price universe (for ranking), carrying scope-level
-- price/amount counts and full-universe signed validation. MATERIALIZED once, then ranked.
-- Scope-level price/amount counts are INDEPENDENT (price_valid_count from price_tot,
-- amount_valid_count from amount_tot; never COALESCE each other's semantics).
full_contrib AS MATERIALIZED (
  SELECT mb.board_id, mb.board_name, mb.board_type, mb.trade_date, mb.instrument_id,
         mb.member_return_1d,
         mb.member_return_1d / pt.price_valid_count AS signed_return_contribution,
         abs(mb.member_return_1d) / pt.total_abs_ret AS abs_price_change_share,
         mb.amount / at.total_amount AS amount_share,
         pt.price_valid_count, pt.total_abs_ret,
         at.amount_valid_count, at.total_amount,
         ct.price_candidate_count,
         (ct.price_candidate_count - pt.price_valid_count) AS missing_exact_t1_count,
         pt.equal_weight_return_mean,
         pt.sum_signed_return_contribution,
         (pt.sum_signed_return_contribution - pt.equal_weight_return_mean) AS signed_contribution_delta,
         pt.sum_abs_price_change_share,
         at.sum_amount_share
  FROM mem_bar mb
  JOIN price_tot pt
    ON pt.board_id = mb.board_id AND pt.board_type = mb.board_type AND pt.trade_date = mb.trade_date
  LEFT JOIN amount_tot at
    ON at.board_id = mb.board_id AND at.board_type = mb.board_type AND at.trade_date = mb.trade_date
  JOIN cand_tot ct
    ON ct.board_id = mb.board_id AND ct.board_type = mb.board_type AND ct.trade_date = mb.trade_date
  WHERE mb.member_return_1d IS NOT NULL
)
SELECT * FROM (
  SELECT fc.board_id, fc.board_name, fc.board_type, fc.trade_date, fc.instrument_id::text,
         i.symbol, i.name,
         fc.member_return_1d, fc.signed_return_contribution, fc.abs_price_change_share, fc.amount_share,
         fc.price_valid_count, fc.total_abs_ret,
         fc.amount_valid_count, fc.total_amount,
         fc.price_candidate_count, fc.missing_exact_t1_count,
         fc.equal_weight_return_mean, fc.sum_signed_return_contribution, fc.signed_contribution_delta,
         fc.sum_abs_price_change_share, fc.sum_amount_share,
         row_number() OVER (PARTITION BY fc.board_type, fc.board_id, fc.trade_date
                            ORDER BY fc.signed_return_contribution DESC) AS positive_rank,
         row_number() OVER (PARTITION BY fc.board_type, fc.board_id, fc.trade_date
                            ORDER BY fc.signed_return_contribution ASC) AS negative_rank,
         row_number() OVER (PARTITION BY fc.board_type, fc.board_id, fc.trade_date
                            ORDER BY fc.abs_price_change_share DESC) AS abs_price_rank,
         row_number() OVER (PARTITION BY fc.board_type, fc.board_id, fc.trade_date
                            ORDER BY fc.amount_share DESC NULLS LAST) AS amount_rank
  FROM full_contrib fc
  LEFT JOIN instruments i ON i.id = fc.instrument_id
) r
-- keep a row if it is in the TRUE global top-10 of ANY of the four independent lists
WHERE r.positive_rank <= 10 OR r.negative_rank <= 10 OR r.abs_price_rank <= 10 OR r.amount_rank <= 10
ORDER BY r.board_type, r.board_id, r.trade_date, r.positive_rank;
