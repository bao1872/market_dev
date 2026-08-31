"""Small dependency-free pytest plugin that records execution truth by nodeid."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_collected: list[str] = []
_deselected: list[str] = []
_phases: dict[str, dict[str, str]] = {}


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--panji-evidence-report",
        action="store",
        default=None,
        help="write machine-readable Panji evidence report",
    )


def pytest_sessionstart(session: Any) -> None:
    del session
    _collected.clear()
    _deselected.clear()
    _phases.clear()


def pytest_collection_finish(session: Any) -> None:
    _collected[:] = [item.nodeid for item in session.items]


def pytest_deselected(items: list[Any]) -> None:
    _deselected.extend(item.nodeid for item in items)


def pytest_runtest_logreport(report: Any) -> None:
    phases = _phases.setdefault(report.nodeid, {})
    if report.skipped:
        phases[report.when] = "skipped"
    elif report.failed:
        phases[report.when] = "failed"
    else:
        phases[report.when] = "passed"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    output = session.config.getoption("--panji-evidence-report")
    if not output:
        return
    tests: dict[str, dict[str, Any]] = {}
    for nodeid in _collected:
        phases = _phases.get(nodeid, {})
        values = set(phases.values())
        if "failed" in values:
            status = "failed"
        elif "skipped" in values:
            status = "skipped"
        elif phases.get("call") == "passed":
            status = "passed"
        else:
            status = "not_run"
        tests[nodeid] = {"status": status, "phases": phases}
    payload = {
        "schema_version": 1,
        "exitstatus": int(exitstatus),
        "collected": sorted(set(_collected)),
        "deselected": sorted(set(_deselected)),
        "tests": tests,
    }
    Path(output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
