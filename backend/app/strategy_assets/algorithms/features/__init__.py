# cores - 核心算法模块
"""
核心算法库，包含所有可复用的计算逻辑（算法/公式/指标/转换/统计）。
遵循单一事实源（SSOT）原则，禁止重复实现。

包内导入前自动注入 datasource mock：
features 模块顶层有 `from datasource.pytdx_client import ...`，
策略运行时不使用该依赖（行情由 backend bar_repository 提供），
因此在包初始化时注入 mock，避免 ModuleNotFoundError。
"""
import sys
import types


def _ensure_datasource_mock() -> None:
    """注入 datasource mock 到 sys.modules（features 模块导入依赖）。

    features 模块顶层 import datasource.pytdx_client（仅数据获取用），
    策略运行时不使用，注入 mock 使包内导入不报错。
    """
    if "datasource" not in sys.modules:
        datasource_mock = types.ModuleType("datasource")
        pytdx_client_mock = types.ModuleType("datasource.pytdx_client")
        pytdx_client_mock.__dict__["connect_pytdx"] = lambda *a, **kw: None
        pytdx_client_mock.__dict__["PERIOD_MAP"] = {}
        datasource_mock.__dict__["pytdx_client"] = pytdx_client_mock
        sys.modules["datasource"] = datasource_mock
        sys.modules["datasource.pytdx_client"] = pytdx_client_mock


_ensure_datasource_mock()
