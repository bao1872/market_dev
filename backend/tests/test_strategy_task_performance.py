"""Full-cohort metric regression; no database or market data."""
import copy

import pytest

from app.services.strategy_task_performance import evaluate_tasks, validate_samples


def snapshot():
    # Metrics-only contract input. Deliberately includes below-threshold positives
    # and tied scores; an alert-only implementation gives the wrong recall/AUC.
    rows = []
    for i, (score, label) in enumerate(((.9, 1), (.5, 1), (.5, 0), (.1, 0), (.2, None))):
        rows.append({'sample_id': str(i), 'symbol': 'SH:600000', 'task': 'L1_L_D1',
                     'layer': 'L1', 'kind': 'L', 'stage': 'D1', 'decision_date': '2026-08-03',
                     'score': score, 'threshold': .6, 'label': label, 'eligible': True,
                     'label_available_at': '2026-08-04' if label is not None else None,
                     'period': 'TEST', 'evaluation_domain': 'ALL_ELIGIBLE_CANDIDATES'})
    return {'metadata': {'release_id': 'a'*64}, 'task_samples': validate_samples(rows)}


def test_full_cohort_recall_and_tied_auc():
    result = evaluate_tasks(snapshot(), symbol='SH:600000')['tasks'][0]
    assert (result['tp'], result['fp'], result['fn'], result['tn']) == (1, 0, 1, 2)
    assert result['precision'] == 1
    assert result['recall'] == .5
    assert result['auc'] == .875
    assert result['n_unresolved'] == 1


def test_scope_period_and_identity_fail_closed():
    snap = snapshot()
    assert evaluate_tasks(snap, symbol='SZ:000001')['status'] == 'NO_SAMPLES'
    assert evaluate_tasks(snap, period='VAL')['tasks'] == []
    with pytest.raises(ValueError, match='RELEASE_MISMATCH'):
        evaluate_tasks(snap, release_id='b'*64)
    del snap['task_samples']
    assert evaluate_tasks(snap)['status'] == 'UNAVAILABLE'


def test_one_class_and_excluded_samples_are_not_false_green():
    snap = snapshot()
    snap['task_samples'][1]['eligible'] = False
    snap['task_samples'][2]['eligible'] = False
    snap['task_samples'][3]['eligible'] = False
    row = evaluate_tasks(snap)['tasks'][0]
    assert row['auc'] is None
    assert row['auc_unavailable_reason'] == 'REQUIRES_BOTH_CLASSES'
    assert row['n_excluded'] == 3


def test_invalid_or_misclassified_samples_rejected():
    for key, value in [('score', float('nan')), ('task', 'L1_H_D1'), ('period', 'FAKE'),
                       ('evaluation_domain', 'ALERTS_ONLY'), ('label', True)]:
        rows = copy.deepcopy(snapshot()['task_samples'])
        rows[0][key] = value
        with pytest.raises(ValueError):
            validate_samples(rows)


def test_trend_uses_only_matching_endpoint_and_keeps_original_labels():
    snap = snapshot()
    for i, row in enumerate(snap['task_samples']):
        row['trend_context'] = {'direction': '向上' if i < 3 else '向下',
            'endpoint_match': i != 1, 'as_of': row['decision_date'], 'algorithm_sha': 'a'*64}
    validate_samples(snap['task_samples'])
    result = evaluate_tasks(snap)
    trend = result['trend']
    assert trend['coverage'] == {'missing_context': 0, 'endpoint_mismatch': 1, 'matched': 4}
    up = next(t for t in trend['tasks'] if t['direction'] == '向上')
    assert (up['tp'], up['fp'], up['fn'], up['tn']) == (1, 0, 0, 1)
    assert up['auc'] == 1
    down = next(t for t in trend['tasks'] if t['direction'] == '向下')
    assert down['n_unresolved'] == 1 and down['auc'] is None
    assert result['tasks'][0]['recall'] == .5  # Existing task cohort unchanged.


def test_legacy_missing_context_is_unavailable_and_wrong_day_rejected():
    snap = snapshot()
    trend = evaluate_tasks(snap)['trend']
    assert trend['status'] == 'UNAVAILABLE' and trend['coverage']['missing_context'] == 5
    snap['task_samples'][0]['trend_context'] = {'direction': '向上', 'endpoint_match': True,
        'as_of': '2026-08-04', 'algorithm_sha': 'a'*64}
    with pytest.raises(ValueError, match='trend context'):
        validate_samples(snap['task_samples'])
