"""Pure tests for the T0 changed-files gate (scripts/quality/t0_gate.py).

These verify the gate's own logic without running ruff/mypy/eslint/tsc for real:
the subprocess calls are mocked. They assert the contract that bit us before:

  * deleted files are recorded but NEVER passed to ruff/mypy/py_compile/eslint/tsc;
  * a copy (C) leaves the source file existing (NOT deleted);
  * a rename (R) deletes the old path and adds the new path;
  * an empty manifest (the origin/dev == HEAD false green) is a hard FAIL
    unless --allow-empty is explicit — for the full T0 gate;
  * a committed range whose BASE is not an ancestor of HEAD is a hard FAIL
    (IDENTITY_INVALID): the three-dot diff would otherwise silently use the
    merge-base and not represent the claimed task delta;
  * python / frontend / other files are partitioned correctly;
  * a language with changed source but MISSING required tooling is a hard FAIL
    (TOOLING_MISSING) — never a silent pass.

Isolation: every monkeypatch is done through the pytest `monkeypatch` fixture, so
patches are automatically reverted per test. No `.start()` leaks remain.
"""
import pytest
from t0_gate import (
    CheckerResult,
    FileChange,
    Manifest,
    classify,
    evaluate,
    git_diff_name_status,
    main,
    parse_name_status,
)

pytestmark = pytest.mark.pure_unit


def _patch_run(monkeypatch, t0_gate_module, fake_run) -> None:
    monkeypatch.setattr(t0_gate_module.subprocess, "run", fake_run)


def _patch_attr(monkeypatch, module, name: str, value) -> None:
    monkeypatch.setattr(module, name, value)


def _patch_resolvers(monkeypatch, t0_gate_module) -> None:
    """Point every tool resolver at a dummy binary so the mocked subprocess runs.

    Without this, run_* checks the REAL filesystem for the binary and would report
    TOOLING_MISSING in a test environment that lacks the venv / node_modules.
    """
    _patch_attr(monkeypatch, t0_gate_module, "_resolve_ruff", lambda: "/fake/ruff")
    _patch_attr(monkeypatch, t0_gate_module, "_resolve_mypy_python", lambda: "/fake/python")
    _patch_attr(monkeypatch, t0_gate_module, "_resolve_eslint", lambda: "/fake/eslint")
    _patch_attr(monkeypatch, t0_gate_module, "_resolve_tsc", lambda: "/fake/tsc")


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(
    canned_diff: str,
    ruff_rc: int = 0,
    mypy_rc: int = 0,
    eslint_rc: int = 0,
    tsc_rc: int = 0,
    pycompile_rc: int = 0,
):
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        prog = cmd[0]
        if prog == "git":
            return _FakeResult(0, canned_diff, "")
        if "ruff" in prog:
            return _FakeResult(ruff_rc, "", "ruff-err" if ruff_rc else "")
        if "eslint" in prog:
            return _FakeResult(eslint_rc, "", "")
        if "tsc" in prog:
            return _FakeResult(tsc_rc, "", "")
        if "py_compile" in cmd:
            return _FakeResult(pycompile_rc, "", "")
        if "mypy" in cmd:
            return _FakeResult(mypy_rc, "", "mypy-err" if mypy_rc else "")
        if "python" in prog or prog.endswith("python3"):
            return _FakeResult(mypy_rc, "", "mypy-err" if mypy_rc else "")
        return _FakeResult(0, "", "")

    return fake_run, calls


def test_parse_name_status_handles_all_statuses() -> None:
    raw = "M\tbackend/tests/a.py\nA\tbackend/tests/b.py\nD\tbackend/tests/c.py\nR100\tbackend/tests/old.py\tbackend/tests/new.py\nC100\tbackend/tests/src.py\tbackend/tests/dst.py\nT\tbackend/tests/e.py\n"
    changes = parse_name_status(raw)
    # M(1) + A(1) + D(1) + R(2: new exists + old deleted) + C(1: only new,
    # source is NOT deleted) + T(1) = 7
    assert len(changes) == 7
    existing = [c.path for c in changes if not c.deleted]
    deleted = [c.path for c in changes if c.deleted]
    all_paths = [c.path for c in changes]

    # rename: new exists, old deleted
    assert "backend/tests/new.py" in existing
    assert "backend/tests/old.py" in deleted
    # copy: new exists, OLD is intentionally NOT in the manifest at all
    assert "backend/tests/dst.py" in existing
    assert "backend/tests/src.py" not in all_paths
    # plain delete / modify / add
    assert "backend/tests/c.py" in deleted
    assert "backend/tests/a.py" in existing
    assert "backend/tests/b.py" in existing
    assert "backend/tests/e.py" in existing


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
        "ruff": CheckerResult(0, ""),
        "py_compile": CheckerResult(0, ""),
        "mypy": CheckerResult(0, ""),
        "eslint": CheckerResult(0, "", skipped=True),
        "tsc": CheckerResult(0, "", skipped=True),
    }
    assert evaluate(manifest, results, allow_empty=False) is False
    assert evaluate(manifest, results, allow_empty=True) is True


