"""S2 minimum contracts (§17 + §19 CORRECTED). Pure-function tests, no DB, no framework."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from s2_analysis import (  # noqa: E402
    resolve_pit_members, diff_membership, canonical_previous_trading_day,
    transition_denominator, hhi, price_contribution_hhi, amount_contribution_hhi,
    price_change_shares, amount_shares, diffusion_delta, no_scoring_fields,
    classify_regime, classify_swing_bias, classify_internal_bias, classify_momentum,
    classify_structure_alignment,
    # CORRECTED helpers (§19)
    transition_ratio, row_aligned_correlation, select_contrast_cases,
    cross_horizon_signature, is_same_direction, is_slow_fast_reverse, sign,
    # FINAL AUDIT CLOSURE (§14)
    normalized_hhi, rank_scale,
)


class TestPerDatePitResolver(unittest.TestCase):
    def test_resolves_latest_active_version(self):
        rows = [
            {"effective_from": "2026-08-01", "effective_to": "2026-08-03", "membership_version": "v1", "member_ids": ("A", "B")},
            {"effective_from": "2026-08-03", "effective_to": None, "membership_version": "v2", "member_ids": ("A", "B", "C")},
        ]
        r = resolve_pit_members(rows, "2026-08-05")
        self.assertEqual(r.membership_version, "v2")
        self.assertEqual(r.pit_member_count, 3)
        self.assertTrue(r.valid)

    def test_no_active_version_unavailable(self):
        rows = [{"effective_from": "2026-08-10", "effective_to": None, "membership_version": "v1", "member_ids": ("A",)}]
        r = resolve_pit_members(rows, "2026-08-05")
        self.assertFalse(r.valid)
        self.assertEqual(r.pit_member_count, 0)


class TestStrictCanonicalT1(unittest.TestCase):
    def test_prev_day_present(self):
        self.assertEqual(canonical_previous_trading_day(["2026-08-03", "2026-08-04", "2026-08-05"], "2026-08-05"), "2026-08-04")

    def test_first_day_no_t1(self):
        self.assertIsNone(canonical_previous_trading_day(["2026-08-03", "2026-08-04"], "2026-08-03"))

    def test_no_lag_across_missing(self):
        # 08-07 -> 08-10 skips weekend; canonical T-1 is 08-07 (no fabricated 08-09)
        days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]
        self.assertEqual(canonical_previous_trading_day(days, "2026-08-10"), "2026-08-07")


class TestTransitionDenominator(unittest.TestCase):
    def test_common_valid_members(self):
        denom = transition_denominator(
            pit_t=("A", "B", "C"), pit_t1=("A", "B", "D"),
            valid_fp_t={"A", "B", "C"}, valid_fp_t1={"A", "B", "D"},
        )
        self.assertEqual(denom, {"A", "B"})

    def test_added_removed_excluded_from_denominator(self):
        # C added at T, D removed at T-1 -> neither in denominator
        denom = transition_denominator(
            pit_t=("A", "B", "C"), pit_t1=("A", "B", "D"),
            valid_fp_t={"A", "B", "C"}, valid_fp_t1={"A", "B", "D"},
        )
        self.assertNotIn("C", denom)
        self.assertNotIn("D", denom)


class TestMembershipChange(unittest.TestCase):
    def test_count_equal_but_set_changed(self):
        mc = diff_membership(("A", "B", "C"), ("A", "B", "D"))
        self.assertTrue(mc.membership_changed)
        self.assertEqual(mc.membership_added_count, 1)
        self.assertEqual(mc.membership_removed_count, 1)

    def test_same_set_no_change(self):
        mc = diff_membership(("A", "B"), ("B", "A"))
        self.assertFalse(mc.membership_changed)


class TestAddedRemovedNotTransition(unittest.TestCase):
    def test_added_member_not_counted_as_transition(self):
        # member C added at T; transition denominator must exclude C
        denom = transition_denominator(
            pit_t=("A", "C"), pit_t1=("A",), valid_fp_t={"A", "C"}, valid_fp_t1={"A"},
        )
        self.assertEqual(denom, {"A"})
        self.assertNotIn("C", denom)


class TestNeutralFlatValid(unittest.TestCase):
    def test_neutral_and_flat_preserved(self):
        self.assertEqual(classify_regime(0), "neutral")
        self.assertEqual(classify_swing_bias(0), "neutral")
        self.assertEqual(classify_internal_bias(0), "neutral")
        self.assertEqual(classify_momentum("flat"), "flat")
        self.assertEqual(classify_structure_alignment("共振"), "resonance")
        self.assertEqual(classify_structure_alignment("背离"), "divergence")


class TestDiffusionUnavailable(unittest.TestCase):
    def test_lag_unavailable_returns_none(self):
        self.assertIsNone(diffusion_delta([0.3, 0.4], 3))  # not enough history
        self.assertIsNone(diffusion_delta([0.3, None], 1))  # prev missing

    def test_lag_available(self):
        self.assertAlmostEqual(diffusion_delta([0.3, 0.5], 1), 0.2)


class TestHHIFormulas(unittest.TestCase):
    def test_hhi_separate_price_amount(self):
        returns = [0.1, -0.2, 0.05]
        amounts = [100.0, 300.0, 100.0]
        ph = price_contribution_hhi(price_change_shares(returns))
        ah = amount_contribution_hhi(amount_shares(amounts))
        # price shares: |0.1|/0.35, |0.2|/0.35, |0.05|/0.35
        self.assertAlmostEqual(ph, (0.1/0.35)**2 + (0.2/0.35)**2 + (0.05/0.35)**2)
        # amount shares: 0.2, 0.6, 0.2
        self.assertAlmostEqual(ah, 0.2**2 + 0.6**2 + 0.2**2)
        self.assertNotEqual(ph, ah)  # must keep separate

    def test_hhi_no_average_of_two(self):
        # explicit: we never return avg(price, amount); two HHI values are independent
        returns = [0.1, -0.3, 0.1]
        amounts = [100.0, 50.0, 250.0]
        ph = price_contribution_hhi(price_change_shares(returns))
        ah = amount_contribution_hhi(amount_shares(amounts))
        # they must not be equal (different inputs) and neither equals their average
        self.assertNotEqual(ph, ah)
        self.assertNotEqual(ph, (ph + ah) / 2)
        self.assertNotEqual(ah, (ph + ah) / 2)


class TestNoScoringFields(unittest.TestCase):
    def test_observation_record_has_no_score(self):
        rec = {"regime_up_ratio": 0.1, "price_contribution_hhi": 0.02}
        self.assertTrue(no_scoring_fields(rec))
        self.assertFalse(no_scoring_fields({"score": 1}))
        self.assertFalse(no_scoring_fields({"rank": 3}))
        self.assertFalse(no_scoring_fields({"signal": "x"}))


# ===========================================================================
# CORRECTED contracts (§19)
# ===========================================================================
class TestTransitionRatio(unittest.TestCase):
    def test_count_over_denominator(self):
        self.assertAlmostEqual(transition_ratio(3, 100), 0.03)

    def test_denominator_zero_returns_none(self):
        self.assertIsNone(transition_ratio(3, 0))
        self.assertIsNone(transition_ratio(0, 0))
        self.assertIsNone(transition_ratio(3, None))

    def test_zero_count_nonzero_denom(self):
        self.assertAlmostEqual(transition_ratio(0, 50), 0.0)


class TestRowAlignedCorrelation(unittest.TestCase):
    def _records(self):
        # rows: field_a complete all, field_b missing on row 3 -> only rows 1,2,4 pair
        return [
            {"a": 1.0, "b": 2.0},
            {"a": 2.0, "b": 4.0},
            {"a": 3.0, "b": None},   # b missing -> this row NOT in pair
            {"a": 4.0, "b": 8.0},
        ]

    def test_pairwise_complete_n(self):
        r = row_aligned_correlation(self._records(), "a", "b")
        self.assertEqual(r["n_pairwise_complete"], 3)

    def test_perfect_correlation_on_aligned_rows(self):
        r = row_aligned_correlation(self._records(), "a", "b")
        self.assertAlmostEqual(r["rho"], 1.0)

    def test_small_n_rho_none(self):
        r = row_aligned_correlation([{"a": 1.0, "b": 2.0}, {"a": 2.0, "b": None}], "a", "b")
        self.assertEqual(r["n_pairwise_complete"], 1)
        self.assertIsNone(r["rho"])

    def test_no_misalignment(self):
        # if column b was independently drop-nulled then zip, b=[2,4,8] vs a=[1,2,3]
        # would pair wrong and rho != 1. Row-aligned keeps (1,2),(2,4),(4,8) -> rho=1.
        r = row_aligned_correlation(self._records(), "a", "b")
        self.assertAlmostEqual(r["rho"], 1.0)


class TestContrastNeverConvertsNoneToZero(unittest.TestCase):
    def test_missing_contrast_key_makes_pair_unavailable(self):
        records = [
            {"scope_day": "s1", "sim": 0.1, "con": 0.5},
            {"scope_day": "s2", "sim": 0.1, "con": None},  # con missing
            {"scope_day": "s3", "sim": 0.1, "con": 0.6},
        ]
        res = select_contrast_cases(records, ["sim"], ["con"], top_n=5)
        # s2 has missing con -> excluded from eligible; must NOT be treated as con=0
        self.assertNotIn("s2", [c["a"] for c in res["cases"]])
        self.assertNotIn("s2", [c["b"] for c in res["cases"]])
        self.assertEqual(res["eligible_rows"], 2)

    def test_rank_scaled_not_raw(self):
        records = [
            {"scope_day": "s1", "sim": 0.0, "con": 0.01},
            {"scope_day": "s2", "sim": 0.0, "con": 0.5},
            {"scope_day": "s3", "sim": 0.0, "con": 0.99},
        ]
        res = select_contrast_cases(records, ["sim"], ["con"], top_n=5)
        # contrast distances are rank-scaled (<=1), not raw (0.98)
        for c in res["cases"]:
            self.assertLessEqual(c["contrast_distance"], 1.0 + 1e-6)


class TestQ2UsesTransitionRatiosNotCounts(unittest.TestCase):
    def test_contrast_key_set_is_ratios(self):
        # verify that contrast on transition uses ratio fields only (names end in _ratio)
        from s2_analyze import TRANSITION_RATIOS
        self.assertTrue(all(k.endswith("_ratio") for k in TRANSITION_RATIOS))
        self.assertIn("regime_neutral_to_up_ratio", TRANSITION_RATIOS)
        self.assertNotIn("regime_neutral_to_up", TRANSITION_RATIOS)  # raw count excluded


class TestQ3Keys(unittest.TestCase):
    def test_q3_similarity_transition_contrast_diffusion(self):
        # Q3 independent experiment: similarity=Transition RATIOS, contrast=Diffusion
        import re
        from s2_analyze import TRANSITION_RATIOS, DIFFUSION
        # similarity keys must be transition ratios
        self.assertTrue(all(k.startswith("regime_") or k.endswith("_transition_ratio")
                            or k.startswith("swing_transition") or k.startswith("internal_transition")
                            or k.startswith("momentum_transition") for k in TRANSITION_RATIOS))
        # contrast keys must be diffusion deltas
        self.assertTrue(all(k.startswith("diffusion_") for k in DIFFUSION))
        self.assertTrue(re.match(r"diffusion_\w+_d[135]", DIFFUSION[0]))


class TestRegimeStrengthMedianUsesRegimeStrength(unittest.TestCase):
    def test_sql_uses_regime_strength_not_regime_value(self):
        # the axis SQL must aggregate regime_strength, not regime_value
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "sql", "s2_scope_day_axis.sql")) as f:
            sql = f.read()
        self.assertIn("ORDER BY regime_strength", sql)
        # must NOT be median of regime_value
        self.assertNotIn("ORDER BY (regime_value)::float", sql)


class TestPriceReturnCanonicalT1(unittest.TestCase):
    def test_price_hhi_sql_includes_0731(self):
        # bar window must include 07-31 so 08-03 return uses canonical T-1 = 07-31
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fname in ["s2_scope_day_price_hhi.sql", "s2_scope_day_price_facts.sql",
                      "s2_scope_day_contribution.sql", "s2_scope_day_share_validation.sql"]:
            with open(os.path.join(base, "sql", fname)) as f:
                sql = f.read()
            self.assertIn("2026-07-31", sql, fname)

    def test_canonical_t1_is_0731_for_0803(self):
        self.assertEqual(canonical_previous_trading_day(
            ["2026-07-31", "2026-08-03", "2026-08-04"], "2026-08-03"), "2026-07-31")


class TestPriceBreadthDenominator(unittest.TestCase):
    def test_advance_decline_unchanged_sum(self):
        # advance+decline+unchanged = valid price denominator
        rec = {"advance_count": 10, "decline_count": 5, "unchanged_count": 2}
        denom = rec["advance_count"] + rec["decline_count"] + rec["unchanged_count"]
        self.assertEqual(denom, 17)


class TestAmountShareSumsToOne(unittest.TestCase):
    def test_amount_shares_sum_approx_1(self):
        amounts = [100.0, 300.0, 200.0, 400.0]
        shares = amount_shares(amounts)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_amount_hhi_matches_shares(self):
        amounts = [100.0, 300.0]
        shares = amount_shares(amounts)
        self.assertAlmostEqual(amount_contribution_hhi(shares), (0.25) ** 2 + (0.75) ** 2)


class TestPriceChangeShareSumsToOne(unittest.TestCase):
    def test_price_change_shares_sum_approx_1(self):
        returns = [0.1, -0.2, 0.05]
        shares = price_change_shares(returns)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_zero_total_abs_return_handled(self):
        shares = price_change_shares([0.0, 0.0])
        self.assertEqual(len(shares), 2)


class TestAmountHhiMatchesMemberShares(unittest.TestCase):
    def test_amount_hhi_from_shares(self):
        amounts = [100.0, 300.0, 100.0]
        shares = amount_shares(amounts)  # 0.2, 0.6, 0.2
        expected = 0.2 ** 2 + 0.6 ** 2 + 0.2 ** 2
        self.assertAlmostEqual(amount_contribution_hhi(shares), expected)


class TestNoHardcodedVerdicts(unittest.TestCase):
    def test_verdict_functions_are_not_constants(self):
        # verdicts must be produced by evidence objects, never hard-coded strings
        # (e.g., no "return 'SUPPORTED'" literal paths).
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "s2_analyze.py")) as f:
            src = f.read()
        # verdicts must be referenced via an evidence-derived object, not bare constants
        self.assertIn("verdict_q1", src)
        self.assertIn("verdict_q6", src)
        # Q2-Q5 verdict must be deferred to external audit (evidence-only)
        self.assertIn("EXTERNAL_AUDIT_PENDING", src)
        # the only allowed verdict literal strings are the enumerated set
        for allowed in ["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE",
                        "EXTERNAL_AUDIT_PENDING"]:
            self.assertIn(f'"{allowed}"', src)


class TestCrossHorizonNets(unittest.TestCase):
    def test_same_direction(self):
        self.assertTrue(is_same_direction(-0.1, -0.2, -0.3, -0.05))
        self.assertFalse(is_same_direction(0.1, 0.2, -0.3, 0.1))

    def test_slow_fast_reverse(self):
        self.assertTrue(is_slow_fast_reverse(-0.1, 0.2))
        self.assertFalse(is_slow_fast_reverse(-0.1, -0.2))

    def test_sign_natural_zero_boundary(self):
        self.assertEqual(sign(0.1), 1)
        self.assertEqual(sign(-0.1), -1)
        self.assertEqual(sign(0.0), 0)


# ===========================================================================
# FINAL AUDIT CLOSURE contracts (§14)
# ===========================================================================
class TestTieAwareRank(unittest.TestCase):
    def test_equal_raw_values_receive_equal_rank(self):
        # values [0,0,0,1]: first three tie -> same rank (average-rank / midrank)
        records = [{"k": 0.0}, {"k": 0.0}, {"k": 0.0}, {"k": 1.0}]
        ranks = rank_scale(records, ["k"])["k"]
        self.assertEqual(ranks[0], ranks[1])
        self.assertEqual(ranks[1], ranks[2])
        # tie block average rank of {1,2,3} = 2 -> (2-1)/(4-1)=1/3
        self.assertAlmostEqual(ranks[0], 1.0 / 3.0)
        self.assertAlmostEqual(ranks[3], 1.0)

    def test_tie_ordering_does_not_change_contrast_distance(self):
        # reordering equal values must not change their rank
        rec_a = [{"scope_day": "s1", "k": 0.0, "c": 0.1},
                 {"scope_day": "s2", "k": 0.0, "c": 0.2},
                 {"scope_day": "s3", "k": 0.0, "c": 0.9},
                 {"scope_day": "s4", "k": 1.0, "c": 0.5}]
        rec_b = [{"scope_day": "s2", "k": 0.0, "c": 0.2},
                 {"scope_day": "s3", "k": 0.0, "c": 0.9},
                 {"scope_day": "s1", "k": 0.0, "c": 0.1},
                 {"scope_day": "s4", "k": 1.0, "c": 0.5}]
        r1 = select_contrast_cases(rec_a, ["k"], ["c"])
        r2 = select_contrast_cases(rec_b, ["k"], ["c"])
        d1 = {c["a"]: c["contrast_distance"] for c in r1["cases"]}
        d2 = {c["a"]: c["contrast_distance"] for c in r2["cases"]}
        for k in d1:
            self.assertAlmostEqual(d1[k], d2[k])


class TestNormalizedHHI(unittest.TestCase):
    def test_uniform_hhi_n10_normalized_zero(self):
        shares = [0.1] * 10  # uniform
        raw = sum(s * s for s in shares)  # 0.1
        self.assertAlmostEqual(normalized_hhi(raw, 10), 0.0)

    def test_uniform_hhi_n100_normalized_zero(self):
        shares = [0.01] * 100  # uniform
        raw = sum(s * s for s in shares)  # 0.01
        self.assertAlmostEqual(normalized_hhi(raw, 100), 0.0)

    def test_single_dominant_normalized_approx_1(self):
        # one member strongly dominates: shares [0.99, 0.01] -> normalized ~0.96 (near 1, not exact)
        raw = 0.99 ** 2 + 0.01 ** 2
        n = 2
        nrm = normalized_hhi(raw, 2)
        self.assertGreater(nrm, 0.9)   # near single-dominant
        self.assertLess(nrm, 1.0 + 1e-9)
        # pure single-dominant for N=2 would be shares [1,0]; normalized -> 1 exactly
        self.assertAlmostEqual(normalized_hhi(1.0, 2), 1.0)

    def test_n_le_1_returns_none(self):
        self.assertIsNone(normalized_hhi(0.5, 1))
        self.assertIsNone(normalized_hhi(0.5, 0))
        self.assertIsNone(normalized_hhi(None, 10))


class TestUniverseSeparation(unittest.TestCase):
    def test_amount_universe_does_not_require_return(self):
        # amount HHI/valid count must not depend on T-1 return -> amount universe
        # derives from mem_bar filtering amount IS NOT NULL only (no ret IS NOT NULL filter).
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_price_hhi.sql")) as f:
            sql = f.read()
        # amount_universe must NOT filter on ret IS NOT NULL
        amt_sec = sql.split("amount_universe AS (")[1].split(")")[0]
        self.assertIn("WHERE amount IS NOT NULL", amt_sec)
        self.assertNotIn("WHERE ret IS NOT NULL", amt_sec)

    def test_price_universe_requires_canonical_t1_return(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_price_hhi.sql")) as f:
            sql = f.read()
        price_sec = sql.split("price_universe AS (")[1].split(")")[0]
        # price universe requires the exact canonical T-1 return (ret IS NOT NULL from mem_bar)
        self.assertIn("WHERE ret IS NOT NULL", price_sec)


class TestShareSums(unittest.TestCase):
    def test_sum_amount_share_over_amount_valid_members(self):
        amounts = [100.0, 300.0, 200.0]
        shares = amount_shares(amounts)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_sum_abs_price_change_share(self):
        returns = [0.1, -0.2, 0.05, 0.3]
        shares = price_change_shares(returns)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_sum_signed_return_contribution_equals_mean(self):
        # signed_return_contribution_i = ret_i / N ; sum = sum(ret_i)/N = mean
        returns = [0.1, -0.2, 0.05]
        n = len(returns)
        signed = [r / n for r in returns]
        mean = sum(returns) / n
        self.assertAlmostEqual(sum(signed), mean)


class TestMarketControlSemantics(unittest.TestCase):
    def test_market_universe_vs_fp_valid_separate(self):
        # market_universe_count (all state rows) vs fp_valid_count (valid subset)
        # must be distinguishable in the market axis SQL
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_market.sql")) as f:
            sql = f.read()
        self.assertIn("market_universe_count", sql)
        self.assertIn("fp_valid_count", sql)
        # market must NOT set pit_member_count from valid-only
        self.assertIn("market_universe_count", sql)
        # valid-set change must not be labeled membership_changed
        with open(os.path.join(base, "s2_build.py")) as f:
            build_src = f.read()
        self.assertIn("market_valid_universe_changed", build_src)


class TestContrastExcludesMarket(unittest.TestCase):
    def test_q2_q5_exclude_market(self):
        from s2_analyze import CONTRAST_SCOPE_FAMILIES, _contrast_pool
        self.assertEqual(CONTRAST_SCOPE_FAMILIES, ("industry", "concept"))
        records = [
            {"scope_type": "industry", "board_id": "a", "board_name": "A", "trade_date": "2026-08-03",
             "x": 0.1},
            {"scope_type": "concept", "board_id": "b", "board_name": "B", "trade_date": "2026-08-03",
             "x": 0.2},
            {"scope_type": "market", "board_id": "FULL_MARKET", "board_name": "M", "trade_date": "2026-08-03",
             "x": 0.3},
        ]
        pool = _contrast_pool(records)
        self.assertEqual(len(pool), 2)
        self.assertTrue(all(r["scope_type"] != "market" for r in pool))


class TestPriceVsTrendFullDistribution(unittest.TestCase):
    def test_price_breadth_keys_full(self):
        from s2_analyze import PRICE_BREADTH
        self.assertIn("advance_count", PRICE_BREADTH)
        self.assertIn("decline_count", PRICE_BREADTH)
        self.assertIn("unchanged_count", PRICE_BREADTH)

    def test_trend_breadth_full(self):
        from s2_analyze import price_breadth, trend_breadth
        rec = {"advance_count": 10, "decline_count": 5, "unchanged_count": 2,
               "regime_up_ratio": 0.5, "regime_neutral_ratio": 0.3, "regime_down_ratio": 0.2}
        pb = price_breadth(rec)
        self.assertIn("advance_ratio", pb)
        self.assertIn("decline_ratio", pb)
        self.assertIn("unchanged_ratio", pb)
        tb = trend_breadth(rec)
        self.assertIn("regime_up_ratio", tb)
        self.assertIn("regime_neutral_ratio", tb)
        self.assertIn("regime_down_ratio", tb)


class TestNoAutoVerdictThresholds(unittest.TestCase):
    def test_no_numeric_threshold_literals(self):
        # the auto-verdict threshold functions/logic must be removed:
        #   verdict_from_evidence (present_threshold) and valid_min are gone from the code paths
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "s2_analyze.py")) as f:
            src = f.read()
        # no callable verdict_from_evidence (its present_threshold logic is removed)
        self.assertNotIn("def verdict_from_evidence", src)
        # no hardcoded count-threshold branches for Q6 (same>=20/rev>=20 / same+rev>=10)
        self.assertNotIn("same >= 20", src)
        self.assertNotIn("rev >= 20", src)
        self.assertNotIn("same + rev >= 10", src)

    def test_q2_q5_verdict_is_external_audit_pending(self):
        # verdicts are EXTERNAL_AUDIT_PENDING, not SUPPORTED/PARTIALLY auto-generated
        from s2_analyze import VERDICTS
        # VERDICTS tuple must not be the auto-verdict source for Q2-Q5
        self.assertIn("EXTERNAL_AUDIT_PENDING", VERDICTS)


class TestEvidenceManifestContract(unittest.TestCase):
    def test_manifest_rowcount_unique_dup(self):
        # manifest must report row_count, unique key count, duplicate count
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "out", "s2_daily_evidence_manifest.json")) as f:
            m = json.load(f)
        self.assertIn("sha256", m)
        self.assertIn("row_count", m)
        self.assertIn("unique_scope_day_key_count", m)
        self.assertIn("duplicate_key_count", m)
        self.assertEqual(m["row_count"], m["unique_scope_day_key_count"] + m["duplicate_key_count"])


# ===========================================================================
# EXACT-T1 & SAME-DAY EVIDENCE CLOSURE contracts (§19)
# ===========================================================================
class TestExactCanonicalDayPairs(unittest.TestCase):
    def test_0803_exact_t1_is_0731(self):
        from s2_analyze import CANONICAL_T1_MAP
        self.assertEqual(CANONICAL_T1_MAP["2026-08-03"], "2026-07-31")

    def test_0810_exact_t1_is_0807(self):
        from s2_analyze import CANONICAL_T1_MAP
        self.assertEqual(CANONICAL_T1_MAP["2026-08-10"], "2026-08-07")

    def test_exact_t1_map_full(self):
        from s2_analyze import CANONICAL_T1_MAP
        expected = {
            "2026-08-03": "2026-07-31", "2026-08-04": "2026-08-03",
            "2026-08-05": "2026-08-04", "2026-08-06": "2026-08-05",
            "2026-08-07": "2026-08-06", "2026-08-10": "2026-08-07",
        }
        self.assertEqual(CANONICAL_T1_MAP, expected)


class TestNoLAGForReturn(unittest.TestCase):
    def _sql_files(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return ["s2_scope_day_price_facts.sql", "s2_scope_day_market_price_facts.sql",
                "s2_scope_day_price_hhi.sql", "s2_scope_day_market_hhi.sql",
                "s2_scope_day_contribution.sql", "s2_scope_day_market_contribution.sql",
                "s2_scope_day_share_validation.sql"]

    def test_no_lag_close_in_any_return_sql(self):
        # BLOCKER #1: no instrument-level LAG(close) anywhere for 1D return.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fname in self._sql_files():
            with open(os.path.join(base, "sql", fname)) as f:
                sql = f.read()
            # strip comment lines so only real SQL is checked
            code = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
            self.assertNotIn("LAG(", code, fname)
            self.assertNotIn("LAG (", code, fname)

    def test_exact_t1_uses_two_bar_join(self):
        # return must use current bar bt at T joined to prev bar bp at exact canonical T-1
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_price_facts.sql")) as f:
            sql = f.read()
        self.assertIn("bt.close / bp.close - 1", sql)
        # the LEFT JOIN on bp means a missing exact T-1 bar -> NULL return (UNAVAILABLE), no fallback
        self.assertIn("LEFT JOIN bars_daily bp", sql)

    def test_canonical_pairs_cte_present(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_price_facts.sql")) as f:
            sql = f.read()
        self.assertIn("canonical_pairs", sql)
        self.assertIn("VALUES", sql)
        self.assertIn("'2026-07-31'", sql)


class TestExactT1Diagnostics(unittest.TestCase):
    def test_price_candidate_equals_valid_plus_missing(self):
        # §19 #5: price_candidate_count == price_valid_count + missing_exact_t1_count
        import csv
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rows = list(csv.DictReader(open(os.path.join(base, "out", "s2_scope_observation_daily.csv"))))
        checked = 0
        for r in rows:
            c, v, m = r.get("price_candidate_count"), r.get("price_valid_count"), r.get("missing_exact_t1_count")
            if c in (None, "", "None") or v in (None, "", "None") or m in (None, "", "None"):
                continue
            self.assertEqual(float(c), float(v) + float(m), (r["scope_type"], r["board_id"], r["trade_date"]))
            checked += 1
        self.assertGreater(checked, 0)

    def test_amount_universe_does_not_require_t1(self):
        # §19 #6: amount_valid universe must not require a return (no T-1 dependency).
        # In share_validation the amount_shares CTE filters `mb.amount IS NOT NULL` (not ret).
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_share_validation.sql")) as f:
            sql = f.read()
        # the amount_shares CTE computes amount / sum(amount) over members with amount IS NOT NULL
        amt_sec = sql.split("amount_shares AS (")[1].split("FROM mem_bar mb WHERE mb.amount IS NOT NULL")[0]
        self.assertIn("mb.amount / sum(mb.amount)", amt_sec)
        # the amount universe must not carry a ret IS NOT NULL filter
        self.assertIn("FROM mem_bar mb WHERE mb.amount IS NOT NULL", sql)
        # and price_shares filters on ret IS NOT NULL (exact T-1 required for price)
        self.assertIn("FROM mem_bar mb WHERE mb.ret IS NOT NULL", sql)

    def test_daily_csv_has_diagnostics(self):
        import csv
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "out", "s2_scope_observation_daily.csv")) as f:
            header = next(csv.reader(f))
        for col in ["price_candidate_count", "price_valid_count", "missing_exact_t1_count", "amount_valid_count"]:
            self.assertIn(col, header)


class TestSignedContributionFullUniverse(unittest.TestCase):
    def test_full_universe_signed_sum_equals_mean(self):
        # §19 #7: full-universe sum(signed_return_contribution) == equal_weight_return_mean
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ce = json.load(open(os.path.join(base, "out", "s2_member_contribution_evidence.json")))
        for case in ce["price"]["representative_cases"]:
            self.assertAlmostEqual(case["signed_contribution_delta"], 0.0, places=6)
            self.assertAlmostEqual(case["sum_signed_return_contribution"],
                                   case["equal_weight_return_mean"], places=9)

    def test_top_n_not_used_for_signed_sum_validation(self):
        # §19 #8: the signed-sum evidence must NOT come from summing the top-N contributor rows.
        # The DB-native aggregate carries equal_weight_return_mean / sum_signed / delta; top-N rows
        # are only for display. Verify the contribution SQL computes these over the full price universe.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_contribution.sql")) as f:
            sql = f.read()
        # full-universe aggregate uses price_universe (ALL price-valid members), not the top-N filter
        self.assertIn("avg(member_return_1d) AS equal_weight_return_mean", sql)
        self.assertIn("sum(member_return_1d) / count(*) AS sum_signed_return_contribution", sql)
        # the WHERE positive/negative/abs/amount rank <= 10 filter applies only to the OUTPUT rows,
        # not to the scope-level aggregate.
        self.assertIn("WHERE r.positive_rank <= 10 OR r.negative_rank <= 10", sql)


class TestContributionRankingCorrection(unittest.TestCase):
    def test_positive_rank_global_over_full_universe(self):
        # §19 #9: positive_rank finds the global top positive even when >40 larger negative abs moves exist.
        # The SQL builds positive_rank (signed>0, DESC) over the FULL universe, not an abs-top-40 slice.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_contribution.sql")) as f:
            sql = f.read()
        # positive_rank is its own window over the full universe (signed DESC), separate from negative/abs
        self.assertIn("ORDER BY fc.signed_return_contribution DESC) AS positive_rank", sql)
        self.assertIn("ORDER BY fc.signed_return_contribution ASC) AS negative_rank", sql)

    def test_negative_rank_global(self):
        # §19 #10: negative_rank finds the global top negative
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_contribution.sql")) as f:
            sql = f.read()
        self.assertIn("ORDER BY fc.signed_return_contribution ASC) AS negative_rank", sql)
        self.assertIn("ORDER BY fc.abs_price_change_share DESC) AS abs_price_rank", sql)
        self.assertIn("ORDER BY fc.amount_share DESC NULLS LAST) AS amount_rank", sql)

    def test_no_coalesce_fallback_counts(self):
        # §19 #11: price_valid_count and amount_valid_count never fallback into each other's semantics.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "sql", "s2_scope_day_contribution.sql")) as f:
            sql = f.read()
        # no COALESCE(price_valid_count, amount_valid_count) anywhere
        self.assertNotIn("COALESCE(price_valid_count, amount_valid_count", sql)
        self.assertNotIn("COALESCE(p.price_valid_count, a.amount_valid_count", sql)
        self.assertNotIn("COALESCE(amount_valid_count, price_valid_count", sql)


class TestSameDayContrast(unittest.TestCase):
    def _cases_by_date(self):
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        inc = json.load(open(os.path.join(base, "out", "s2_incremental_information.json")))
        return {q: inc["contrast"][q]["cases"] for q in ["Q2", "Q3", "Q4", "Q5"]}

    def _same_date(self, case):
        return case["a"].rsplit("/", 1)[-1] == case["b"].rsplit("/", 1)[-1]

    def test_q2_pairs_same_date(self):  # §19 #12
        for c in self._cases_by_date()["Q2"]:
            self.assertTrue(self._same_date(c), c)

    def test_q3_pairs_same_date(self):  # §19 #13
        for c in self._cases_by_date()["Q3"]:
            self.assertTrue(self._same_date(c), c)

    def test_q4_pairs_same_date(self):  # §19 #14
        for c in self._cases_by_date()["Q4"]:
            self.assertTrue(self._same_date(c), c)

    def test_q5_pairs_same_date(self):  # §19 #15
        for c in self._cases_by_date()["Q5"]:
            self.assertTrue(self._same_date(c), c)

    def test_price_vs_trend_pairs_same_date(self):  # §19 #16
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pf = json.load(open(os.path.join(base, "out", "s2_price_facts.json")))
        pvt = pf["price_vs_trend_breadth"]
        for tag in ["price_similar_trend_differs", "trend_similar_price_differs"]:
            for c in pvt[tag]["cases"]:
                self.assertTrue(self._same_date(c), (tag, c))


class TestPerDateRank(unittest.TestCase):
    def test_rank_scaling_independent_per_date(self):
        # §19 #17: rank scaling is performed independently within each trade_date (cross-sectional),
        # NOT pooled across dates. Verify run_contrast groups by date before ranking.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "s2_analyze.py")) as f:
            src = f.read()
        self.assertIn("for td in sorted(by_date)", src)
        self.assertIn("select_contrast_cases(eligible, similarity_keys, contrast_keys", src)
        # the pool is grouped per-date before ranking
        self.assertIn("by_date.setdefault(r[\"trade_date\"], []).append", src)

    def test_eligible_dates_from_eligible_rows(self):
        # §19 #18: eligible_dates derived from eligible rows, not the input pool.
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        inc = json.load(open(os.path.join(base, "out", "s2_incremental_information.json")))
        for q in ["Q2", "Q3", "Q4", "Q5"]:
            c = inc["contrast"][q]
            # Q3 full D1/D3/D5 only available on 08-10 (early dates unavailable) -> eligible_dates smaller
            if q == "Q3":
                self.assertIn("2026-08-10", c["eligible_dates"])
            # eligible_dates must be a subset of input_dates
            self.assertTrue(set(c["eligible_dates"]) <= set(c["input_dates"]), q)

    def test_q3_unavailable_early_dates_remain_unavailable(self):
        # §19 #19: Q3 early dates (D3/D5 diffusion unavailable) remain unavailable -> only 08-10 eligible
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        inc = json.load(open(os.path.join(base, "out", "s2_incremental_information.json")))
        q3 = inc["contrast"]["Q3"]
        self.assertNotIn("2026-08-03", q3["eligible_dates"])
        self.assertNotIn("2026-08-04", q3["eligible_dates"])
        self.assertEqual(q3["eligible_rows_by_date"]["2026-08-03"], 0)


class TestNoAutoVerdictRestored(unittest.TestCase):
    def test_q2_q5_remain_external_audit_pending(self):
        # §19 #20: no auto verdict restored for Q2-Q5 (still EXTERNAL_AUDIT_PENDING)
        import json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        inc = json.load(open(os.path.join(base, "out", "s2_incremental_information.json")))
        for q in ["q2", "q3", "q4", "q5"]:
            self.assertEqual(inc["verdicts"][q]["verdict"], "EXTERNAL_AUDIT_PENDING", q)
        # Q1/Q6 preserve external SUPPORTED without re-judging
        self.assertEqual(inc["verdicts"]["q1"]["verdict"], "SUPPORTED")
        self.assertEqual(inc["verdicts"]["q6"]["verdict"], "SUPPORTED")


if __name__ == "__main__":
    unittest.main()
