"""Pure tests for the T1 modified-scope unit gate (scripts/quality/t1_gate.py).

These verify T1's own logic without running the real backend/frontend/quality
test runners: subprocess is mocked and changed-file identity is delegated to a
mocked t0_gate. They assert the contract that Commit B is meant to enforce:

  * changed backend test  -> auto selected and (mock) run;
  * changed frontend __tests__ -> auto selected;
  * backend production + matching backend test -> PASS (not T1_REQUIRED);
  * backend/frontend production with NO targeted test -> T1_REQUIRED (rc!=0);
  * explicit backend selector (incl. pytest nodeid) -> accepted;
  * `../` or non-existent explicit selector -> rejected (hard failure);
  * postgres/external selector deselected to 0 collected -> FAIL (no false green);
  * deleted test file -> not run;
  * docs-only change -> NOT_APPLICABLE;
  * quality test changed -> auto run;
  * committed range is not polluted by worktree/untracked junk;
  * non-ancestor BASE/HEAD committed range -> IDENTITY_INVALID.

Isolation: every monkeypatch is done through the pytest `monkeypatch` fixture, so
patches are automatically reverted per test. No `.start()` leaks remain.
"""
from pathlib import Path

import pytest
import t0_gate
import t1_gate
from t1_gate import categorize, main, validate_backend_selectors

pytestmark = pytest.mark.pure_unit


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(
    backend_rc: int = 0,
    backend_out: str = "7 passed in 0.10s",
    frontend_rc: int = 0,
    frontend_out: str = "tests 3\npassing 3",
    quality_rc: int = 0,
    quality_out: str = "3 passed in 0.05s",
):
    def fake_run(args, *a, **k):  # noqa: ANN001
        s = " ".join(str(x) for x in args)
        if "pytest" in s:
            # backend run carries the "-m not postgres" marker; quality run does not
            if "-m" in s:
                return _FakeResult(backend_rc, backend_out, "")
            return _FakeResult(quality_rc, quality_out, "")
        if "tsx" in s:
            return _FakeResult(frontend_rc, frontend_out, "")
        return _FakeResult(0, "", "")

    return fake_run


def _run(
    monkeypatch,
    changed,  # noqa: ANN001
    args=None,  # noqa: ANN001
    ancestor: bool = True,
    fake_run=None,  # noqa: ANN001
    existing=None,  # noqa: ANN001
):  # noqa: ANN001
    monkeypatch.setattr(t0_gate, "_assert_ancestor", lambda b, h: ancestor)
    monkeypatch.setattr(t1_gate, "changed_paths_via_t0", lambda b, h, w: changed)
    if fake_run is not None:
        monkeypatch.setattr(t1_gate.subprocess, "run", fake_run)
    if existing is not None:
        monkeypatch.setattr(t1_gate, "_existing", existing)
    return main((args or []) + ["--base", "X", "--head", "Y"])


# --------------------------------------------------------------------------
# Pure categorization (no IO)
# --------------------------------------------------------------------------
def test_categorize_backend_test_auto():
    sel = categorize(["backend/tests/test_foo.py"])
    assert sel.backend_auto == ["tests/test_foo.py"]
    assert sel.backend_production == []


def test_categorize_frontend_test_auto():
    sel = categorize(["frontend/src/features/x/__tests__/y.test.ts"])
    assert "src/features/x/__tests__/y.test.ts" in sel.frontend_auto
    assert sel.frontend_production == []


def test_categorize_quality_infra_and_test():
    sel = categorize(
        ["scripts/quality/t1_gate.py", "scripts/quality/tests/test_t1_gate.py"]
    )
    assert "scripts/quality/t1_gate.py" in sel.quality_infra
    assert "scripts/quality/tests/test_t1_gate.py" in sel.quality_auto


def test_committed_range_not_polluted_by_junk():
    changed = [
        "frontend/src/foo.ts",
        "frontend/dist/x.js",  # build output: not frontend/src -> not production
        ".tmp/x.py",  # junk: not backend/app
        "backend/app/foo.py",
        "backend/tests/test_foo.py",
        "docs/notes.md",
    ]
    sel = categorize(changed)
    assert sel.frontend_production == ["frontend/src/foo.ts"]
    assert sel.frontend_auto == []
    assert sel.backend_production == ["backend/app/foo.py"]
    assert sel.backend_auto == ["tests/test_foo.py"]
    assert "frontend/dist/x.js" in sel.other
    assert ".tmp/x.py" in sel.other