def test_evaluate_fails_on_ruff_error() -> None:
    manifest = Manifest(base="B", head="H", changed_paths=["backend/tests/a.py"])
    results = {
        "ruff": CheckerResult(1, "F401"),
        "py_compile": CheckerResult(0, ""),
        "mypy": CheckerResult(0, ""),
        "eslint": CheckerResult(0, "", skipped=True),
        "tsc": CheckerResult(0, "", skipped=True),
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
        "ruff": CheckerResult(0, ""),
        "py_compile": CheckerResult(0, ""),
        "mypy": CheckerResult(0, ""),
        "eslint": CheckerResult(0, "", skipped=False),
        "tsc": CheckerResult(0, "", skipped=False),
    }
    assert evaluate(manifest, results, allow_empty=False) is True


def test_evaluate_fails_on_tooling_missing() -> None:
    manifest = Manifest(
        base="B",
        head="H",
        changed_paths=["frontend/src/x.ts"],
        frontend_files=["frontend/src/x.ts"],
    )
    results = {
        "ruff": CheckerResult(0, "", skipped=True),
        "py_compile": CheckerResult(0, "", skipped=True),
        "mypy": CheckerResult(0, "", skipped=True),
        "eslint": CheckerResult(2, "missing", tooling_missing=True),
        "tsc": CheckerResult(0, "", skipped=False),
    }
    assert evaluate(manifest, results, allow_empty=False) is False


def test_main_end_to_end_deleted_files_excluded(monkeypatch) -> None:
    import t0_gate

    canned = "M\tbackend/tests/foo.py\nD\tbackend/tests/bar.py\nR100\tbackend/tests/old.py\tbackend/tests/new.py\nA\tfrontend/src/x.ts\n"
    fake_run, calls = _make_fake_run(canned)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 0

    all_args = " ".join(" ".join(c) for c in calls)
    # deleted paths must never reach a checker
    assert "bar.py" not in all_args
    assert "old.py" not in all_args
    # existing python/frontend paths must be checked by every scheduled checker
    assert "foo.py" in all_args
    assert "new.py" in all_args
    assert "x.ts" in all_args
    # py_compile is now a scheduled checker
    assert any("py_compile" in c for c in calls)


def test_python_changed_schedules_ruff_pycompile_mypy(monkeypatch) -> None:
    import t0_gate

    # backend/app file (not a test file) so the production Mypy gate is scheduled.
    canned = "M\tbackend/app/foo.py\n"
    fake_run, calls = _make_fake_run(canned)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 0
    assert any("ruff" in c[0] for c in calls if c)
    assert any("py_compile" in c for c in calls)
    assert any("mypy" in c for c in calls)


def test_main_empty_manifest_returns_1(monkeypatch) -> None:
    import t0_gate

    fake_run, _ = _make_fake_run("")  # empty diff
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 1  # origin/dev == HEAD false green => hard FAIL


def test_main_worktree_builds_correct_git_args(monkeypatch) -> None:
    import t0_gate

    canned = "M\tbackend/tests/foo.py\n"
    fake_run, calls = _make_fake_run(canned)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "1775d0b1", "--worktree"])
    assert rc == 0
    git_calls = [c for c in calls if c and c[0] == "git"]
    # worktree mode must diff against the base (unstaged + cached), not BASE...HEAD
    assert any("diff" in c and "--cached" in c for c in git_calls)
    assert not any("..." in " ".join(c) for c in git_calls)


def test_main_tooling_missing_eslint_fails(monkeypatch) -> None:
    import t0_gate

    canned = "A\tfrontend/src/x.ts\n"
    fake_run, _ = _make_fake_run(canned)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    _patch_attr(monkeypatch, t0_gate, "_resolve_eslint", lambda: None)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 1  # frontend changed + eslint missing => FAIL, not PASS


