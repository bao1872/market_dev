"""S1 minimal contract tests (§16)。纯单元测试，不连 DB。

覆盖：
- CURRENT_ONLY membership 不得进入历史样本
- PIT membership date <= observation trade_date
- denominator = 当日有效 PIT members
- state ratios 使用同一 denominator
- ratio 求和一致（互斥分类状态）
- 无 future membership / facts
"""
from __future__ import annotations

import unittest
from datetime import date

from experiments.scope_observation.s1.s1_contracts import (
    MembershipWindow,
    ScopeObsRow,
    categorical_sum_to_axis_denominator,
    is_current_only,
    membership_applies_at,
    momentum_flat_is_valid,
    mutually_exclusive_ratios_sum_ok,
    no_future_facts,
    no_future_membership,
    one_snapshot_cannot_backfill_newer_versions,
    ratio_contract,
    resolve_pit_definition_at,
    swing_neutral_is_valid,
    valid_denominator,
)


class S1ContractTests(unittest.TestCase):
    def test_current_only_membership_cannot_enter_historical_sample(self) -> None:
        # membership 从 2026-08-01 才生效，历史观察日 2026-02-09 无法 PIT 覆盖
        current = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=None)
        self.assertTrue(is_current_only(current, date(2026, 2, 9)))
        # 观察日在 effective_from 当天或之后 → 不是 current-only（可覆盖）
        self.assertFalse(is_current_only(current, date(2026, 8, 1)))

    def test_pit_membership_applies_only_when_date_in_window(self) -> None:
        w = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=None)
        self.assertTrue(membership_applies_at(w, date(2026, 8, 3)))
        self.assertFalse(membership_applies_at(w, date(2026, 7, 31)))  # before effective_from
        closed = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=date(2026, 8, 5))
        self.assertFalse(membership_applies_at(closed, date(2026, 8, 6)))  # past effective_to

    def test_denominator_is_valid_pit_members(self) -> None:
        row = ScopeObsRow(
            scope_id="gold",
            trade_date=date(2026, 8, 3),
            member_count=80,
            state_counts={"regime_up": 2, "regime_neutral": 38, "regime_down": 40},
        )
        self.assertEqual(valid_denominator(row), 80)

    def test_state_ratios_use_same_denominator(self) -> None:
        row = ScopeObsRow(
            scope_id="market",
            trade_date=date(2026, 8, 3),
            member_count=5277,
            state_counts={"regime_up": 155, "regime_neutral": 2964, "regime_down": 2158},
        )
        ratios = ratio_contract(row.state_counts, valid_denominator(row))
        # 每个 ratio 都以 5277 为分母
        self.assertAlmostEqual(ratios["regime_up"], 155 / 5277)
        self.assertAlmostEqual(ratios["regime_down"], 2158 / 5277)

    def test_mutually_exclusive_ratios_sum_to_one(self) -> None:
        # regime up/neutral/down 互斥且全覆盖
        counts = {"regime_up": 155, "regime_neutral": 2964, "regime_down": 2158}
        self.assertTrue(mutually_exclusive_ratios_sum_ok(counts, 5277))
        # 若计数之和 != denominator，则不一致
        self.assertFalse(mutually_exclusive_ratios_sum_ok({"regime_up": 10, "regime_down": 10}, 5277))

    def test_no_future_membership(self) -> None:
        w = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=None)
        # effective_from 晚于观察日 = future membership
        self.assertFalse(no_future_membership(w, date(2026, 7, 31)))
        self.assertTrue(no_future_membership(w, date(2026, 8, 1)))

    def test_no_future_facts(self) -> None:
        self.assertFalse(no_future_facts(date(2026, 8, 10), date(2026, 8, 3)))
        self.assertTrue(no_future_facts(date(2026, 8, 3), date(2026, 8, 3)))


