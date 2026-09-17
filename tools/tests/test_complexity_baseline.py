from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "complexity_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("complexity_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cycle_report_excludes_resolved_after_close_dsa_recovery_cycle() -> None:
    module = _load_module()
    cycles = module._cycles(module._python_graph())
    members = [set(component) for component in cycles]

    assert not any(
        {
            "app.services.after_close_orchestrator",
            "app.services.dsa_recovery_service",
        }.issubset(component)
        for component in members
    )


def test_structural_baseline_has_stable_required_fields(capsys) -> None:
    module = _load_module()
    module.main()
    output = capsys.readouterr().out

    assert '"schema_version": 1' in output
    assert '"python_cycles"' in output
    assert '"core_nested_imports"' in output
    assert '"largest_production_files"' in output
