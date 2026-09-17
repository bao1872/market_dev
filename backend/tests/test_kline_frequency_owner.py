"""Contract and dependency guards for the daily-bar aggregation owner."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from app.domain.shared.kline_frequency import convert_kline_frequency
from app.repositories import bar_repository


def _daily_bars() -> pd.DataFrame:
    index = pd.to_datetime(
        ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03", "2026-02-09"]
    )
    return pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "high": [11.0, 12.0, 13.0, 14.0, 15.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "volume": [100, 200, 300, 400, 500],
            "amount": [1000, 2200, 3600, 5200, 7000],
            "adj_factor": [1.0, 1.0, 1.1, 1.1, 1.2],
        },
        index=index,
    )


def test_repository_compatibility_export_uses_canonical_owner() -> None:
    assert bar_repository.convert_kline_frequency is convert_kline_frequency
    for frequency in ("w", "m"):
        assert_frame_equal(
            bar_repository.convert_kline_frequency(_daily_bars(), frequency),
            convert_kline_frequency(_daily_bars(), frequency),
            check_exact=True,
        )


def test_market_adapters_do_not_import_bar_repository() -> None:
    app_dir = Path(__file__).parent.parent / "app"
    targets = (
        app_dir / "core" / "pytdx_adapter.py",
        app_dir / "core" / "exchange" / "db_exchange.py",
        app_dir / "services" / "kline_aggregator.py",
    )
    violations: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.repositories.bar_repository":
                violations.append(path.relative_to(app_dir).as_posix())
    assert not violations, f"market adapters reverse-import bar_repository: {violations}"


def test_exchange_implementations_depend_on_contract_not_factory_facade() -> None:
    app_dir = Path(__file__).parent.parent / "app"
    targets = (
        app_dir / "core" / "pytdx_adapter.py",
        app_dir / "core" / "exchange" / "db_exchange.py",
    )
    violations: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.core.exchange":
                violations.append(path.relative_to(app_dir).as_posix())
    assert not violations, f"exchange implementation imports factory facade: {violations}"
