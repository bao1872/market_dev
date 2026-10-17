"""Batch preparation for the frozen scorer; candidate/feature semantics stay there.

One context per contiguous PIT factor regime. Confirmed snapshots and trailing
features are prefix invariant; future candidates are explicitly masked out.
"""
from __future__ import annotations

from copy import copy
from time import perf_counter

import numpy as np


class _Predictions:
    def __init__(self, model, matrix):
        values = model.predict(matrix, num_threads=1)
        self.rows = {row.tobytes(): value for row, value in zip(matrix, values, strict=True)}

    def predict(self, matrix, **kwargs):
        return np.asarray([self.rows[row.tobytes()] for row in matrix])


class _Engine:
    def __init__(self, owner, frame, indices, profile):
        self.owner = owner
        started = perf_counter()
        self.final, self.snapshots, self.audit = owner.replay(frame)
        profile['replay_seconds'] += perf_counter()-started
        started = perf_counter()
        self.rows, self.matrix = owner.features(frame, self.snapshots, indices)
        profile['features_seconds'] += perf_counter()-started
        self.row_by_bar = {int(t): i for i, t in enumerate(indices)}
        candidates, _ = owner.causal_candidates(frame)
        self.candidates = {}
        for candidate in candidates:
            j = int(candidate[0])
            for stage in (1, 2, 3):
                self.candidates.setdefault(j + stage, []).append(candidate)

    def replay(self, frame):
        n = len(frame)
        return self.snapshots[n-1], self.snapshots[:n], self.audit

    def features(self, frame, snapshots, indices):
        positions = [self.row_by_bar[int(t)] for t in indices]
        return self.rows.iloc[positions].reset_index(drop=True), self.matrix[positions]

    def causal_candidates(self, frame):
        return self.candidates.get(len(frame)-1, []), 0


def decision_contexts(runtime, raw, start, pit_source=None):
    """Yield exact decision-local frames and a scorer sharing batched work.

    No frozen package files/weights are modified. The original score method
    still owns task eligibility and output encoding.
    """
    from .model import _pit_frame

    first = max(6, start)
    profile = {'regimes': 0, 'decisions': 0, 'prepare_seconds': 0., 'replay_seconds': 0.,
               'features_seconds': 0., 'predict_seconds': 0., 'decision_seconds': 0.}
    if pit_source is None:
        boundaries = [0, len(raw)]
    else:
        factors = pit_source.adj_factor.to_numpy(float)
        if len(factors) != len(raw) or not np.isfinite(factors).all() or (factors <= 0).any():
            raise ValueError('INVALID_HISTORY_PIT_FACTORS')
        boundaries = [0, *(np.flatnonzero(factors[1:] != factors[:-1])+1).tolist(), len(raw)]
    for left, right in zip(boundaries[:-1], boundaries[1:], strict=True):
        begin = max(first, left)
        if begin >= right:
            continue
        started = perf_counter()
        frame = raw.iloc[:right] if pit_source is None else _pit_frame(
            pit_source.iloc[:right], str(raw.datetime.iloc[right-1].date()))
        profile['prepare_seconds'] += perf_counter()-started
        profile['regimes'] += 1
        # A separate runtime copy confines shared prepared state to this stock.
        prepared = copy(runtime)
        engine = _Engine(runtime.engine, frame, np.arange(max(20, begin), right), profile)
        prepared.engine = engine
        started = perf_counter()
        prepared.models = ({key: _Predictions(model, engine.matrix)
                            for key, model in runtime.models.items()} if len(engine.matrix) else runtime.models)
        profile['predict_seconds'] += perf_counter()-started
        prepared.history_profile = profile
        for t in range(begin, right):
            profile['decisions'] += 1
            yield t, frame.iloc[:t+1], prepared
