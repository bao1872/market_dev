"""Unified rolling train/validation/test windows for strategy model training.

Contract (fixed, shared by every model in the pipeline):
  * Train     : 24 complete natural months
  * Validation:  6 complete natural months
  * Test      :  3 complete natural months
  * Cutoff    : the last complete natural month strictly before the run month.

For a run in 2026-10 this MUST yield exactly:
  TRAIN = 2024-01-01 .. 2025-12-31
  VAL   = 2026-01-01 .. 2026-06-30
  TEST  = 2026-07-01 .. 2026-09-30

No caller may derive its own boundaries: the full-market model and every
per-symbol model consume the SAME dict produced here.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

MONTHS_TRAIN = 24
MONTHS_VAL = 6
MONTHS_TEST = 3
MIN_TRADING_DAYS_PER_TEST_MONTH = 15

SPLIT_NAMES = ('train', 'val', 'test')


class WindowError(ValueError):
    pass


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    """Shift by whole months, always landing on the first day of a month."""
    total = (d.year * 12 + (d.month - 1)) + n
    return date(total // 12, total % 12 + 1, 1)


def previous_complete_month(as_of: date) -> tuple[date, date]:
    """(start, end) of the last complete natural month before `as_of`'s month."""
    first = month_start(as_of)
    start = add_months(first, -1)
    return start, month_end(start)


def rolling_windows(as_of: date) -> dict:
    """Return the single shared window definition for this run date."""
    cut_start, cut_end = previous_complete_month(as_of)
    test_start = add_months(cut_start, -(MONTHS_TEST - 1))
    val_end = test_start - timedelta(days=1)
    val_start = add_months(month_start(val_end), -(MONTHS_VAL - 1))
    train_end = val_start - timedelta(days=1)
    train_start = add_months(month_start(train_end), -(MONTHS_TRAIN - 1))
    windows = {
        'train': (train_start, train_end),
        'val': (val_start, val_end),
        'test': (test_start, cut_end),
    }
    for name in SPLIT_NAMES:
        lo, hi = windows[name]
        if lo > hi:
            raise WindowError('REVERSED_WINDOW_' + name.upper())
    if not (windows['train'][1] < windows['val'][0]
            and windows['val'][1] < windows['test'][0]):
        raise WindowError('ROLLING_WINDOW_OVERLAP')
    return {
        'as_of': as_of.isoformat(),
        'cutoff_month': (cut_start.isoformat(), cut_end.isoformat()),
        'months': {'train': MONTHS_TRAIN, 'val': MONTHS_VAL, 'test': MONTHS_TEST},
        'windows': {k: (v[0].isoformat(), v[1].isoformat()) for k, v in windows.items()},
        'dates': {k: (v[0], v[1]) for k, v in windows.items()},
    }


def split_of(day: date, windows: dict) -> str | None:
    """Which split a *decision* day belongs to (None = outside all windows)."""
    for name in SPLIT_NAMES:
        lo, hi = windows['dates'][name]
        if lo <= day <= hi:
            return name
    return None


def trading_days_by_month(dates) -> dict[str, int]:
    """Count distinct trading days per YYYY-MM key."""
    seen: dict[str, set] = {}
    for d in dates:
        key = f'{d.year:04d}-{d.month:02d}'
        seen.setdefault(key, set()).add(d)
    return {k: len(v) for k, v in sorted(seen.items())}


def assess_test_window(dates, windows: dict) -> dict:
    """Is the Test window fully covered by the available data?

    Never claims a complete three-month test from partial data.
    """
    dates = sorted(set(dates))
    test_lo, test_hi = windows['dates']['test']
    observed_max = dates[-1] if dates else None
    in_window = [d for d in dates if test_lo <= d <= test_hi]
    per_month = trading_days_by_month(in_window)
    expected_months = []
    cursor = month_start(test_lo)
    while cursor <= test_hi:
        expected_months.append(f'{cursor.year:04d}-{cursor.month:02d}')
        cursor = add_months(cursor, 1)
    thin_months = [m for m in expected_months
                   if per_month.get(m, 0) < MIN_TRADING_DAYS_PER_TEST_MONTH]
    missing_months = [m for m in expected_months if per_month.get(m, 0) == 0]
    complete = bool(observed_max and observed_max >= test_hi
                    and not thin_months and not missing_months)
    return {
        'status': 'TEST_WINDOW_COMPLETE' if complete else 'TEST_WINDOW_INCOMPLETE',
        'expected_test_start': test_lo.isoformat(),
        'expected_test_end': test_hi.isoformat(),
        'observed_max_date': observed_max.isoformat() if observed_max else None,
        'missing_date_range': (
            f'{max(observed_max, test_lo).isoformat()}..{test_hi.isoformat()}'
            if (observed_max is None or observed_max < test_hi) else None),
        'expected_months': expected_months,
        'trading_days_by_month': per_month,
        'missing_months': missing_months,
        'thin_months': thin_months,
        'trading_days_in_test': len(in_window),
    }


def stock_history_status(first_date: date, last_date: date, windows: dict) -> dict:
    """Per-symbol history sufficiency for the shared 24/6/3 contract.

    A symbol whose data starts after the train start can never be reported as a
    complete 24-month training history.
    """
    train_lo, _ = windows['dates']['train']
    test_lo, test_hi = windows['dates']['test']
    if last_date < test_lo:
        return {'status': 'INSUFFICIENT_HISTORY',
                'reason': 'NO_DATA_IN_TEST_WINDOW',
                'first_date': first_date.isoformat(), 'last_date': last_date.isoformat()}
    if first_date > train_lo:
        covered_months = (last_date.year * 12 + last_date.month) - (
            first_date.year * 12 + first_date.month) + 1
        if covered_months < MONTHS_TRAIN:
            return {'status': 'INSUFFICIENT_HISTORY',
                    'reason': 'HISTORY_SHORTER_THAN_24M_TRAIN_WINDOW',
                    'first_date': first_date.isoformat(), 'last_date': last_date.isoformat(),
                    'covered_months': covered_months,
                    'required_train_months': MONTHS_TRAIN}
        return {'status': 'PARTIAL_HISTORY',
                'reason': 'FIRST_DATE_AFTER_TRAIN_START_BUT_WINDOW_COVERED',
                'first_date': first_date.isoformat(), 'last_date': last_date.isoformat(),
                'covered_months': covered_months}
    return {'status': 'COMPLETE_24M_HISTORY',
            'reason': None,
            'first_date': first_date.isoformat(), 'last_date': last_date.isoformat()}


def json_safe_windows(windows: dict) -> dict:
    """Window dict without `date` objects, safe for JSON and content hashing."""
    return {'as_of': windows['as_of'], 'cutoff_month': windows['cutoff_month'],
            'months': windows['months'], 'windows': windows['windows']}
