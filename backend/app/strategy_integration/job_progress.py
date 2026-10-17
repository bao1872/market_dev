"""Strategy adapter over the existing long-task budget. No writes unless job-owned."""
from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.utils.long_task_budget import LongTaskBudgetState

from .release_store import atomic_json


def progress(stage, completed, total, **details):
    path = os.getenv('PANJI_STRATEGY_PROGRESS_FILE')
    if not path:
        return
    existing = Path(path).parent
    while not existing.exists():
        existing = existing.parent
    reserve = max(512, int(os.getenv('PANJI_STRATEGY_DISK_RESERVE_MB', '4096')))*1024*1024
    if shutil.disk_usage(existing).free < reserve:
        raise RuntimeError('STRATEGY_DISK_HEADROOM_EXHAUSTED')
    state = LongTaskBudgetState(chunk_size=1, total=total,
        memory_budget_mb=int(os.getenv('PANJI_STRATEGY_MEMORY_BUDGET_MB', '2048')))
    state.record_chunk_done(completed)
    if state.should_stop():
        raise RuntimeError('STRATEGY_MEMORY_BUDGET_EXCEEDED')
    atomic_json(Path(path), {'stage': stage, 'completed': completed, 'total': total,
        'updated_at': datetime.now(UTC).isoformat(), 'budget': state.to_status(), **details})
