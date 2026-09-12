"""[A2 P0] 权限越权面契约测试（纯单元，无 DB / 无网络）。

背景（PHASE 0 审计发现的两处真实越权面）：

1. `GET/POST /v1/instruments*` 四个端点此前**完全无鉴权**
   （只有 `Depends(get_db)`），任何未登录方可枚举全市场标的。
2. `GET /v1/boards*` 两个端点后端仅 `require_authenticated`，
   但前端 `/boards` 路由要求 `market_data` → 登录用户可绕过前端直读。

本测试以 **路由依赖内省 + 源码契约** 双重方式锁死这两点，
不依赖数据库，可在 PURE_UNIT_TEST 下运行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.api.board_analysis import board_router
from app.api.instruments import router as instruments_router
from app.models.user_capability import (
    CAPABILITY_MARKET_DATA,
    CAPABILITY_SELF_SELECTION,
)

pytestmark = pytest.mark.pure_unit

_BACKEND = Path(__file__).resolve().parents[1]
_INSTRUMENTS_SRC = (_BACKEND / "app" / "api" / "instruments.py").read_text(encoding="utf-8")
_BOARD_SRC = (_BACKEND / "app" / "api" / "board_analysis.py").read_text(encoding="utf-8")


def _closure_strings(dep: Any) -> set[str]:
    """收集某个依赖 callable 闭包中出现的字符串常量。"""
    found: set[str] = set()
    for cell in getattr(dep.call, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:  # pragma: no cover - 空 cell
            continue
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            found.update(str(v) for v in value if isinstance(v, str))
    return found


def _route_dependencies(router: Any) -> list[tuple[str, Any]]:
    return [(getattr(r, "path", ""), r) for r in router.routes if hasattr(r, "dependant")]


# ── 1. instruments 路由：必须存在 capability 门禁 ──────────────────────
def test_instruments_routes_require_capability_dependency() -> None:
    """每个 /v1/instruments 端点都必须带 capability 依赖（修复前为 0 个）。"""
    routes = _route_dependencies(instruments_router)
    assert routes, "instruments 路由不应为空"

    for path, route in routes:
        # 至少一个非 get_db 的业务依赖存在
        assert route.dependant.dependencies, f"{path} 缺少任何依赖（越权面）"


def test_instruments_dependency_gates_self_selection_or_market_data() -> None:
    """门禁必须放行 self_selection 或 market_data（与前端 /market 一致）。"""
    routes = _route_dependencies(instruments_router)
    for path, route in routes:
        names: set[str] = set()
        for dep in route.dependant.dependencies:
            names |= _closure_strings(dep)
        assert CAPABILITY_SELF_SELECTION in names or CAPABILITY_MARKET_DATA in names, (
            f"{path} 的依赖未包含 self_selection / market_data: {names}"
        )


# ── 2. board_analysis 路由：必须是 market_data，而非仅登录 ─────────────
def test_board_routes_require_market_data_capability() -> None:
    routes = _route_dependencies(board_router)
    assert routes, "board 路由不应为空"

    for path, route in routes:
        names: set[str] = set()
        for dep in route.dependant.dependencies:
            names |= _closure_strings(dep)
        assert CAPABILITY_MARKET_DATA in names, (
            f"{path} 未要求 market_data（修复前仅 require_authenticated）: {names}"
        )


# ── 3. 源码契约：不得回退到"仅登录" ────────────────────────────────────
def test_board_analysis_source_no_longer_uses_require_authenticated() -> None:
    # 允许模块 docstring 提及该名称（用于记录历史缺陷），
    # 但不得再作为依赖使用，也不得再被导入。
    assert "Depends(require_authenticated)" not in _BOARD_SRC, (
        "board_analysis 不得再以 require_authenticated 作为依赖（前端要求 market_data）"
    )
    assert "AccessContext, require_authenticated" not in _BOARD_SRC, (
        "board_analysis 不得再导入 require_authenticated"
    )


def test_instruments_source_declares_capability_gate() -> None:
    assert "_REQUIRE_INSTRUMENT_DISCOVERY = require_any_capability(" in _INSTRUMENTS_SRC
    assert "CAPABILITY_SELF_SELECTION" in _INSTRUMENTS_SRC
    assert "CAPABILITY_MARKET_DATA" in _INSTRUMENTS_SRC


def test_instruments_all_four_endpoints_carry_access_param() -> None:
    """4 个端点都必须挂上 _access 依赖（防止只改一半）。"""
    assert _INSTRUMENTS_SRC.count("_access: AccessContext = Depends(") == 4