def test_main_tooling_missing_tsc_fails(monkeypatch) -> None:
    import t0_gate

    canned = "A\tfrontend/src/x.ts\n"
    fake_run, _ = _make_fake_run(canned)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    _patch_attr(monkeypatch, t0_gate, "_resolve_tsc", lambda: None)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 1  # frontend changed + tsc missing => FAIL, not PASS


def _make_policy_fake_run(
    canned_diff: str,
    *,
    mypy_rc: int = 0,
    ruff_rc: int = 0,
    pycompile_rc: int = 0,
    eslint_rc: int = 0,
    tsc_rc: int = 0,
):
    """Fake run that distinguishes mypy from py_compile / ruff / frontend tools.

    The default _make_fake_run conflates 'python' in prog with mypy; this helper
    keys off the invoked submodule so py_compile and mypy can be set independently.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        prog = cmd[0]
        if prog == "git":
            return _FakeResult(0, canned_diff, "")
        if "ruff" in prog:
            return _FakeResult(ruff_rc, "", "ruff-err" if ruff_rc else "")
        if "eslint" in prog:
            return _FakeResult(eslint_rc, "", "")
        if "tsc" in prog:
            return _FakeResult(tsc_rc, "", "")
        if "py_compile" in cmd:
            return _FakeResult(pycompile_rc, "", "")
        if "mypy" in cmd:
            return _FakeResult(mypy_rc, "", "mypy-err" if mypy_rc else "")
        return _FakeResult(0, "", "")

    return fake_run, calls


def test_modified_legacy_production_mypy_errors_warn_not_block(monkeypatch, capsys) -> None:
    import t0_gate

    canned = "M\tbackend/app/mod.py\n"
    fake_run, _ = _make_policy_fake_run(canned, mypy_rc=3)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 0  # advisory mypy does not block T0
    out = capsys.readouterr().out
    assert "mypy_legacy [WARN]" in out


def test_new_production_mypy_error_blocks(monkeypatch) -> None:
    import t0_gate

    canned = "A\tbackend/app/new.py\n"
    fake_run, _ = _make_policy_fake_run(canned, mypy_rc=3)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 1  # new production file mypy is HARD


def test_tests_excluded_from_production_mypy(monkeypatch) -> None:
    import t0_gate

    canned = "M\tbackend/tests/foo.py\nM\tbackend/app/bar.py\n"
    fake_run, calls = _make_policy_fake_run(canned, mypy_rc=3)
    _patch_run(monkeypatch, t0_gate, fake_run)
    _patch_resolvers(monkeypatch, t0_gate)
    rc = main(["--base", "0582d202", "--head", "1775d0b1"])
    assert rc == 0  # modified-legacy mypy is advisory -> WARN, not blocking
    mypy_calls = [c for c in calls if "mypy" in c]
    assert mypy_calls, "mypy should still run on backend/app files"
    for c in mypy_calls:
        joined = " ".join(c)
        assert "foo.py" not in joined, "test files must not enter production Mypy"
        assert "bar.py" in joined


def test_committed_range_excludes_untracked_python(monkeypatch) -> None:
    import t0_gate

    def fake_git(args):
        joined = " ".join(args)
        if "--name-status" in args and "..." in joined:
            return "M\tbackend/app/real.py\n"
        if "ls-files" in args:
            return "scratch/untracked.py\n"
        return ""

    _patch_attr(monkeypatch, t0_gate, "_git", fake_git)
    raw = git_diff_name_status("BASE", "HEAD", worktree=False)
    # committed range must NOT pull in untracked files
    assert "scratch/untracked.py" not in raw
    assert "backend/app/real.py" in raw


def test_worktree_includes_legit_new_source_but_filters_junk(monkeypatch) -> None:
    import t0_gate

    def fake_git(args):
        if "ls-files" in args:
            return "frontend/src/new.ts\n.tmp_junk/x.py\n"
        return ""  # unstaged / cached diffs empty

    _patch_attr(monkeypatch, t0_gate, "_git", fake_git)
    raw = git_diff_name_status("BASE", "HEAD", worktree=True)
    assert "frontend/src/new.ts" in raw  # legit new source enters manifest
    assert ".tmp_junk/x.py" not in raw  # junk is filtered out


def test_committed_range_base_not_ancestor_head_fails(monkeypatch) -> None:
    import t0_gate

    # Simulate BASE not being an ancestor of HEAD; the gate must refuse the
    # three-dot diff (which would otherwise silently use the merge-base).
    _patch_attr(monkeypatch, t0_gate, "_assert_ancestor", lambda base, head: False)
    rc = main(["--base", "BADBASE", "--head", "H"])
    assert rc == 2  # IDENTITY_INVALID, checkers not run
