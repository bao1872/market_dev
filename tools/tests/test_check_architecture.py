"""tools/check_architecture.py v2 docs structure 检查测试。

覆盖 check_v2_docs_structure() 的 6 个场景：
1. 9 current + 8 maps + 无 legacy → 0 violations；
2. 缺 1 个 required current → violation；
3. 缺 1 个 required map → violation；
4. current/ 下残留旧 00-18 文件 → violation；
5. current/ 目录不存在 → violation；
6. maps/ 目录不存在 → violation。

使用 tmp_path + monkeypatch 注入临时 ROOT，不修改真实 docs。

运行:
    python -m pytest tools/tests/test_check_architecture.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 tools/ 加入 sys.path 以导入 check_architecture
_TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_architecture as ca  # noqa: E402


def _create_full_v2_structure(root: Path) -> None:
    """在 root 下创建完整的 v2 docs 结构（9 current + 8 maps）。"""
    current_dir = root / "docs" / "current"
    maps_dir = root / "docs" / "maps"
    current_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)

    for name in ca.V2_REQUIRED_CURRENT_FILES:
        (current_dir / name).write_text(f"# {name}\n", encoding="utf-8")

    for name in ca.V2_REQUIRED_MAP_FILES:
        (maps_dir / name).write_text(f"# {name}\n", encoding="utf-8")


def _run_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[ca.Violation]:
    """Monkeypatch ROOT 到 tmp_path 并运行 check_v2_docs_structure。"""
    monkeypatch.setattr(ca, "ROOT", tmp_path)
    return ca.check_v2_docs_structure()


class TestCheckV2DocsStructure:
    """v2 docs structure 检查的 6 个测试场景。"""

    def test_01_all_required_files_present_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """9 current + 8 maps + 无 legacy → 0 violations。"""
        _create_full_v2_structure(tmp_path)
        violations = _run_check(tmp_path, monkeypatch)
        assert violations == [], f"期望 0 violations，实际: {violations}"

    def test_02_missing_required_current_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 1 个 required current 文件 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        (tmp_path / "docs" / "current" / "MANIFEST.md").unlink()
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("MANIFEST.md" in v.context for v in violations)

    def test_03_missing_required_map_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 1 个 required map 文件 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        (tmp_path / "docs" / "maps" / "api-route-map.md").unlink()
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("api-route-map.md" in v.context for v in violations)

    def test_04_legacy_current_file_residual_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current/ 下残留旧 00-18 文件 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        (tmp_path / "docs" / "current" / "11-jobs-integrations.md").write_text(
            "# legacy\n", encoding="utf-8"
        )
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("11-jobs-integrations.md" in v.context for v in violations)

    def test_05_current_dir_missing_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current/ 目录不存在 → violation。"""
        maps_dir = tmp_path / "docs" / "maps"
        maps_dir.mkdir(parents=True, exist_ok=True)
        for name in ca.V2_REQUIRED_MAP_FILES:
            (maps_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("current" in v.context for v in violations)

    def test_06_maps_dir_missing_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maps/ 目录不存在 → 8 个 violation（每个 required map 缺失报一条）。"""
        current_dir = tmp_path / "docs" / "current"
        current_dir.mkdir(parents=True, exist_ok=True)
        for name in ca.V2_REQUIRED_CURRENT_FILES:
            (current_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("maps" in v.context.lower() for v in violations)
