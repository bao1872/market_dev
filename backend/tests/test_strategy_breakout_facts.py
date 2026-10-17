"""Observable break facts and their forward evaluation have distinct semantics."""
from types import SimpleNamespace

import pandas as pd
import pytest

from app.strategy_integration.breakout import event_identity, observe_breakout
from app.strategy_integration.producer import make_evaluation


def fixture(kind='L'):
    raw = pd.DataFrame({'datetime': pd.bdate_range('2026-01-02', periods=40),
        'open': [10.]*40, 'high': [10.5]*40, 'low': [9.5]*40, 'close': [10.]*40, 'volume': [100.]*40})
    raw.loc[5, ['high','low']] = [10., 8.]
    raw.loc[6, 'high'] = 12.
    raw.loc[7, ['high','low']] = [13., 7.]
    opposite = {'type': 'H', 'bar': 1, 'price': 11., 'confirmed_at': 3}
    if kind == 'H':
        raw.loc[:, ['open','high','low','close']] = pd.DataFrame({
            'open': 20-raw.open, 'high': 20-raw.low, 'low': 20-raw.high, 'close': 20-raw.close})
        opposite.update(type='L', price=9.)
    return raw, [[opposite] if i >= 3 else [] for i in range(40)]


@pytest.mark.parametrize('kind,direction', [('L', 'HIGH'), ('H', 'LOW')])
def test_first_break_is_threshold_independent_and_tracking_is_not_recounted(kind, direction):
    raw, snapshots = fixture(kind)
    first = observe_breakout(raw, snapshots, 5, kind, 6)
    tracked = observe_breakout(raw, snapshots, 5, kind, 7)
    assert first['direction'] == direction and first['is_first_today']
    assert tracked['first_date'] == first['first_date'] and not tracked['is_first_today']
    assert tracked['first_stage'] == 'D1'
    equal = raw.copy()
    equal.loc[6, 'high' if kind == 'L' else 'low'] = first['reference_price']
    assert observe_breakout(equal, snapshots, 5, kind, 6) is None


def test_already_broken_is_not_automatically_evaluated_as_success():
    raw, snapshots = fixture()
    fact = observe_breakout(raw, snapshots, 5, 'L', 6)
    fact['event_id'] = event_identity('SH:600000', str(raw.datetime.iloc[5].date()), 'L', 'a'*64)
    rows = [{'joint_alert': False, 'first_breakout': i == 6, 'breakout': fact,
        'decision_date': str(raw.datetime.iloc[i].date()), 'observation_id': f'o{i}', 'stage': f'D{i-5}'} for i in (6,7)]
    stock = {'candidates': [{'candidate_id': 'c1', 'kind': 'L',
        'anchor_date': str(raw.datetime.iloc[5].date()), 'observations': rows}]}
    evaluation = make_evaluation({'SH:600000': raw}, {'SH:600000': stock},
        SimpleNamespace(replay=lambda raw: ([], [], None)),
        as_of=str(raw.datetime.iloc[-1].date()), source_sha='f'*64)
    assert len(evaluation['events']) == 1
    event = evaluation['events'][0]
    assert event['outcome'] == 'FAIL' and event['endpoint_pct'] == 0
    assert event['outcome_definition'] == 'DIRECTIONAL_ENDPOINT_POSITIVE_NEXT_OPEN_20B'
