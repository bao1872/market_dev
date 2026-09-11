"""架构守卫：PytdxAdapter 必须是唯一 pytdx socket owner。

真实扫描 ``backend/app`` 生产树，禁止出现绕过 adapter 的直接 pytdx 访问：

    - ``.api.get_``
    - ``get_pytdx_adapter().api``
    - ``TdxHq_API(``

排除项（必须保留理由）：
    - ``core/pytdx_adapter.py``：adapter 自身；它是唯一被允许持有 socket 的模块。
    - ``strategy_assets/``：分析/可视化查看器资产（viewer / factor lab 脚本），
      不在 backend production runtime 链路上运行（无 API/worker 调用入口），
      其直接 TdxHq_API 属既有历史；本阶段（PytdxAdapter provider boundary 收口）
      范围为生产 pytdx 调用面，故显式排除并在文档中记录，后续单独收口。

用法：PURE_UNIT_TEST=1 pytest tests/test_pytdx_provider_boundary_guard.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.pure_unit

# 生产代码禁止出现的绕过 adapter 的 pytdx 直接访问
_FORBIDDEN = (
    ".api.get_",
    "get_pytdx_adapter().api",
    "TdxHq_API(",
)

# adapter 自身是唯一 socket owner
_ADAPTER_REL = Path("core/pytdx_adapter.py")
# 分析/可视化查看器资产，非生产运行链路（理由见模块 docstring）
_EXCLUDED_PREFIX = "strategy_assets/"


def test_pytdx_adapter_is_only_socket_owner() -> None:
    root = Path(__file__).parents[1] / "app"

    violations: list[str] = []

    for path in root.rglob("*.py"):
        rel = path.relative_to(root)

        if rel == _ADAPTER_REL:
            continue

        if str(rel).startswith(_EXCLUDED_PREFIX):
            continue

        text = path.read_text(encoding="utf-8")

        for token in _FORBIDDEN:
            if token in text:
                violations.append(f"{rel}: {token}")

    assert not violations, "\n".join(violations)


def test_guard_scans_real_production_tree() -> None:
    """守卫必须真实扫描到生产树（避免写成永不失败的空测试）。"""
    root = Path(__file__).parents[1] / "app"
    scanned = list(root.rglob("*.py"))
    assert len(scanned) > 100, f"扫描到的生产文件异常少：{len(scanned)}"
    assert (root / _ADAPTER_REL).exists(), "adapter 文件必须存在"
