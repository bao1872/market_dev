"""Node Cluster 架构守护 AST 测试（CHANGE-20260718-004）。

验证 Node Cluster 唯一计算内核架构：

1. 除 `node_cluster_engine.py` 外，`app/` 生产模块禁止从
   `unified_volume_profile` 导入 `compute_unified_volume_profile`。
2. 除 `node_cluster_engine.py` 外，`app/` 生产模块禁止从
   `luxalgo_volume_profile_pytdx_15m_aligned` 导入任何名称。
3. `strategy_assets/algorithms/features/` 目录是底层 VP kernel 所在，
   不受约束（engine 通过它调用）。
4. 业务模块禁止直接调用 `compute_volume_profile`（应通过 engine）。
5. 前端禁止 `computeVolumeProfile` / `computeUnifiedVolumeProfile` /
   对 profile nodes 做 value area 过滤。

模式参考 `test_market_data_ssot_architecture.py`（_iter_production_files + ast.walk）。
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "backend" / "app"
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

# engine 是唯一允许导入底层 VP 的模块
_ENGINE_MODULE = "services/node_cluster_engine.py"

# 底层 VP kernel 所在目录（允许内部互相导入）
_VP_KERNEL_DIR = "strategy_assets/algorithms/features"

# 禁止业务模块从 unified_volume_profile 导入的名称
_FORBIDDEN_FROM_UVP = {
    "compute_unified_volume_profile",
    "prepare_node_cluster_bars",
    "UnifiedVolumeProfileResult",
    "NodeClusterBarsResult",
}

# 禁止业务模块从 luxalgo_volume_profile_pytdx_15m_aligned 导入的名称（全部）
_FORBIDDEN_FROM_LUXALGO = {
    "compute_volume_profile",
    "VolumeProfileConfig",
    "VolumeProfileResult",
}


def _iter_production_files() -> list[tuple[Path, str]]:
    """枚举 app/ 下所有生产 .py 文件，返回 (path, rel_posix) 列表。

    跳过：engine 本身、VP kernel 目录、__pycache__。
    """
    result: list[tuple[Path, str]] = []
    for p in sorted(_APP_DIR.rglob("*.py")):
        rel = p.relative_to(_APP_DIR).as_posix()
        if rel == _ENGINE_MODULE:
            continue
        if rel.startswith(_VP_KERNEL_DIR):
            continue
        if "__pycache__" in rel:
            continue
        result.append((p, rel))
    return result


def _imported_names_from(tree: ast.AST, module_suffix: str) -> set[str]:
    """返回从以 module_suffix 结尾的模块导入的名称集合。

    匹配 `from ....unified_volume_profile import X` 等（任意层级前缀）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith(module_suffix) or node.module.split(".")[-1] == module_suffix:
                for alias in node.names:
                    names.add(alias.name)
    return names


