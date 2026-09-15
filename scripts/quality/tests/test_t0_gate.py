"""Pure tests for the T0 changed-files gate (scripts/quality/t0_gate.py).

These verify the gate's own logic without running ruff/mypy/eslint for real:
the subprocess calls are mocked. They assert the contract that bit us before:

  * deleted files are recorded but NEVER passed to ruff/mypy/eslint;
  * an empty manifest (the origin/dev == HEAD false green) is a hard FAIL
    unless --allow-empty is explicit;
  * python / frontend / other files are partitioned correctly.
"""
import pytest
from t0_gate import (
    FileChange,
    Manifest,
    classify,
    evaluate,
    main,
    parse_name_status,
)

pytestmark = pytest.mark.pure_unit


def test_parse_name_status_handles_all_statuses() -> None:
    raw = "\n".join(
        [
            "M\tbackend/tests/a.py",
            "A\tbackend/tests/b.py",
            "D\tbackend/tests/c.py",
            "R100\tbackend/tests/old.py\tbackend/tests/new.py",
            "C100\tbackend/tests/src.py\tbackend/tests/dst.py",
            "T\tbackend/tests/e.py",
            "",
        ]
    )
    changes = parse_name_status(raw)
    # rename/copy each produce 2 entries (new exists, old deleted)
    # M(1) + A(1) + D(1) + R(2) + C(2) + T(1) = 8
    assert len(changes) == 8
    deleted = [c.path for c in changes if c.deleted]
    existing = [c.path for c in changes if not c.deleted]
    assert "backend/tests/c.py" in deleted
    assert "backend/tests/old.py" in deleted
    assert "backend/tests/src.py" in deleted
    assert "backend/tests/new.py" in existing
    assert "backend/tests/dst.py" in existing
    assert "backend/tests/a.py" in existing


def test_classify_partitions_correctly() -> None:
    changes = [
        FileChange("M", "backend/tests/a.py"),
        FileChange("D", "backend/tests/c.py", deleted=True),
        FileChange("A", "frontend/src/x.ts"),
        FileChange("M", "docs/notes.md"),
    ]
    manifest = classify("B", "H", changes)
    assert manifest.changed_paths == [
        "backend/tests/a.py",
        "backend/tests/c.py",
        "frontend/src/x.ts",
        "docs/notes.md",
    ]
    assert manifest.existing_changed_paths == [
        "backend/tests/a.py",
        "frontend/src/x.ts",
        "docs/notes.md",
    ]
    assert manifest.deleted_paths == ["backend/tests/c.py"]
    assert manifest.python_files == ["backend/tests/a.py"]
    assert manifest.frontend_files == ["frontend/src/x.ts"]
    assert manifest.other_files == ["docs/notes.md"]


def test_evaluate_empty_manifest_fails_without_allow_empty() -> None:
    manifest = Manifest(base="B", head="H")
    results = {
        "ruff": (0, ""),
        "mypy": (0, ""),
        "eslint": (0, "", True),
    }
    assert evaluate(manifest, results, allow_empty=False) is False
    assert evaluate(manifest, results, allow_empty=True) is True


def test_evaluate_fails_on_ruff_error() -> None:
    manifest = Manifest(base="B", head="H", changed_paths=["backend/tests/a.py"])
    results = {
        "ruff": (1, "F401"),
        "mypy": (0, ""),
        "eslint": (0, "", True),
    }
    assert evaluate(manifest, results, allow_empty=False) is False


def test_evaluate_passes_when_all_clean() -> None:
    manifest = Manifest(
        base="B",
        head="H",
        changed_paths=["backend/tests/a.py", "frontend/src/x.ts"],
        python_files=["backend/tests/a.py"],
        frontend_files=["frontend/src/x.ts"],
    )
    results = {
        "ruff": (0, ""),
        "mypy": (0, ""),
        "eslint": (0, "", False),
    }
    assert evaluate(manifest, results, allow_empty=False) is True


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(canned_diff: str, ruff_rc: int = 0, mypy_rc: int = 0, eslint_rc: int = 0):
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        prog = cmd[0]
        if prog == "git":
            return _FakeResult(0, canned_diff, "")
        if "ruff" in prog:
            return _FakeResult(ruff_rc, "", "ruff-err" if ruff_rc else "")
        if "eslint" in prog:
            return _FakeResult(eslint_rc, "", "")
        if "python" in prog or prog.endswith("python3"):
            # venv python -m mypy ...
            return _FakeResult(mypy_rc, "", "mypy-err" if mypy_rc else "")
        return _FakeResult(0, "", "")

    return fake_run, calls


def test_main_end_to_end_deleted_files_excluded() -> None:
    canned = "\n".join(
        [
            "M\tbackend/tests/foo.py",
            "D\tbackend/tests/bar.py",
            "R100\tbackend/tests/old.py\tbackend/tests/new.py",
            "A\tfrontend/src/x.ts",
            "",
        ]
    )
    fake_run, calls = _make_fake_run(canned)
    import t0_gate

    monkeypatch_run(t0_gate, fake_run)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 0

    all_args = " ".join(" ".join(c) for c in calls)
    # deleted paths must never reach a checker (basename is robust: mypy strips
    # the backend/ prefix and passes relative paths, ruff keeps repo-relative)
    assert "bar.py" not in all_args
    assert "old.py" not in all_args
    # but existing python/frontend paths must be checked
    assert "foo.py" in all_args
    assert "new.py" in all_args
    assert "x.ts" in all_args


def test_main_empty_manifest_returns_1() -> None:
    fake_run, _ = _make_fake_run("")  # empty diff
    import t0_gate

    monkeypatch_run(t0_gate, fake_run)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 1  # origin/dev == HEAD false green => hard FAIL


def test_main_worktree_builds_correct_git_args() -> None:
    canned = "M\tbackend/tests/foo.py\n"
    fake_run, calls = _make_fake_run(canned)
    import t0_gate

    monkeypatch_run(t0_gate, fake_run)
    rc = main(["--base", "1775d0b1", "--worktree"])
    assert rc == 0
    git_calls = [c for c in calls if c and c[0] == "git"]
    # worktree mode must diff against the base (unstaged + cached), not BASE...HEAD
    assert any("diff" in c and "--cached" in c for c in git_calls)
    assert not any("..." in " ".join(c) for c in git_calls)


def monkeypatch_run(t0_gate_module, fake_run) -> None:  # noqa: ANN001
    import unittest.mock as mock

    mock.patch.object(t0_gate_module.subprocess, "run", side_effect=fake_run).start()
