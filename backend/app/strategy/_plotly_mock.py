"""plotly mock 注入工具（公共模块）。

features/ 算法模块顶层 `import plotly.graph_objects as go`（及 `plotly.subplots`）
仅用于可视化函数（绘图 HTML）。策略运行时（selector/monitor）仅调用计算函数，
不依赖 plotly。本模块在 plotly 未安装时注入轻量 mock 到 sys.modules，避免引入重依赖，
同时不修改 features/ 源码。

用法：
    from app.strategy._plotly_mock import ensure_plotly_mock
    ensure_plotly_mock()  # 在导入 features 模块前调用
"""

from __future__ import annotations

import logging
import sys
import types

logger = logging.getLogger("strategy._plotly_mock")


def ensure_plotly_mock() -> None:
    """若 plotly 未安装，注入轻量 mock 到 sys.modules（仅满足 features 顶层 import）。

    features/ 模块顶层 `import plotly.graph_objects as go` 及
    `from plotly.subplots import make_subplots` 仅用于可视化函数。
    策略运行时仅调用计算函数，不依赖 plotly。注入 mock 避免引入重依赖，
    同时不修改 features/ 源码。
    """
    if "plotly" in sys.modules:
        return
    try:
        import plotly  # noqa: F401
        return
    except ImportError:
        pass
    # 构造 plotly + plotly.graph_objects + plotly.subplots mock
    plotly_mock = types.ModuleType("plotly")
    go_mock = types.ModuleType("plotly.graph_objects")
    # 提供最小占位属性（可视化函数不会被策略运行时调用）
    go_mock.Figure = type("Figure", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Candlestick = type("Candlestick", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Bar = type("Bar", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Layout = type("Layout", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Scatter = type("Scatter", (), {"__init__": lambda self, *a, **kw: None})
    plotly_mock.graph_objects = go_mock
    # plotly.subplots.make_subplots 也被 features 顶层 import
    subplots_mock = types.ModuleType("plotly.subplots")
    subplots_mock.make_subplots = lambda *a, **kw: None
    sys.modules["plotly"] = plotly_mock
    sys.modules["plotly.graph_objects"] = go_mock
    sys.modules["plotly.subplots"] = subplots_mock
    logger.debug("已注入 plotly mock（features 可视化依赖，策略运行时不使用）")
