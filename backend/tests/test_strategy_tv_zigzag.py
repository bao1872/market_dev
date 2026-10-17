"""Consume the actual compiled TV owner, including its repaintable last leg."""
import pandas as pd

from app.services.strategy_tv_zigzag import current_segment, overlay


def frame(high, low=None):
    low = low if low is not None else [v-.5 for v in high]
    return pd.DataFrame({'datetime': pd.bdate_range('2026-01-02', periods=len(high)),
        'high': high, 'low': low, 'volume': [10.]*len(high)})


def test_dashed_projection_is_the_last_leg_even_without_two_solid_pivots():
    raw = frame([10,11,12,13,14,20,19,18,17,16,15,14,15,16,17,18])
    graph, segment = overlay(raw), current_segment(raw)
    assert not graph['segments'] and graph['projected'] is not None
    assert segment['direction'] == '向下' and segment['lineStyle'] == 'dashed'
    assert (segment['fromIndex'], segment['toIndex']) == (5,11)
    assert segment['lastDate'] != segment['asOf']
    assert segment['slopePctPerSession'] == segment['totalReturnPct']/6


def test_no_projection_uses_last_solid_leg_and_projection_takes_priority():
    prefix = [10,11,12,13,14,20,16,15,14,13,12,10]
    solid = frame(prefix+[12]*5)
    assert overlay(solid)['projected'] is None
    result = current_segment(solid)
    assert result['direction'] == '向下' and result['lineStyle'] == 'solid'
    projected = frame(prefix+[11,12,13,14,15])
    assert current_segment(projected)['direction'] == '向上'
    assert current_segment(projected)['lineStyle'] == 'dashed'


def test_same_bar_segment_has_direction_but_no_division_by_zero():
    high, low = [15.]*21, [14.]*21
    high[10], low[10] = 20., 10.
    segment = current_segment(frame(high, low))
    assert segment['direction'] == '向下' and segment['sessions'] == 0
    assert segment['slopePctPerSession'] is None


def test_flat_and_too_short_have_no_fabricated_direction():
    assert current_segment(frame([100.]*30, [100.]*30)) is None
    assert current_segment(frame([10.,11.])) is None
