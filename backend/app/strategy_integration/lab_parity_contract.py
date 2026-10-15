"""Frozen panji_lab label/candidate contract, adapted for explicit split cutoffs.

Research reference: panji_lab experiment/two-stage-final-unified-d123
9c32a5e33c5d25a901d2e56757406789e9eaef37,
scripts/panji_two_stage_final.py:l1_label/l2_label/one_stock.
No weights, I/O, runtime publication, or oracle features are used in scoring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

WARMUP = 20
STAGES = (1, 2, 3)
TARGET = {'L': 'HH', 'H': 'LL'}  # H metadata typo in frozen research bundle is deliberately corrected.


@dataclass(frozen=True)
class DecisionLabels:
    l1: int | None
    l2: int | None
    l1_reason: str
    l2_reason: str
    target_tag: str | None = None
    first_break_bar: int | None = None
    # Bar index at which the respective label is *definitively known* (the
    # future pivot/confirmation it depends on has occurred). This is almost
    # always later than `decision_bar`; excluding it from OOF training would
    # leak future information into the personal model. None when the label is
    # unresolved within the split (never available).
    l1_available_bar: int | None = None
    l2_available_bar: int | None = None


def causal_candidates_pit(frame, pit_source, fmod, pit_frame_fn):
    """Left-5/right-1 candidates observed as-of *j+1*, not the run date.

    Within each contiguous factor regime the QFQ as-of transformation is
    constant through the decision dates; one reference replay per regime is
    enough to preserve all historical candidate identities. Never match by
    numerical factor alone (factor values can recur non-contiguously).
    """
    if pit_source is None:
        return fmod.causal_candidates(frame)[0]
    if len(frame) != len(pit_source) or 'adj_factor' not in pit_source:
        raise ValueError('PIT_CANDIDATE_SOURCE_MISSING_OR_MISALIGNED')
    date1 = pd.to_datetime(frame['datetime']).dt.normalize().to_numpy()
    date2 = pd.to_datetime(pit_source['datetime']).dt.normalize().to_numpy()
    if not np.array_equal(date1, date2):
        raise ValueError('PIT_CANDIDATE_DATE_MISALIGNMENT')
    factors = pd.to_numeric(pit_source['adj_factor'], errors='coerce').to_numpy(float)
    if not np.isfinite(factors).all() or (factors <= 0).any():
        raise ValueError('PIT_CANDIDATE_INVALID_FACTORS')
    regimes = np.r_[0, np.cumsum(factors[1:] != factors[:-1])]
    found = []
    for reg in np.unique(regimes[1:]):
        # Candidate j becomes observable only at j+1 close.
        decision_bars = np.flatnonzero(regimes == reg)
        valid = decision_bars[decision_bars >= 1]
        if not len(valid):
            continue
        last = int(valid[-1])
        asof_frame = pit_frame_fn(pit_source.iloc[:last+1].reset_index(drop=True),
                                  str(pd.Timestamp(frame.datetime.iloc[last]).date()))
        if len(asof_frame) != last + 1:
            raise ValueError('PIT_CANDIDATE_FRAME_LENGTH_CHANGED')
        candidates, _ = fmod.causal_candidates(asof_frame)
        found.extend((int(j), k, float(px)) for j, k, px in candidates
                     if j + 1 <= last and regimes[j + 1] == reg)
    found.sort(key=lambda row: (row[0], row[1]))
    if len(found) != len({(j, k) for j, k, _ in found}):
        raise ValueError('PIT_CANDIDATE_DUPLICATE')
    return found


def split_oracle_frame(frame, pit_source, fmod, pit_frame_fn, cutoff_bar):
    """Future labels use only oracle truth mature as-of this split end."""
    if cutoff_bar < 0 or cutoff_bar >= len(frame):
        raise ValueError('INVALID_SPLIT_ORACLE_CUTOFF')
    if pit_source is not None:
        oracle = pit_frame_fn(pit_source.iloc[:cutoff_bar+1].reset_index(drop=True),
                              str(pd.Timestamp(frame.datetime.iloc[cutoff_bar]).date()))
    else:
        oracle = frame.iloc[:cutoff_bar+1].reset_index(drop=True).copy()
    final, _, _ = fmod.replay(oracle)
    return oracle, final


def labels_for_stage(raw, final, j: int, kind: str, decision_bar: int,
                     cutoff_bar: int) -> DecisionLabels:
    """Reference-equivalent L1/L2 eligibility with strict PIT split maturity.

    L1: confirmed true pivot = 1; false only if an opposite final pivot was
    confirmed by this split cutoff; otherwise UNRESOLVED.
    L2: reference's *path-first-hit*, not the final pivot's price comparison.
    All anchor/order/equality/early-hit exclusions mirror lab l2_label().
    """
    if kind not in ('H', 'L') or not (0 <= j < decision_bar <= cutoff_bar < len(raw)):
        raise ValueError('INVALID_DECISION_OR_CUTOFF')
    ix = next((i for i, p in enumerate(final)
               if int(p['bar']) == j and p['type'] == kind), None)
    l1_mat = None
    if ix is not None and int(final[ix]['confirmed_at']) <= cutoff_bar:
        l1, r1 = 1, 'CONFIRMED_TRUE_ANCHOR'
        l1_mat = int(final[ix]['confirmed_at'])
    elif ix is None and any(int(p['bar']) > j and p['type'] != kind and
                              int(p['confirmed_at']) <= cutoff_bar for p in final):
        l1, r1 = 0, 'CONFIRMED_FALSE_EXTREME'
        l1_mat = min(int(p['confirmed_at']) for p in final
                     if int(p['bar']) > j and p['type'] != kind
                     and int(p['confirmed_at']) <= cutoff_bar)
    else:
        l1, r1 = None, 'UNRESOLVED_L1_WITHIN_SPLIT'
    if ix is None:
        return DecisionLabels(l1, None, r1, 'NOT_TRUE_ANCHOR', l1_available_bar=l1_mat)
    if ix == 0 or ix + 1 >= len(final):
        return DecisionLabels(l1, None, r1, 'NO_PREVIOUS_OR_NEXT_PIVOT', l1_available_bar=l1_mat)
    prior, nxt = final[ix-1], final[ix+1]
    if prior['type'] != nxt['type']:
        return DecisionLabels(l1, None, r1, 'BAD_ZIGZAG_ALTERNATION', l1_available_bar=l1_mat)
    if j < WARMUP:
        return DecisionLabels(l1, None, r1, 'WARMUP_INSUFFICIENT', l1_available_bar=l1_mat)
    ref = float(prior['price'])
    # Bar-local ordering is unknown from daily OHLC. Cannot treat an already
    # crossed reference on the anchor bar as a later break.
    if kind == 'L' and float(raw.high.iloc[j]) > ref:
        return DecisionLabels(l1, None, r1, 'ANCHOR_BAR_INTRADAY_ORDER_UNKNOWN', l1_available_bar=l1_mat)
    if kind == 'H' and float(raw.low.iloc[j]) < ref:
        return DecisionLabels(l1, None, r1, 'ANCHOR_BAR_INTRADAY_ORDER_UNKNOWN', l1_available_bar=l1_mat)
    q = int(nxt['bar'])
    if q > cutoff_bar or int(nxt['confirmed_at']) > cutoff_bar:
        return DecisionLabels(l1, None, r1, 'NEXT_PIVOT_NOT_MATURE_WITHIN_SPLIT', l1_available_bar=l1_mat)
    if q <= j or q <= decision_bar:
        return DecisionLabels(l1, None, r1, 'TARGET_ALREADY_OCCURRED', l1_available_bar=l1_mat)
    series = (raw.high if kind == 'L' else raw.low).iloc[j+1:q+1]
    broken = series > ref if kind == 'L' else series < ref
    first = int(broken[broken].index[0]) if bool(broken.any()) else None
    if first is not None and first <= decision_bar:
        reason = ('BREAKOUT_ON_DECISION_DAY' if first == decision_bar
                  else 'TARGET_ALREADY_OCCURRED')
        return DecisionLabels(l1, None, r1, reason, first_break_bar=first, l1_available_bar=l1_mat)
    # The frozen l2_label specifically regards an equality touch (without
    # strict breakout) as ambiguous and excludes the row, not label=0.
    if first is None and bool((series == ref).any()):
        return DecisionLabels(l1, None, r1, 'EQUAL_HIGH_LOW', l1_available_bar=l1_mat)
    tag = ('HH' if first is not None else 'LH') if kind == 'L' else (
        'LL' if first is not None else 'HL')
    # The L2 label is *definitively* known only once every precondition of the
    # full lab contract is satisfied: the current anchor pivot is confirmed, the
    # next ZigZag pivot is confirmed (it gates the 'no break -> opposite tag'
    # branch), and the reference has been crossed (or the next pivot reached).
    # Using only the breakout bar (first) or the next pivot bar (q) would mature
    # the label before its truth is settled and leak future information into OOF.
    l2_mat = max(
        int(final[ix]['confirmed_at']),
        int(nxt['confirmed_at']),
        int(first) if first is not None else q,
    )
    return DecisionLabels(l1, int(tag == TARGET[kind]), r1, 'OK',
                          target_tag=tag, first_break_bar=first,
                          l1_available_bar=l1_mat, l2_available_bar=l2_mat)