# --------------------------------------------------------------------------
# Production changed without a targeted test => T1_REQUIRED
# --------------------------------------------------------------------------
def test_backend_production_no_test_required(monkeypatch, capsys):  # noqa: ANN001
    rc = _run(monkeypatch, ["backend/app/services/foo_service.py"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "T1 UNKNOWN / REQUIRED" in captured.out
    assert "backend production changed but no backend targeted test" in captured.out


def test_frontend_production_no_test_required(monkeypatch, capsys):  # noqa: ANN001
    rc = _run(monkeypatch, ["frontend/src/foo.ts"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "T1 UNKNOWN / REQUIRED" in captured.out
    assert "frontend production changed but no frontend targeted test" in captured.out


# --------------------------------------------------------------------------
# Production changed WITH a targeted test => PASS
# --------------------------------------------------------------------------
def test_backend_prod_plus_test_passes(monkeypatch, capsys):  # noqa: ANN001
    fake = _make_fake_run()
    rc = _run(
        monkeypatch,
        ["backend/app/foo.py", "backend/tests/test_foo.py"],
        fake_run=fake,
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "T1 PASS" in captured.out


def test_frontend_prod_plus_test_passes(monkeypatch, capsys):  # noqa: ANN001
    fake = _make_fake_run()
    rc = _run(
        monkeypatch,
        ["frontend/src/foo.ts", "frontend/src/features/x/__tests__/foo.test.ts"],
        fake_run=fake,
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "T1 PASS" in captured.out


def test_quality_test_changed_auto_runs(monkeypatch, capsys):  # noqa: ANN001
    fake = _make_fake_run()
    rc = _run(
        monkeypatch,
        ["scripts/quality/t1_gate.py", "scripts/quality/tests/test_t1_gate.py"],
        fake_run=fake,
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "T1 PASS" in captured.out
    assert "Auto quality tests" in captured.out


# --------------------------------------------------------------------------
# Explicit selectors
# --------------------------------------------------------------------------
def test_explicit_backend_selector_accepted(monkeypatch, capsys):  # noqa: ANN001
    fake = _make_fake_run()
    rc = _run(
        monkeypatch,
        [],
        args=["--backend-tests", "tests/test_foo.py"],
        fake_run=fake,
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "T1 PASS" in captured.out


def test_explicit_pytest_nodeid_accepted(monkeypatch):  # noqa: ANN001
    fake = _make_fake_run()
    rc = _run(
        monkeypatch,
        [],
        args=["--backend-tests", "tests/test_foo.py::test_case"],
        fake_run=fake,
        existing=lambda p: True,
    )
    assert rc == 0


def test_dotdot_selector_rejected(monkeypatch, capsys):  # noqa: ANN001
    rc = _run(
        monkeypatch,
        [],
        args=["--backend-tests", "../escape.py"],
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Rejected selectors" in captured.out


def test_selector_not_exist_rejected(monkeypatch, capsys):  # noqa: ANN001
    def _exists(p):  # noqa: ANN001
        return "nope" not in str(p)

    rc = _run(
        monkeypatch,
        [],
        args=["--backend-tests", "tests/nope.py"],
        existing=_exists,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Rejected selectors" in captured.out


def test_postgres_excluded_zero_executed_fails(monkeypatch, capsys):  # noqa: ANN001
    # pytest returns 5 when the only selected tests are deselected (0 collected).
    fake = _make_fake_run(backend_rc=5, backend_out="no tests ran")
    rc = _run(
        monkeypatch,
        ["backend/tests/test_only_pg.py"],
        fake_run=fake,
        existing=lambda p: True,
    )
    captured = capsys.readouterr()
    assert rc == 1  # 0 collected must FAIL, never "nothing ran => PASS"
    assert "T1 FAIL" in captured.out


def test_deleted_test_file_not_run(monkeypatch):  # noqa: ANN001
    def _exists(p):  # noqa: ANN001
        return "deleted" not in str(p)

    monkeypatch.setattr(t1_gate, "_existing", _exists)
    accepted, rejected = validate_backend_selectors("backend/tests/deleted.py", Path("/repo"))
    # the test lives under backend/tests but the file is gone on disk -> rejected
    assert accepted == []
    assert rejected == ["backend/tests/deleted.py"]


# --------------------------------------------------------------------------
# Docs-only / not applicable
# --------------------------------------------------------------------------
def test_docs_only_not_applicable(monkeypatch, capsys):  # noqa: ANN001
    rc = _run(monkeypatch, ["docs/notes.md", "Makefile"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "T1 NOT_APPLICABLE" in captured.out


# --------------------------------------------------------------------------
# Identity gate reuse
# --------------------------------------------------------------------------
def test_committed_range_non_ancestor_fails(monkeypatch, capsys):  # noqa: ANN001
    rc = _run(monkeypatch, ["backend/tests/test_foo.py"], ancestor=False)
    captured = capsys.readouterr()
    assert rc == 2
    assert "IDENTITY_INVALID" in captured.err
