"""Dependency guards for canonical adapters and their consumers."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

from app.services.canonical_adapters import compute_smc_adapter
from app.services.canonical_smc_adapter import compute_smc_view


def _imports(path: Path, module: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
    ]


def test_read_model_consumers_do_not_import_adapter_composition() -> None:
    services = Path(__file__).parent.parent / "app" / "services"
    violations: dict[str, list[int]] = {}
    for name in ("feature_snapshot_service.py", "indicator_service.py"):
        lines = _imports(services / name, "app.services.canonical_adapters")
        if lines:
            violations[name] = lines
    assert not violations


def test_structural_factors_do_not_import_adapter_composition() -> None:
    services = Path(__file__).parent.parent / "app" / "services"
    assert not _imports(
        services / "structural_factor_service.py",
        "app.services.canonical_adapters",
    )


def test_registered_smc_adapter_delegates_to_shared_owner(monkeypatch) -> None:
    calls: list[tuple[int, dict | None]] = []

    def fake_compute(bars, display_bars=250, params=None):
        calls.append((display_bars, params))
        return {"owner": "shared", "rows": len(bars)}

    monkeypatch.setattr(
        "app.services.canonical_smc_adapter.compute_smc_view", fake_compute
    )
    bars = pd.DataFrame({"close": [1.0]})
    assert compute_smc_adapter(bars, display_bars=1, params={"x": 1}) == {
        "owner": "shared",
        "rows": 1,
    }
    assert calls == [(1, {"x": 1})]
    assert callable(compute_smc_view)
