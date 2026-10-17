"""Pure shared evaluation wire contract; no model loading or market-data access."""
from datetime import date

PATH_HORIZON = 20
MATURITY_CONTRACT = 'INDEPENDENT_STRUCTURE_AND_PATH_V1'
EVALUATION_PROTOCOL = f'INDEPENDENT_MATURITY_NEXT_OPEN_{PATH_HORIZON}B_V2'
PERIODS = ('TRAIN', 'VAL', 'TEST', 'POST_TEST', 'UNASSIGNED')


def signal_period(day, windows):
    date.fromisoformat(day)
    if windows is None:
        return 'UNASSIGNED'
    if not isinstance(windows, dict) or set(windows) != {'train','val','test'}:
        raise ValueError('INVALID_EVALUATION_SPLIT_WINDOWS')
    previous = None
    for name in ('train','val','test'):
        bounds = windows[name]
        if not isinstance(bounds, (list,tuple)) or len(bounds) != 2:
            raise ValueError('INVALID_EVALUATION_SPLIT_BOUNDS')
        start, end = (date.fromisoformat(v) for v in bounds)
        if start > end or (previous is not None and previous >= start):
            raise ValueError('INVALID_EVALUATION_SPLIT_ORDER')
        previous = end
    for name, bounds in windows.items():
        if bounds[0] <= day <= bounds[1]:
            return name.upper()
    return 'POST_TEST' if day > windows['test'][1] else 'UNASSIGNED'
