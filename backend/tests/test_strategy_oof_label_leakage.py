"""OOF training must not consume labels that mature after the fold start.

The personal model's out-of-fold base scores are trained fold-by-fold on prior
rows. A row can carry a decision_date earlier than the fold start yet still have
a label confirmed only later (L1/L2 depend on future pivot confirmation). Using
such a label to fit the fold's model leaks future information. These tests pin
the fix end to end.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import lab_parity_contract as parity
from app.strategy_integration import personal_trainer as pt


def _sample(sample_id, decision_date, label_available_at, label=1,
             features=None):
    return {
        'sample_id': sample_id, 'symbol': 'X', 'decision_date': decision_date,
        'anchor_date': decision_date, 'label': label,
        'features': np.array(features if features is not None else [0.5],
                             dtype=np.float32),
        'label_available_at': label_available_at,
    }


def test_oof_prior_excludes_future_matured_label():
    """Counterexample from the review: decision before fold, label matures after."""
    fold_start = date(2024, 5, 1)
    early_decision = _sample('A', date(2024, 1, 1), date(2024, 7, 1))   # label late
    early_matured = _sample('B', date(2024, 2, 1), date(2024, 2, 2))    # label early
    future_decision = _sample('C', date(2024, 8, 1), date(2024, 8, 2))  # in block
    rows = [early_decision, early_matured, future_decision]
    prior = pt._oof_prior_rows(rows, fold_start)
    prior_ids = {r['sample_id'] for r in prior}
    assert 'A' not in prior_ids          # leaked future label excluded
    assert 'B' in prior_ids             # genuinely prior, included
    assert 'C' not in prior_ids         # belongs to the block, never trains


def test_oof_prior_requires_both_dates_before_fold():
    fold_start = date(2024, 5, 1)
    # decision before but label exactly at fold start -> NOT prior (strict <)
    at = _sample('A', date(2024, 1, 1), fold_start)
    # decision before, label before -> prior
    before = _sample('B', date(2024, 1, 1), date(2024, 3, 1))
    assert pt._oof_prior_rows([at], fold_start) == []
    assert [r['sample_id'] for r in pt._oof_prior_rows([before], fold_start)] == ['B']


def test_label_available_bar_after_decision_for_l2_path_label():
    """L2 path label must mature strictly after the decision bar."""
    n = 65
    d = pd.DataFrame({
        'datetime': pd.bdate_range('2026-07-01', periods=n),
        'open': np.full(n, 100.0), 'high': np.full(n, 105.0),
        'low': np.full(n, 95.0), 'close': np.full(n, 100.0),
        'volume': np.full(n, 1000.0)})
    d.loc[30, 'high'] = 114.   # break ref=112 at bar 30
    # H/L/H zigzag; anchor L at 25, next H at 45 confirmed at 62; ref 112 broken at 30.
    final = [
        {'bar': 15, 'type': 'H', 'price': 112., 'confirmed_at': 20},
        {'bar': 25, 'type': 'L', 'price': 94., 'confirmed_at': 36},
        {'bar': 45, 'type': 'H', 'price': 114., 'confirmed_at': 62},
    ]
    lab = parity.labels_for_stage(d, final, 25, 'L', 26, 64)
    # L1 matures at the anchor's confirmation (36); L2 at the first break (30).
    assert lab.l2 == 1                        # crossed -> HH, target is HH
    assert lab.l1_available_bar == 36
    assert lab.l2_available_bar == 30
    assert lab.l1_available_bar > 26
    assert lab.l2_available_bar > 26


def test_label_available_bar_is_next_pivot_when_no_break():
    n = 65
    d = pd.DataFrame({
        'datetime': pd.bdate_range('2026-07-01', periods=n),
        'open': np.full(n, 100.0), 'high': np.full(n, 105.0),
        'low': np.full(n, 95.0), 'close': np.full(n, 100.0),
        'volume': np.full(n, 1000.0)})
    final = [
        {'bar': 15, 'type': 'H', 'price': 112., 'confirmed_at': 20},
        {'bar': 25, 'type': 'L', 'price': 94., 'confirmed_at': 36},
        {'bar': 45, 'type': 'H', 'price': 90., 'confirmed_at': 62},  # never crossed
    ]
    lab = parity.labels_for_stage(d, final, 25, 'L', 26, 64)
    assert lab.l2 == 0                       # no break -> LH, target is HH
    assert lab.l2_available_bar == 45         # matures at next pivot bar
    assert lab.l2_available_bar > 26


def test_oof_scores_exclude_future_label_end_to_end():
    """End-to-end: a future-labeled prior row must not train the fold model.

    Fold 1's training prior is built from rows whose decision_date AND label
    availability precede the fold start. A future-labeled row (A) is excluded, so
    the only matured prior (B) is single-class and the block (D) cannot be
    scored. Without the fix A would be in the prior (two classes) and D would be
    scored -- the leakage.
    """
    pytest.importorskip('lightgbm')
    key = ('L1', 'L', 1)
    fmod = _FakeFmod()

    a = _sample('A', date(2024, 1, 1), date(2024, 7, 1), label=1)   # future label
    b = _sample('B', date(2024, 3, 1), date(2024, 3, 2), label=0)   # matured, other class
    c = _sample('C', date(2024, 5, 1), date(2024, 7, 15), label=1)  # future label
    d = _sample('D', date(2024, 8, 1), date(2024, 8, 2), label=1)   # block to score
    rows = {key: [a, b, c, d]}
    scores, prov = pt.compute_oof_base_scores(
        rows, fmod, folds=3, trees=20, threads=1, seed=0)
    # D must NOT be scored: excluding future-labeled A and C leaves only B, which
    # is single-class, so the fold-1 model cannot be trained. This proves the
    # future-labeled rows were excluded from the fold's training set.
    assert scores.get('D') is None


class _FakeFmod:
    # compute_oof_base_scores only needs ALL_COLS for column naming; it builds a
    # DataFrame from the rows' features. The fake exposes the column list.
    ALL_COLS = [f'f{i}' for i in range(1)]
