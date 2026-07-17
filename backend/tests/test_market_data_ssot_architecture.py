"""市场数据 SSOT 架构守护测试（CHANGE-20260717-002）。

验证 MarketDataAggregationService (MDAS) 是行情读取唯一出口，复权只做一次：

1. 除 MDAS 和 repository 内部外，生产模块禁止从 bar_repository 导入私有
   _query_*、_get_adj_factor_df、apply_adj_factor_to_bars（旧复权封装）、旧 get_bars。
2. 除 AdjustmentFactorService 和 repository 内部外，生产模块禁止直接导入
   app.services.adj_factor 的 apply_adj_factor*（业务层应通过 MDAS/AdjustmentFactorService）。
3. 除 MDAS 外，生产模块禁止导入 kline_aggregator（周/月聚合出口唯一）。
4. 业务层禁止自行 resample 周/月聚合或调用 convert_kline_frequency 二次复权。

覆盖范围：indicator_service、strategy_batch、feature_snapshot、structural/temporal、
monitor、capture、chart_bars、bars/indicators API 及 app/ 全部生产模块。
"""
from __future__ import annotations

import ast
from pathlib import Path

_APP_DIR = Path(__file__).parent.parent / "app"

# 允许集（不受架构约束的模块，按相对 app/ 的 POSIX 路径）
_MDAS_MODULE = "services/market_data_aggregation_service.py"
_AFS_MODULE = "services/adjustment_factor_service.py"
_BAR_REPO_MODULE = "repositories/bar_repository.py"
_KLINE_AGG_MODULE = "services/kline_aggregator.py"
_ALLOWED_MODULES = {_MDAS_MODULE, _AFS_MODULE, _BAR_REPO_MODULE, _KLINE_AGG_MODULE}

# bar_repository 中禁止业务模块导入的名称
# （私有 _query_* 行情查询 / _get_adj_factor_df / 旧 apply_adj_factor_to_bars 复权封装 / 旧 get_bars）
_FORBIDDEN_FROM_BAR_REPO = {
    "_query_15min_bars",
    "_query_60min_bars",
    "_query_minute_bars",
    "_query_daily_bars",
    "_get_adj_factor_df",
    "apply_adj_factor_to_bars",
    "get_bars",
}

# app.services.adj_factor 中禁止业务模块直接导入的名称（纯计算模块，应由 AdjustmentFactorService 包装）
_FORBIDDEN_FROM_ADJ_FACTOR = {
    "apply_adj_factor",
    "apply_adj_factor_intraday",
    "apply_adj_factor_with_as_of",
    "_apply_adj_factor_core",
}

# 周/月聚合频率字符串（禁止业务层自行 resample 到这些周期）
_WEEKLY_MONTHLY_FREQS = {"W", "W-MON", "W-SUN", "M", "MS", "ME", "1W", "1M"}


def _iter_production_files() -> list[tuple[Path, str]]:
    """枚举 app/ 下所有生产 .py 文件（排除允许集），返回 (path, rel) 列表。"""
    result: list[tuple[Path, str]] = []
    for p in sorted(_APP_DIR.rglob("*.py")):
        rel = p.relative_to(_APP_DIR).as_posix()
        if rel in _ALLOWED_MODULES:
            continue
        # 跳过 __pycache__
        if "__pycache__" in rel:
            continue
        result.append((p, rel))
    return result


def _imported_names(tree: ast.AST, module_name: str) -> set[str]:
    """返回从 `from module_name import ...` 导入的名称集合。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            for alias in node.names:
                names.add(alias.name)
    return names


def _has_import_from(tree: ast.AST, module_name: str) -> bool:
    """是否存在 `from module_name import ...` 或 `import module_name`。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    return True
    return False


def _resample_freqs(tree: ast.AST) -> set[str]:
    """提取所有 .resample(<freq>) 调用的频率字符串字面量。"""
    freqs: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "resample"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            freqs.add(node.args[0].value)
    return freqs


