from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from app.domain.shared.bar_identity import (
    compute_source_bar_hash as canonical_hash,
)
from app.domain.shared.bar_identity import (
    compute_source_bar_times as canonical_times,
)
from app.services.chart_bars_service import (
    compute_source_bar_hash as compatibility_hash,
)
from app.services.chart_bars_service import (
    compute_source_bar_times as compatibility_times,
)


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0, 10.2],
            "high": [10.5, 10.8],
            "low": [9.8, 10.1],
            "close": [10.2, 10.7],
            "volume": [100000.0, 120000.0],
            "amount": [1020000.0, 1284000.0],
        },
        index=pd.to_datetime(["2026-09-16 10:00:00", "2026-09-16 10:15:00"]),
    )


def test_chart_compatibility_exports_are_canonical_helpers() -> None:
    assert compatibility_hash is canonical_hash
    assert compatibility_times is canonical_times


def test_bar_identity_contract_preserves_daily_and_intraday_serialization() -> None:
    bars = _bars()
    assert canonical_times(bars) == ["2026-09-16", "2026-09-16"]
    assert canonical_times(bars, "15m") == [
        "2026-09-16T10:00:00",
        "2026-09-16T10:15:00",
    ]
    assert canonical_hash(bars) == "a2b9b64b0a8b57c8"
    assert canonical_hash(bars, "15m") == "43f3a235e7e405d1"


def test_vectorized_hash_is_exactly_equal_to_frozen_row_serialization() -> None:
    bars = _bars()
    bars.loc[bars.index[0], "volume"] = np.nan
    bars.loc[bars.index[1], "amount"] = np.inf
    fmt = "%Y-%m-%dT%H:%M:%S"
    frozen_parts: list[str] = []
    for idx, row in bars.iterrows():
        frozen_parts.append(
            f"{idx.strftime(fmt)}|{row['open']}|{row['high']}|{row['low']}|"
            f"{row['close']}|{row['volume']}|{row['amount']}"
        )
    expected = hashlib.sha256("\n".join(frozen_parts).encode("utf-8")).hexdigest()[:16]

    assert canonical_hash(bars, "15m") == expected
