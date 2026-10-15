"""Frozen lab l2_label golden parity. Set PANJI_LAB_REFERENCE_SCRIPT (SHA-pinned)."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import lab_parity_contract as parity

FROZEN_SCRIPT_SHA = '1cc123274c00209552b7ddc920c804be92e714c07fc7ef088c1c7e14a8b816bd'


def lab_reference():
    p = Path(os.environ.get('PANJI_LAB_REFERENCE_SCRIPT', ''))
    if not p.is_file():
        pytest.fail('PANJI_LAB_REFERENCE_SCRIPT is REQUIRED; no skipped reference parity')
    assert hashlib.sha256(p.read_bytes()).hexdigest() == FROZEN_SCRIPT_SHA
    spec = importlib.util.spec_from_file_location('panji_lab_golden_immutable', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def example(kind, *, hit=30, equality=False, ambiguous=False, confirmed=52):
    n = 65
    d = pd.DataFrame({
        'datetime': pd.bdate_range('2026-07-01', periods=n),
        'open': np.full(n, 100.0),
        'high': np.full(n, 105.0),
        'low': np.full(n, 95.0),
        'close': np.full(n, 100.0),
        'volume': np.full(n, 1000.0),
    })
    if kind == 'L':
        prev, anchor, nxt, ref = 'H', 'L', 'H', 112.
        if hit is not None:
            d.loc[hit, 'high'] = 114.
        elif equality:
            d.loc[35, 'high'] = ref
        if ambiguous:
            d.loc[25, 'high'] = 114.
        pp, ap, np_ = 112., 94., 114.
    else:
        prev, anchor, nxt, ref = 'L', 'H', 'L', 89.
        if hit is not None:
            d.loc[hit, 'low'] = 87.
        elif equality:
            d.loc[35, 'low'] = ref
        if ambiguous:
            d.loc[25, 'low'] = 87.
        pp, ap, np_ = 89., 117., 87.
    final = [
        {'bar': 15, 'type': prev, 'price': pp, 'confirmed_at': 20},
        {'bar': 25, 'type': anchor, 'price': ap, 'confirmed_at': 36},
        {'bar': 45, 'type': nxt, 'price': np_, 'confirmed_at': confirmed},
    ]
    return d, final


@pytest.mark.parametrize('kind', ['H', 'L'])
@pytest.mark.parametrize('hit,equality,ambiguous', [
    (30, False, False), (None, False, False),
    (26, False, False), (None, True, False),
    (30, False, True),
])
def test_all_lab_l2_labels_and_exclusions(kind, hit, equality, ambiguous):
    ref = lab_reference()
    raw, final = example(kind, hit=hit, equality=equality, ambiguous=ambiguous)
    ans, reason = ref.l2_label(raw, final, 1, kind, 25, 26)
    actual = parity.labels_for_stage(raw, final, 25, kind, 26, 64)
    assert actual.l2_reason == reason, (kind, hit, equality, ambiguous, actual, reason)
    assert actual.l2 == (int(ans['y']) if reason == 'OK' else None)
    assert actual.target_tag == (ans.get('target_tag') if reason == 'OK' else None)
    assert actual.first_break_bar == (ans.get('first_break_bar') if reason == 'OK'
                                      else actual.first_break_bar)


def test_split_maturity_prevents_future_l2_and_l1():
    raw, final = example('L', hit=30, confirmed=62)
    answer = parity.labels_for_stage(raw.iloc[:61], final[:2], 25, 'L', 26, 60)
    assert answer.l2 is None
    # Pivot may have been confirmed, but the future next pivot is not mature.
    assert answer.l1 == 1


def test_false_candidate_only_negative_after_confirmed_opposite_pivot():
    raw, final = example('L', hit=30)
    # Candidate at bar 22 is not a true anchor; H pivot 45 is only confirmable at 52.
    earlier = parity.labels_for_stage(raw.iloc[:46], final[:2], 22, 'L', 23, 45)
    assert earlier.l1 is None
    later = parity.labels_for_stage(raw, final, 22, 'L', 23, 64)
    assert later.l1 == 0


def test_pit_candidates_match_frozen_causal_detector_on_flat_factors():
    reference = lab_reference()
    root = Path(os.environ['PANJI_LAB_REFERENCE_ENGINE'])
    assert hashlib.sha256(root.read_bytes()).hexdigest() == (
        '36c862bc318b5e53617b7527d5db166de3243addc85d2d1ae6a655574a93fb1a')
    spec = importlib.util.spec_from_file_location('panji_lab_frozen_features', root)
    fmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmod)
    r = np.random.default_rng(3)
    n = 85
    prices = 100 + np.cumsum(r.normal(0, 1, n))
    raw = pd.DataFrame({
        'datetime': pd.bdate_range('2026-01-01', periods=n),
        'open': prices, 'close': prices,
        'high': prices+2, 'low': prices-2, 'volume': np.full(n, 1000.0),
    })
    pit = raw.copy()
    pit['adj_factor'] = np.ones(n)
    fn = lambda source, _asof: source.drop(columns=['adj_factor']).reset_index(drop=True)
    actual = parity.causal_candidates_pit(raw, pit, fmod, fn)
    expected, _ = fmod.causal_candidates(raw)
    assert [(j,k) for j,k,_ in actual] == [(j,k) for j,k,_ in expected]