def test_no_business_module_imports_forbidden_from_bar_repository() -> None:
    """业务模块禁止从 bar_repository 导入私有 _query_*/_get_adj_factor_df/apply_adj_factor_to_bars/旧 get_bars。

    MDAS 是行情读取唯一出口；私有查询函数仅 MDAS 和 repository 内部可用。
    """
    violations: list[str] = []
    for p, rel in _iter_production_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        imported = _imported_names(tree, "app.repositories.bar_repository")
        forbidden = imported & _FORBIDDEN_FROM_BAR_REPO
        if forbidden:
            violations.append(f"{rel}: 导入禁止的 bar_repository 名称 {sorted(forbidden)}")
    assert not violations, (
        "违反 SSOT：业务模块直接导入 repository 私有行情/复权函数（应走 MDAS）:\n"
        + "\n".join(violations)
    )


def test_no_business_module_imports_adj_factor_directly() -> None:
    """业务模块禁止直接导入 app.services.adj_factor 的 apply_adj_factor*。

    复权计算应由 AdjustmentFactorService（经 MDAS 调用）统一应用一次，
    业务层不得自行二次复权。
    """
    violations: list[str] = []
    for p, rel in _iter_production_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        imported = _imported_names(tree, "app.services.adj_factor")
        forbidden = imported & _FORBIDDEN_FROM_ADJ_FACTOR
        if forbidden:
            violations.append(f"{rel}: 直接导入 adj_factor {sorted(forbidden)}")
    assert not violations, (
        "违反 SSOT：业务模块直接导入复权计算模块（应通过 AdjustmentFactorService/MDAS）:\n"
        + "\n".join(violations)
    )


def test_only_mdas_imports_kline_aggregator() -> None:
    """kline_aggregator 只能被 MDAS 导入（周/月聚合出口唯一）。

    周/月线必须由 MDAS "日线完成复权后再聚合"，业务层不得自行聚合。
    """
    violations: list[str] = []
    for p, rel in _iter_production_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        if _has_import_from(tree, "app.services.kline_aggregator"):
            violations.append(rel)
    assert not violations, (
        "违反 SSOT：非 MDAS 模块导入 kline_aggregator（周/月聚合出口应唯一为 MDAS）:\n"
        + "\n".join(violations)
    )


def test_no_business_module_resamples_weekly_monthly() -> None:
    """业务模块禁止自行 resample 到周/月频率（应由 MDAS 聚合）。

    MDAS 自身 resample 仅用于 1m→15min/60min 日内聚合（允许），
    周/月聚合走 kline_aggregator.convert_kline_frequency。

    例外：app/strategy_assets/algorithms/ 下的算法实现模块可在已从 MDAS 获取的
    bars 上自行 resample 计算算法内部特征（如 SMC 的前日/周/月高低点水平线 PDH/PDL），
    这不属于"行情读取出口聚合"，且 SMC 算法受硬约束不得重写。
    """
    violations: list[str] = []
    for p, rel in _iter_production_files():
        # 算法实现层在已获取的 bars 上自行计算特征（非行情出口聚合），不受此约束
        if rel.startswith("strategy_assets/algorithms/"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        freqs = _resample_freqs(tree)
        bad = freqs & _WEEKLY_MONTHLY_FREQS
        if bad:
            violations.append(f"{rel}: resample 周/月频率 {sorted(bad)}")
    assert not violations, (
        "违反 SSOT：业务模块自行 resample 周/月聚合（应走 MDAS）:\n" + "\n".join(violations)
    )


def test_mdas_is_sole_importer_of_private_queries() -> None:
    """正向守护：bar_repository 私有 _query_* 仅被 MDAS 导入（确认出口唯一）。"""
    mdas_path = _APP_DIR / _MDAS_MODULE
    mdas_tree = ast.parse(mdas_path.read_text(encoding="utf-8"))
    mdas_imported = _imported_names(mdas_tree, "app.repositories.bar_repository")
    private_queries = {
        "_query_15min_bars",
        "_query_60min_bars",
        "_query_minute_bars",
        "_query_daily_bars",
    }
    # MDAS 应导入这些私有查询（它是出口）
    assert private_queries.issubset(mdas_imported), (
        f"MDAS 应导入 bar_repository 私有查询 {private_queries}，实际 {mdas_imported}"
    )
    # 其他生产模块不得导入这些私有查询
    # （由 test_no_business_module_imports_forbidden_from_bar_repository 覆盖）
