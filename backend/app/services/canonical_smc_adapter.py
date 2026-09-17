"""Canonical SMC compute-and-view primitive without registry dependencies."""

from __future__ import annotations

from typing import Any

import pandas as pd


def compute_smc_view(
    bars: pd.DataFrame,
    display_bars: int = 250,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the canonical SMC result and adapt it to the display DTO."""
    from app.services.smc_view_adapter import adapt_smc_to_display_dto
    from app.strategy_assets.algorithms.features.smc_indicator import (
        compute_smc_indicators,
    )

    if bars is None or bars.empty:
        raise ValueError("compute_smc_adapter: bars 为空，无法计算")
    missing = [column for column in ("open", "high", "low", "close") if column not in bars]
    if missing:
        raise ValueError(
            f"compute_smc_adapter: bars 缺少列 {missing}，实际列={list(bars.columns)}"
        )

    opens = bars["open"].to_numpy(dtype=float).tolist()
    highs = bars["high"].to_numpy(dtype=float).tolist()
    lows = bars["low"].to_numpy(dtype=float).tolist()
    closes = bars["close"].to_numpy(dtype=float).tolist()
    times = [idx.isoformat() for idx in bars.index]
    full_result = compute_smc_indicators(opens, highs, lows, closes, times, params)
    return adapt_smc_to_display_dto(full_result, display_bars)