class S1CorrectionRegressionTests(unittest.TestCase):
    """S1 Correction §7 regression tests.

    覆盖：
    - one membership snapshot cannot be reused for multiple dates if a newer version intervenes
    - swing_bias=0 is valid neutral state
    - momentum flat is valid state
    - categorical state complete distribution sums to axis denominator
    - candidate_axis momentum fields == MOMENTUM
    """

    def test_one_snapshot_cannot_be_reused_when_newer_version_intervenes(self) -> None:
        # 08-01 legacy 版本（effective_to=08-03）与 08-03 开放版本
        legacy = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=date(2026, 8, 3))
        pit = MembershipWindow(effective_from=date(2026, 8, 3), effective_to=None)
        versions = [legacy, pit]
        # 08-05 必须 resolve 到 08-03 版本，不能复用 legacy 08-01 snapshot
        self.assertEqual(resolve_pit_definition_at(versions, date(2026, 8, 5)), pit)
        self.assertFalse(one_snapshot_cannot_backfill_newer_versions(versions, date(2026, 8, 5), legacy))
        self.assertTrue(one_snapshot_cannot_backfill_newer_versions(versions, date(2026, 8, 5), pit))
        # 08-03 当日 legacy 已过期（effective_to=08-03 不覆盖 08-03），仍应选 08-03
        self.assertEqual(resolve_pit_definition_at(versions, date(2026, 8, 3)), pit)

    def test_resolve_pit_definition_selects_latest_active(self) -> None:
        v1 = MembershipWindow(effective_from=date(2026, 8, 1), effective_to=None)
        v2 = MembershipWindow(effective_from=date(2026, 8, 5), effective_to=None)
        # 08-07 上 v1 与 v2 都有效，取最新 v2
        self.assertEqual(resolve_pit_definition_at([v1, v2], date(2026, 8, 7)), v2)
        # 08-03 上只有 v1 有效
        self.assertEqual(resolve_pit_definition_at([v1, v2], date(2026, 8, 3)), v1)
        # 无有效版本
        self.assertIsNone(resolve_pit_definition_at([MembershipWindow(date(2026, 8, 10), None)], date(2026, 8, 3)))

    def test_swing_bias_zero_is_valid_neutral(self) -> None:
        # 1=上行 / 0=震荡 / -1=下行，0 是合法 neutral，不是 invalid
        self.assertTrue(swing_neutral_is_valid("0"))
        self.assertTrue(swing_neutral_is_valid("1"))
        self.assertTrue(swing_neutral_is_valid("-1"))
        self.assertFalse(swing_neutral_is_valid("2"))
        self.assertFalse(swing_neutral_is_valid(None))

    def test_momentum_flat_is_valid_state(self) -> None:
        # expanding / flat / contracting 都是合法状态；flat 不是 invalid
        self.assertTrue(momentum_flat_is_valid("flat"))
        self.assertTrue(momentum_flat_is_valid("expanding"))
        self.assertTrue(momentum_flat_is_valid("contracting"))
        self.assertFalse(momentum_flat_is_valid("unknown"))
        self.assertFalse(momentum_flat_is_valid(None))

    def test_categorical_state_distribution_sums_to_axis_denominator(self) -> None:
        # 完整分类分布之和必须等于 axis denominator（含 neutral/flat）
        self.assertTrue(categorical_sum_to_axis_denominator({"up": 227, "neutral": 9, "down": 357}, 593))
        self.assertTrue(categorical_sum_to_axis_denominator({"expanding": 2320, "flat": 0, "contracting": 2957}, 5277))
        # 若漏掉 neutral/flat 导致不完整，则违反 contract
        self.assertFalse(categorical_sum_to_axis_denominator({"up": 227, "down": 357}, 593))
        self.assertFalse(categorical_sum_to_axis_denominator({"expanding": 2320, "contracting": 2957}, 5278))

    def test_candidate_axis_momentum_fields_are_momentum(self) -> None:
        # §4：momentum 字段 candidate_axis 必须 == MOMENTUM（回归：曾经标为 STRUCTURE/PRICE）
        momentum_fields = {
            "momentum_direction", "momentum_change", "sqzmom_val", "sqzmom_delta",
            "volatility_phase", "fp_momentum_direction", "fp_momentum_change",
        }
        import csv
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "out" / "scope_observation_component_inventory.csv"
        axis_by_field: dict[str, str] = {}
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                axis_by_field[row["raw_field"]] = row["candidate_axis"]
        for field in momentum_fields:
            self.assertEqual(
                axis_by_field.get(field),
                "MOMENTUM",
                f"{field} 的 candidate_axis 应为 MOMENTUM，实际得到 {axis_by_field.get(field)}",
            )


if __name__ == "__main__":
    unittest.main()