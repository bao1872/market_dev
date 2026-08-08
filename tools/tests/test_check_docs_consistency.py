from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import check_docs_consistency as cdc

SHA = "a" * 40


def setup_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stage: str = "EXPLORATION") -> Path:
    docs = tmp_path / "docs"
    for folder in ("prd", "maps", "changes/2026", "runbooks"):
        (docs / folder).mkdir(parents=True, exist_ok=True)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(f"PROJECT_STAGE = {stage}\nref/ 仅供参考，不得作为运行依赖。\n", encoding="utf-8")
    (docs / "maps/00-system-overview.md").write_text(f"# Map\n核验提交：`{SHA}`\n", encoding="utf-8")
    (docs / "prd/00-product.md").write_text("# Product\n", encoding="utf-8")
    (docs / "changes/INDEX.md").write_text("# Index\n", encoding="utf-8")

    monkeypatch.setattr(cdc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cdc, "DOCS_DIR", docs)
    monkeypatch.setattr(cdc, "AGENTS_FILE", agents)
    monkeypatch.setattr(cdc, "MAP_BASELINE_FILE", docs / "maps/00-system-overview.md")
    monkeypatch.setattr(cdc, "_is_valid_commit", lambda sha: True)
    monkeypatch.setattr(cdc, "_is_ancestor", lambda sha, rev="HEAD": True)
    monkeypatch.setattr(cdc, "_commits_ahead", lambda sha, rev="HEAD": 1)
    return tmp_path


def test_exploration_does_not_require_acceptance_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch, stage="EXPLORATION")
    assert cdc.check_hardening_acceptance() == []


def test_hardening_requires_acceptance_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch, stage="HARDENING")
    assert any("requires an acceptance matrix" in e for e in cdc.check_hardening_acceptance())


def test_hardening_rejects_stale_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch, stage="HARDENING")
    matrix = tmp_path / "docs/changes/2026/PRD-Acceptance-Matrix-2026-08-08.md"
    matrix.write_text(f"# Matrix\n**基线**: `{SHA}`\n", encoding="utf-8")
    monkeypatch.setattr(cdc, "_commits_ahead", lambda sha, rev="HEAD": 5)
    errors = cdc.check_hardening_acceptance()
    assert any("stale" in e for e in errors)


def test_broken_active_link_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch)
    p = tmp_path / "docs/prd/00-product.md"
    p.write_text("# Product\n[missing](../maps/nope.md)\n")
    assert cdc.check_local_links()


def test_archive_link_rot_does_not_block_exploration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch)
    archive = tmp_path / "docs/archive/old"
    archive.mkdir(parents=True)
    (archive / "x.md").write_text("[missing](nope.md)\n")
    assert cdc.check_local_links() == []


def test_active_placeholder_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch)
    (tmp_path / "docs/prd/00-product.md").write_text("# Product\n待填写\n")
    assert cdc.check_placeholders()


def test_webhook_active_regression_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch)
    (tmp_path / "docs/prd/00-product.md").write_text("当前使用 feishu_webhook 发送。\n")
    assert cdc.check_webhook_regression()


def test_deleted_webhook_reference_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch)
    (tmp_path / "docs/prd/00-product.md").write_text("feishu_webhook 已删除，不得恢复。\n")
    assert cdc.check_webhook_regression() == []


def test_acceptance_matrix_selected_by_filename_date_not_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_repo(tmp_path, monkeypatch, stage="HARDENING")
    changes = tmp_path / "docs/changes/2026"
    newer = changes / "PRD-Acceptance-Matrix-2026-08-08.md"
    older = changes / "PRD-Acceptance-Matrix-2026-07-01.md"
    newer.write_text(f"# Matrix\n**基线**: `{SHA}`\n", encoding="utf-8")
    older.write_text(f"# Matrix\n**基线**: `{SHA}`\n", encoding="utf-8")
    # 人为把较早文件 mtime 改新，验证选择不依赖 mtime，而按文件名日期取最新。
    import os

    old_mtime = os.stat(older).st_mtime
    os.utime(older, (old_mtime + 100000, old_mtime + 100000))
    latest = cdc._latest_acceptance_matrix()
    assert latest is not None
    assert "2026-08-08" in latest.name
