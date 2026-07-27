"""tools/check_architecture.py v2 docs structure 检查测试。

[eaffb11 文档重构] 测试已更新以匹配新结构：
- docs/current/ 替换为 docs/prd/（10 个按领域 00-90 编号的 PRD 文件）
- docs/maps/ 仍是 10 个按领域编号的 map 文件
- 旧 docs/current/ 文件名（如 MANIFEST.md、00-product-business.md）在 docs/prd/ 中残留会触发 violation

覆盖 check_v2_docs_structure() 的 6 个场景：
1. 10 PRD + 10 maps + 无 legacy → 0 violations；
2. 缺 1 个 required PRD → violation；
3. 缺 1 个 required map → violation；
4. prd/ 下残留旧文件名 → violation；
5. prd/ 目录不存在 → violation；
6. maps/ 目录不存在 → violation。

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

import check_architecture as ca


def _create_full_v2_structure(root: Path) -> None:
    """在 root 下创建完整的 v2 docs 结构。

    [eaffb11] 新结构：docs/prd/ + docs/maps/，各 10 个按领域编号的文件。
    """
    prd_dir = root / "docs" / "prd"
    maps_dir = root / "docs" / "maps"
    prd_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)

    for name in ca.V2_REQUIRED_PRD_FILES:
        (prd_dir / name).write_text(f"# {name}\n", encoding="utf-8")

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
        """10 PRD + 10 maps + 无 legacy → 0 violations。"""
        _create_full_v2_structure(tmp_path)
        violations = _run_check(tmp_path, monkeypatch)
        assert violations == [], f"期望 0 violations，实际: {violations}"

    def test_02_missing_required_prd_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 1 个 required PRD 文件 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        # 删除一个必需的 PRD 文件
        (tmp_path / "docs" / "prd" / "00-product-scope.md").unlink()
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("00-product-scope.md" in v.context for v in violations)

    def test_03_missing_required_map_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 1 个 required map 文件 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        # 删除一个必需的 map 文件
        (tmp_path / "docs" / "maps" / "00-system-overview.md").unlink()
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("00-system-overview.md" in v.context for v in violations)

    def test_04_legacy_current_file_residual_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/prd/ 下残留旧 docs/current/ 文件名 → 至少 1 violation。"""
        _create_full_v2_structure(tmp_path)
        # 在 docs/prd/ 中创建一个旧 docs/current/ 的文件名
        (tmp_path / "docs" / "prd" / "MANIFEST.md").write_text(
            "# legacy\n", encoding="utf-8"
        )
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("MANIFEST.md" in v.context for v in violations)

    def test_05_prd_dir_missing_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/prd/ 目录不存在 → violation。"""
        maps_dir = tmp_path / "docs" / "maps"
        maps_dir.mkdir(parents=True, exist_ok=True)
        for name in ca.V2_REQUIRED_MAP_FILES:
            (maps_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("prd" in v.context for v in violations)

    def test_06_maps_dir_missing_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/maps/ 目录不存在 → 至少 1 violation（每个 required map 缺失报一条）。"""
        prd_dir = tmp_path / "docs" / "prd"
        prd_dir.mkdir(parents=True, exist_ok=True)
        for name in ca.V2_REQUIRED_PRD_FILES:
            (prd_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        violations = _run_check(tmp_path, monkeypatch)
        assert len(violations) >= 1
        assert any("maps" in v.context.lower() for v in violations)