def _has_call_to(tree: ast.AST, func_name: str) -> bool:
    """是否存在直接函数调用 `func_name(...)`（非属性调用）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name:
            return True
    return False


# =============================================================================
# 后端 AST 守护
# =============================================================================


class TestBackendArchitecture:
    """1-4. 后端生产模块禁止直接导入/调用底层 VP。"""

    def test_no_business_module_imports_compute_unified_volume_profile(self):
        """除 engine 外，禁止从 unified_volume_profile 导入 compute_unified_volume_profile 等。"""
        violations: list[str] = []
        for path, rel in _iter_production_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            imported = _imported_names_from(tree, "unified_volume_profile")
            bad = imported & _FORBIDDEN_FROM_UVP
            if bad:
                violations.append(f"{rel}: 从 unified_volume_profile 导入 {bad}")
        assert not violations, (
            "以下业务模块直接导入 unified_volume_profile（应通过 node_cluster_engine）:\n"
            + "\n".join(violations)
        )

    def test_no_business_module_imports_luxalgo_vp(self):
        """除 engine + VP kernel 外，禁止从 luxalgo_volume_profile_pytdx_15m_aligned 导入。"""
        violations: list[str] = []
        for path, rel in _iter_production_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            imported = _imported_names_from(tree, "luxalgo_volume_profile_pytdx_15m_aligned")
            bad = imported & _FORBIDDEN_FROM_LUXALGO
            if bad:
                violations.append(f"{rel}: 从 luxalgo_volume_profile_pytdx_15m_aligned 导入 {bad}")
        assert not violations, (
            "以下业务模块直接导入 luxalgo VP（应通过 node_cluster_engine）:\n"
            + "\n".join(violations)
        )

    def test_no_business_module_calls_compute_volume_profile_directly(self):
        """业务模块禁止直接调用 compute_volume_profile（应通过 engine.compute_single_period_volume_profile）。"""
        violations: list[str] = []
        for path, rel in _iter_production_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            if _has_call_to(tree, "compute_volume_profile"):
                violations.append(rel)
        assert not violations, (
            "以下业务模块直接调用 compute_volume_profile:\n" + "\n".join(violations)
        )

    def test_engine_is_only_allowed_importer_of_vp(self):
        """engine 模块本身必须导入底层 VP（确认唯一入口存在）。"""
        engine_path = _APP_DIR / _ENGINE_MODULE
        tree = ast.parse(engine_path.read_text(encoding="utf-8"))
        imported_uvp = _imported_names_from(tree, "unified_volume_profile")
        assert "compute_unified_volume_profile" in imported_uvp, (
            "node_cluster_engine.py 必须导入 compute_unified_volume_profile（作为唯一入口）"
        )


# =============================================================================
# 前端 AST 守护
# =============================================================================


def _iter_frontend_files() -> list[Path]:
    """枚举 frontend/src 下所有 .ts/.tsx 文件。

    排除 `pages/LandingPage/`：该目录是营销首页的装饰性动画，
    使用合成 candle 数据计算简单直方图 VP 用于视觉效果，
    非业务计算、非真实股票数据、不复制后端 LuxAlgo VP kernel。
    业务页面（StockDetailPage 等）仍受约束。
    """
    result: list[Path] = []
    if not _FRONTEND_SRC.exists():
        return result
    for p in sorted(_FRONTEND_SRC.rglob("*.ts*")):
        if "__pycache__" in str(p):
            continue
        if p.suffix not in {".ts", ".tsx"}:
            continue
        rel = p.relative_to(_FRONTEND_SRC).as_posix()
        if rel.startswith("pages/LandingPage/"):
            continue
        result.append(p)
    return result


# 前端禁止的 VP 计算函数名
_FORBIDDEN_FRONTEND_NAMES = {
    "computeVolumeProfile",
    "computeUnifiedVolumeProfile",
    "valueAreaHigh",
    "valueAreaLow",
}


class TestFrontendArchitecture:
    """5. 前端禁止 VP 计算 / VA 过滤。"""

    def test_frontend_no_vp_computation_or_va_filter(self):
        """前端禁止定义/调用 computeVolumeProfile / computeUnifiedVolumeProfile / valueArea 过滤。

        前端只负责展示后端返回的 profile DTO，不得自行计算 VP 或过滤 value area。
        允许：is_value_area / is_poc / is_peak 字段读取（DTO 字段，非计算）。
        """
        violations: list[str] = []
        for path in _iter_frontend_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                # 前端 TSX 可能无法用 Python ast 解析；用文本扫描兜底
                text = path.read_text(encoding="utf-8")
                for name in _FORBIDDEN_FRONTEND_NAMES:
                    if name in text:
                        violations.append(f"{path.relative_to(_REPO_ROOT)}: 包含 '{name}'")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in _FORBIDDEN_FRONTEND_NAMES:
                    violations.append(f"{path.relative_to(_REPO_ROOT)}: 定义函数 {node.name}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_FRONTEND_NAMES:
                    violations.append(f"{path.relative_to(_REPO_ROOT)}: 调用 {node.func.id}")
        assert not violations, (
            "前端禁止 VP 计算/VA 过滤（应展示后端 DTO）:\n" + "\n".join(violations)
        )
