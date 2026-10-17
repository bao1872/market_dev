"""Task performance from full eligible cohorts, never alert-only events."""
from __future__ import annotations

import math

LAYERS = ('D0', 'L1', 'L2')


def validate_samples(samples):
    if not isinstance(samples, list) or len(samples) > 2_000_000:
        raise ValueError('invalid task sample array')
    seen = set()
    for row in samples:
        if not isinstance(row, dict):
            raise ValueError('invalid task sample')
        key = row.get('sample_id')
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('duplicate/invalid sample identity')
        seen.add(key)
        if row.get('layer') not in LAYERS or row.get('kind') not in ('H', 'L'):
            raise ValueError('invalid task identity')
        expected_stages = ('D0',) if row['layer'] == 'D0' else ('D1', 'D2', 'D3')
        if row.get('stage') not in expected_stages or row.get('task') != f'{row["layer"]}_{row["kind"]}_{row["stage"]}':
            raise ValueError('invalid task/stage')
        for field in ('score', 'threshold'):
            v = row.get(field)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
                raise ValueError('invalid task score')
        if row.get('label') not in (None, 0, 1) or isinstance(row.get('label'), bool):
            raise ValueError('invalid task label')
        if not isinstance(row.get('eligible'), bool):
            raise ValueError('task eligibility required')
        domain = 'TRUE_ANCHOR_TARGET_ELIGIBLE' if row['layer'] == 'L2' else 'ALL_ELIGIBLE_CANDIDATES'
        if row.get('evaluation_domain') != domain:
            raise ValueError('task domain mismatch')
        from app.services.strategy_candidate_snapshot import SYMBOL_RE, DatasetInvalid, _day
        if not isinstance(row.get('symbol'), str) or not SYMBOL_RE.fullmatch(row['symbol']):
            raise ValueError('invalid task symbol')
        try:
            _day(row.get('decision_date'), 'task.decision_date')
        except DatasetInvalid as exc:
            raise ValueError(str(exc)) from exc
        if row.get('label') is not None:
            try:
                available = _day(row.get('label_available_at'), 'task.label_available_at')
            except DatasetInvalid as exc:
                raise ValueError(str(exc)) from exc
            if available <= row['decision_date']:
                raise ValueError('label already known at decision')
        context = row.get('trend_context')
        if context is not None:
            if (not isinstance(context, dict) or context.get('direction') not in ('向上', '向下')
                    or not isinstance(context.get('endpoint_match'), bool)
                    or context.get('as_of') != row['decision_date']
                    or not isinstance(context.get('algorithm_sha'), str)
                    or len(context['algorithm_sha']) != 64
                    or any(c not in '0123456789abcdef' for c in context['algorithm_sha'])):
                raise ValueError('invalid trend context')
        if row.get('period') not in ('TRAIN', 'VAL', 'TEST', 'UNASSIGNED'):
            raise ValueError('invalid task split')
    return samples


def _auc(rows):
    ordered = sorted(rows, key=lambda r: r[0])
    n_pos = sum(r[1] == 1 for r in ordered)
    n_neg = len(rows) - n_pos
    if not n_pos or not n_neg:
        return None
    # Mann-Whitney statistic with mean ranks for tied scores.
    rank_sum, i = 0.0, 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        rank_sum += sum(r[1] == 1 for r in ordered[i:j]) * (i + 1 + j) / 2
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def evaluate_tasks(snapshot, *, symbol=None, period='TEST', release_id=None):
    meta = snapshot['metadata']
    if release_id and meta.get('release_id') != release_id:
        raise ValueError('PERFORMANCE_RELEASE_MISMATCH')
    samples = snapshot.get('task_samples')
    if samples is None:
        return {'metadata': meta, 'status': 'UNAVAILABLE', 'reason': '未发布完整适用候选评价样本；不能从告警子集计算召回率或 AUC',
                'symbol': symbol, 'period': period, 'tasks': [], 'trend': {'status': 'UNAVAILABLE', 'tasks': [], 'coverage': {}}}
    groups, trend_groups = {}, {}
    coverage = {'missing_context': 0, 'endpoint_mismatch': 0, 'matched': 0}
    if hasattr(samples, 'select_symbol'):
        samples = samples.select_symbol(symbol)
    for row in samples:
        if (symbol is not None and row['symbol'] != symbol) or (period != 'ALL' and row['period'] != period):
            continue
        targets = [(groups, row['task'])]
        if row['layer'] in ('D0', 'L1'):
            context = row.get('trend_context')
            if not context:
                coverage['missing_context'] += 1
            elif not context['endpoint_match']:
                coverage['endpoint_mismatch'] += 1
            else:
                coverage['matched'] += 1
                targets.append((trend_groups, context['direction'] + ':' + row['task']))
        for bucket, key in targets:
            group = bucket.setdefault(key, {'meta': row, 'eligible': 0, 'excluded': 0,
                'matured': [], 'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0})
            if not row['eligible']:
                group['excluded'] += 1
                continue
            group['eligible'] += 1
            if row['label'] is not None:
                group['matured'].append((row['score'], row['label']))
                key = ('tp' if row['label'] else 'fp') if row['score'] >= row['threshold'] else ('fn' if row['label'] else 'tn')
                group[key] += 1
    tasks = _summarize(groups)
    trend_tasks = _summarize(trend_groups)
    return {'metadata': meta, 'status': 'AVAILABLE' if tasks else 'NO_SAMPLES', 'symbol': symbol,
            'period': period, 'tasks': tasks, 'trend': {
                'status': 'AVAILABLE' if trend_tasks else 'UNAVAILABLE',
                'protocol': 'TV_LEG_ENDPOINT_CONFIRMATION_V1', 'tasks': trend_tasks, 'coverage': coverage}}


def _summarize(groups):
    tasks = []
    for task, group in sorted(groups.items()):
        row, matured = group['meta'], group['matured']
        tp, fp, fn, tn = (group[k] for k in ('tp','fp','fn','tn'))
        auc = _auc(matured)
        tasks.append({'task': row['task'], 'direction': row.get('trend_context', {}).get('direction') if row.get('trend_context') else None, 'layer': row['layer'], 'kind': row['kind'], 'stage': row['stage'],
                      'evaluation_domain': row['evaluation_domain'], 'n_eligible': group['eligible'],
                      'n_matured': len(matured), 'n_unresolved': group['eligible']-len(matured),
                      'n_excluded': group['excluded'], 'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
                      'precision': tp/(tp+fp) if tp+fp else None,
                      'recall': tp/(tp+fn) if tp+fn else None, 'auc': auc,
                      'auc_unavailable_reason': None if auc is not None else 'REQUIRES_BOTH_CLASSES',
                      'sample_quality': 'SMALL_SAMPLE' if len(matured) < 30 else 'AVAILABLE'})
    return tasks
