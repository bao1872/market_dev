"""OOF training must not consume labels that mature after the fold start.

The personal model's out-of-fold base scores are trained fold-by-fold on prior
rows. A row can carry a decision_date earlier than the fold start yet still have
a label confirmed only later (L1/L2 depend on future pivot confirmation). Using
such a label to fit the fold's model leaks future information. These tests pin
the fix end to end.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import lab_parity_contract as parity
from app.strategy_integration import personal_trainer as pt
from app.strategy_integration import rolling_windows as rw
from app.strategy_integration.training import build_dataset
from app.strategy_integration import model as strategy_model

ASSETS = Path('/tmp/panji_frozen_assets')


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
    # L1 matures at the anchor's confirmation (36); L2 only once the next pivot is
    # confirmed (62) -- NOT at the bar-30 breakout, and NOT at the bar-45 pivot.
    assert lab.l2 == 1                        # crossed -> HH, target is HH
    assert lab.l1_available_bar == 36
    assert lab.l2_available_bar == 62
    assert lab.l1_available_bar > 26
    assert lab.l2_available_bar > 26
    assert lab.l2_available_bar > 30          # breakout alone is not enough


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
    assert lab.l2_available_bar == 62         # matures at next-pivot CONFIRMATION
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


def _simple_frame(n=900, seed=7, start='2023-01-02'):
    """PIT-free OHLCV frame (no adj_factor) for deterministic engine runs."""
    rng = np.random.default_rng(seed)
    v = 30 + np.cumsum(rng.normal(0, .3, n))
    o = v * .999
    c = v * 1.001
    h = np.maximum(o, c) * 1.02
    l = np.minimum(o, c) * .97
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({'datetime': dates, 'open': o, 'high': h, 'low': l,
                         'close': c, 'volume': np.full(n, 1e6)})


@pytest.mark.skipif(not (ASSETS / 'two_stage_models_local_only.pkl').is_file(),
                    reason='frozen engine assets not present in this environment')
def test_build_dataset_uses_per_candidate_maturity():
    """Counterexample A: every sample carries its OWN label maturity, never the
    last candidate's. Recompute the expected maturity for each sample from its own
    (anchor, kind, decision, split) identity and assert equality. Before the fix
    all samples inherited the last candidate's `lab.l1/l2_available_bar`, so this
    diverges for every non-last candidate and the set of maturities collapses.
    """
    fmod = strategy_model.load_engine(ASSETS)
    w = rw.rolling_windows(date(2026, 10, 15))
    frame = _simple_frame(n=900)
    data, _c, _h = build_dataset({'SH:600000': frame}, fmod, w)
    rows = [r for rs in data.values() for r in rs]
    assert rows, 'expected training samples'
    days = [pd.Timestamp(x).date() for x in frame.datetime]
    bar_of = {d: i for i, d in enumerate(days)}
    # One oracle per split keeps the replay cost bounded (cutoff is split-scoped).
    split_oracle = {}
    for name in rw.SPLIT_NAMES:
        hi = w['dates'][name][1]
        cutoff = next((i for i in range(len(days) - 1, -1, -1)
                       if days[i] <= hi), -1)
        if cutoff < 0:
            continue
        oracle_raw = frame.iloc[:cutoff + 1].reset_index(drop=True)
        final, _, _ = fmod.replay(oracle_raw)
        split_oracle[name] = (cutoff, oracle_raw, final)
    seen_maturities = set()
    for r in rows:
        parts = r['sample_id'].split('|')
        kind, layer = parts[2], parts[4]
        j = bar_of[r['anchor_date']]
        t = bar_of[r['decision_date']]
        name = rw.split_of(r['decision_date'], w)
        cutoff, oracle_raw, oracle_final = split_oracle[name]
        lab = parity.labels_for_stage(oracle_raw, oracle_final, j, kind, t, cutoff)
        exp_bar = lab.l1_available_bar if layer == 'L1' else lab.l2_available_bar
        expected = days[exp_bar] if exp_bar is not None else None
        assert r['label_available_at'] == expected, (
            'sample uses wrong maturity', r['sample_id'],
            r['label_available_at'], expected)
        seen_maturities.add(r['label_available_at'])
    # Distinct candidates must mature on distinct dates; a collapsed set would
    # mean the per-sample maturity was overwritten by the last candidate's.
    assert len(seen_maturities) > 1


@pytest.mark.parametrize('label', [1, 0])
def test_oof_excludes_l2_label_until_next_pivot_confirmed(label):
    """Counterexample B: L2 breakout at bar 30, next pivot at bar 45, confirmed at
    bar 62. The label matures only at the confirmation (62), so an OOF fold that
    starts before it (bar 31 or 50) cannot train on it even though the breakout at
    30 already happened; a fold starting after (bar 63) may. Covers both the L2
    positive (label=1) and negative (label=0) cases.
    """
    n = 65
    d = pd.DataFrame({
        'datetime': pd.bdate_range('2026-07-01', periods=n),
        'open': np.full(n, 100.0), 'high': np.full(n, 105.0),
        'low': np.full(n, 95.0), 'close': np.full(n, 100.0),
        'volume': np.full(n, 1000.0)})
    if label == 1:
        d.loc[30, 'high'] = 114.   # break ref=112 at bar 30 (positive case)
    final = [
        {'bar': 15, 'type': 'H', 'price': 112., 'confirmed_at': 20},
        {'bar': 25, 'type': 'L', 'price': 94., 'confirmed_at': 36},
        {'bar': 45, 'type': 'H', 'price': (114. if label == 1 else 90.),
         'confirmed_at': 62},
    ]
    lab = parity.labels_for_stage(d, final, 25, 'L', 26, 64)
    assert lab.l2 == label            # positive when break, negative when not
    assert lab.l2_available_bar == 62  # maturity is the confirmation, not 30/45
    days = list(pd.bdate_range('2026-07-01', periods=n))
    decision = days[26]
    matured = days[lab.l2_available_bar]
    sample = _sample('L2X', decision, matured, label=label)
    assert pt._oof_prior_rows([sample], days[31]) == []   # breakout@30 not enough
    assert pt._oof_prior_rows([sample], days[50]) == []   # still unconfirmed
    assert [r['sample_id'] for r in pt._oof_prior_rows([sample], days[63])] == ['L2X']


class _FakeFmod:
    # compute_oof_base_scores only needs ALL_COLS for column naming; it builds a
    # DataFrame from the rows' features. The fake exposes the column list.
    ALL_COLS = [f'f{i}' for i in range(1)]
