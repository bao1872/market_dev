"""监控行情 PNG 渲染模块：K线+布林带+筹码峰色带+POC/VAH/VAL 标注。

从旧版 ref/交易/app/monitoring.py render_monitoring_chart() 迁移为独立模块。
使用 plotly 渲染，生成 PNG 文件供飞书图片消息推送。

用法：
    from app.services.monitor_chart_renderer import render_monitoring_chart
    png_path = await render_monitoring_chart(df, bb_mid, bb_upper, bb_lower, profile, symbol, stock_name)

模块自测：
    python -m app.services.monitor_chart_renderer
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    pass

logger = logging.getLogger("monitor_chart_renderer")


def _format_volume(vol: float) -> str:
    """格式化成交量：自动选择万/亿单位。"""
    if abs(vol) >= 1e8:
        return f"{vol / 1e8:.1f}亿"
    elif abs(vol) >= 1e4:
        return f"{vol / 1e4:.1f}万"
    else:
        return f"{vol:.0f}"


def _get_completed_bar_index(df: pd.DataFrame) -> int:
    """判断 DataFrame 中最后一根已完成 bar 的 iloc 索引（日线）。

    如果最后一根 bar 是今天且未收盘(15:00)，则取 iloc[-2]；
    否则取 iloc[-1]。

    Args:
        df: K线 DataFrame（index 为 datetime）

    Returns:
        最后一根已完成 bar 的 iloc 索引（-1 或 -2）
    """
    from datetime import datetime, time

    if df.empty:
        return -1

    now = datetime.now()
    last_ts = df.index[-1]

    if last_ts.date() == now.date():
        if last_ts.hour == 15 and last_ts.minute == 0:
            return -1
        elif now.time() < time(15, 5):
            return -2 if len(df) >= 2 else -1
    elif last_ts.date() < now.date():
        return -1

    return -1


def _load_bollinger_module() -> Any:
    """通过 importlib 从文件路径加载 bollinger features 模块。

    与 BollingerMonitor 使用相同的 importlib 方式加载，
    plotly 未安装时注入 mock。

    Returns:
        features 模块对象（含 bollinger 函数）

    Raises:
        FileNotFoundError: features 模块文件不存在
        ImportError: 模块加载失败
    """
    import importlib.util
    import sys
    import types

    from app.strategy._plotly_mock import ensure_plotly_mock

    features_dir = os.environ.get(
        "FEATURES_DIR", "/root/web_dev/ref/交易/features"
    )
    module_name = "bollinger_features_plotly"
    module_path = os.path.join(features_dir, f"{module_name}.py")

    if not os.path.exists(module_path):
        raise FileNotFoundError(
            f"bollinger features 模块不存在: {module_path}"
        )

    ensure_plotly_mock()

    if "datasource" not in sys.modules:
        try:
            import datasource  # noqa: F401
        except ImportError:
            datasource_mock = types.ModuleType("datasource")
            pytdx_client_mock = types.ModuleType("datasource.pytdx_client")
            pytdx_client_mock.connect_pytdx = lambda *a, **kw: None
            pytdx_client_mock.PERIOD_MAP = {}
            datasource_mock.pytdx_client = pytdx_client_mock
            sys.modules["datasource"] = datasource_mock
            sys.modules["datasource.pytdx_client"] = pytdx_client_mock

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建模块 spec: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise ImportError(
            f"bollinger features 模块加载失败: path={module_path}, error={e}"
        ) from e


async def render_monitoring_chart(
    df: pd.DataFrame,
    bb_mid: pd.Series,
    bb_upper: pd.Series,
    bb_lower: pd.Series,
    profile: Any,
    symbol: str,
    stock_name: str,
    bars_to_plot: int = 120,
) -> str | None:
    """渲染布林带+节点集群 K 线图并导出 PNG。

    生成单行子图：K线+布林带+节点集群色带+POC/VAH/VAL 水平线。
    仅绘制最近 bars_to_plot 根 bar，优化移动端可读性。

    Args:
        df: K线 DataFrame（index 为 datetime）
        bb_mid/bb_upper/bb_lower: 布林带序列（与 df 同长度）
        profile: VolumeProfileResult（可选，为 None 时不绘制节点集群）
        symbol: 股票代码
        stock_name: 股票名称
        bars_to_plot: 绘制最近多少根 bar

    Returns:
        PNG 文件路径，失败返回 None
    """
    try:
        import plotly.graph_objects as go
    except ImportError:
        logger.info("plotly 未安装，跳过图表渲染")
        return None

    if df.empty:
        return None

    # 截取绘制范围
    n_tail = min(bars_to_plot, len(df))
    df_plot = df.tail(n_tail).copy()
    bb_mid_plot = bb_mid.iloc[-n_tail:].values if len(bb_mid) >= n_tail else bb_mid.tail(n_tail).values
    bb_upper_plot = bb_upper.iloc[-n_tail:].values if len(bb_upper) >= n_tail else bb_upper.tail(n_tail).values
    bb_lower_plot = bb_lower.iloc[-n_tail:].values if len(bb_lower) >= n_tail else bb_lower.tail(n_tail).values

    if len(df_plot) < 5:
        return None

    x = np.arange(len(df_plot), dtype=float)
    tick_text = [ts.strftime("%m-%d") for ts in df_plot.index]

    title = f"日线 | {stock_name} {symbol} | 筹码峰已标注"

    fig = go.Figure()

    # 计算右侧 profile 区域锚点（用于筹码分布柱状图）
    profile_anchor = None
    if profile is not None and not profile.profile_df.empty:
        profile_width_bars = max(1.0, len(df_plot) * 0.31)
        offset = int(profile_width_bars) + 13
        last_x = len(df_plot) - 1
        profile_anchor = last_x + offset

    # K线
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=df_plot["open"], high=df_plot["high"],
            low=df_plot["low"], close=df_plot["close"],
            name="K线",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
        ),
    )

    # 筹码分布柱状图
    peak_data: list[dict[str, Any]] = []
    if profile_anchor is not None:
        max_vol = float(profile.profile_df["total_volume"].max()) or 1.0
        profile_width_bars = max(1.0, len(df_plot) * 0.31)
        for _, row in profile.profile_df.iterrows():
            total = float(row["total_volume"])
            bull = float(row["bullish_volume"])
            down = max(total - bull, 0.0)
            y0 = float(row["price_low"]) + 0.1 * profile.price_step
            y1 = float(row["price_low"]) + 0.9 * profile.price_step

            in_va = bool(row["is_value_area"])
            up_base = "rgba(41,98,255,0.70)" if in_va else "rgba(93,96,107,0.50)"
            down_base = "rgba(251,192,45,0.70)" if in_va else "rgba(209,212,220,0.50)"

            bull_w = bull / max_vol * profile_width_bars
            down_w = down / max_vol * profile_width_bars

            # bullish 部分（从锚点向左延伸）
            x1 = profile_anchor
            x0 = x1 - bull_w
            fig.add_shape(type="rect", xref="x", yref="y",
                          x0=x0, x1=x1, y0=y0, y1=y1,
                          line={"width": 0}, fillcolor=up_base, layer="above")
            # down 部分（向左接续）
            x1d = x0
            x0d = x1d - down_w
            fig.add_shape(type="rect", xref="x", yref="y",
                          x0=x0d, x1=x1d, y0=y0, y1=y1,
                          line={"width": 0}, fillcolor=down_base, layer="above")
            end_x = x0d

            # peak 节点高亮：叠加蓝色色带覆盖整个左半部分
            if bool(row["is_peak"]):
                fig.add_shape(type="rect", xref="x", yref="y",
                              x0=0, x1=end_x, y0=y0, y1=y1,
                              line={"width": 0}, fillcolor="rgba(33,150,243,0.50)",
                              layer="above")
                peak_data.append({
                    "y0": y0, "y1": y1, "end_x": end_x,
                    "bullish_volume": float(row["bullish_volume"]),
                    "bearish_volume": float(row["bearish_volume"]),
                })

    # 筹码峰迷你多空柱（在 peak 色带内部绘制绿色多头+红色空头水平柱）
    if profile_anchor is not None and peak_data:
        max_peak_vol = max(max(p["bullish_volume"], p["bearish_volume"]) for p in peak_data) or 1.0
        mini_max_w = profile_width_bars * 0.6
        bar_h_ratio = 0.4
        for pd_item in peak_data:
            y_range = pd_item["y1"] - pd_item["y0"]
            bar_y0 = pd_item["y0"] + y_range * (0.5 - bar_h_ratio / 2)
            bar_y1 = pd_item["y0"] + y_range * (0.5 + bar_h_ratio / 2)
            # 多头柱（绿色，从左端向右）
            bull_w = pd_item["bullish_volume"] / max_peak_vol * mini_max_w
            fig.add_shape(type="rect", xref="x", yref="y",
                          x0=0, x1=bull_w, y0=bar_y0, y1=bar_y1,
                          line={"width": 0}, fillcolor="rgba(38,166,154,0.85)",
                          layer="above")
            # 空头柱（红色，紧接多头柱右侧）
            bear_w = pd_item["bearish_volume"] / max_peak_vol * mini_max_w
            fig.add_shape(type="rect", xref="x", yref="y",
                          x0=bull_w, x1=bull_w + bear_w, y0=bar_y0, y1=bar_y1,
                          line={"width": 0}, fillcolor="rgba(239,83,80,0.85)",
                          layer="above")

    # 布林带填充区域
    fig.add_trace(
        go.Scatter(
            x=np.concatenate([x, x[::-1]]),
            y=np.concatenate([bb_upper_plot, bb_lower_plot[::-1]]),
            fill="toself",
            fillcolor="rgba(33,150,243,0.08)",
            line={"color": "rgba(0,0,0,0)"},
            mode="lines", showlegend=False, hoverinfo="skip",
            name="BB区域",
        ),
    )

    # 布林带上轨
    fig.add_trace(
        go.Scatter(x=x, y=bb_upper_plot, mode="lines",
                   line={"color": "#2196F3", "width": 1.2, "dash": "dash"},
                   name="BB上轨"),
    )

    # 布林带中轨
    fig.add_trace(
        go.Scatter(x=x, y=bb_mid_plot, mode="lines",
                   line={"color": "#FF9800", "width": 1.5},
                   name="BB中轨"),
    )

    # 布林带下轨
    fig.add_trace(
        go.Scatter(x=x, y=bb_lower_plot, mode="lines",
                   line={"color": "#2196F3", "width": 1.2, "dash": "dash"},
                   name="BB下轨"),
    )

    # 筹码峰价格标签 + 多空量标签（右侧标注）
    if profile_anchor is not None:
        peak_rows = profile.peak_df if profile.peak_df is not None else pd.DataFrame()
        label_x = profile_anchor + 1
        for _, row in peak_rows.iterrows():
            price_mid = float(row["price_mid"])
            # 价格标签
            fig.add_annotation(
                x=label_x, y=price_mid,
                text=f"峰 {price_mid:.2f}",
                showarrow=False, xanchor="left",
                font={"color": "#2196F3", "size": 11},
                bgcolor="rgba(19,23,34,0.85)",
            )
            # 多空量标签
            bull_vol = float(row["bullish_volume"])
            bear_vol = float(row["bearish_volume"])
            vol_text = f"多{_format_volume(bull_vol)} 空{_format_volume(bear_vol)}"
            fig.add_annotation(
                x=label_x, y=price_mid,
                text=vol_text,
                showarrow=False, xanchor="left",
                font={"color": "#d1d4dc", "size": 9},
                bgcolor="rgba(19,23,34,0.85)",
                yshift=-14,
            )

    # X轴范围：容纳 profile 柱状图
    if profile_anchor is not None:
        fig.update_xaxes(range=[-2, profile_anchor + 4])

    # 已完成bar竖线标记
    bar_idx = _get_completed_bar_index(df)
    if bar_idx == -2 and len(df_plot) >= 2:
        completed_x = x[-2]
        fig.add_vline(x=completed_x, line_width=1, line_dash="dash",
                      line_color="rgba(255,255,255,0.4)")

    # X轴标签
    step = max(1, len(df_plot) // 8)
    fig.update_xaxes(
        tickmode="array",
        tickvals=list(x[::step]),
        ticktext=tick_text[::step],
        rangeslider_visible=False,
    )

    fig.update_layout(
        template="plotly_dark",
        height=640,
        width=1200,
        hovermode="x unified",
        paper_bgcolor="#131722",
        plot_bgcolor="#131722",
        title={"text": title, "x": 0.01, "font": {"size": 14, "color": "#d1d4dc"}},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        margin={"l": 50, "r": 120, "t": 60, "b": 40},
    )

    # 导出PNG
    fd, out_png = tempfile.mkstemp(suffix=".png")
    os.close(fd)

    try:
        fig.write_image(out_png, width=1200, height=640, scale=2)
        return out_png
    except Exception as e:
        logger.warning("图表 PNG 导出失败: %s", e)
        # 清理临时文件
        try:
            os.unlink(out_png)
        except OSError:
            pass
        return None


if __name__ == "__main__":
    # 自测入口：验证 render_monitoring_chart 函数签名与基本逻辑（无副作用）
    # 1. 验证 _format_volume
    assert _format_volume(1.5e8) == "1.5亿"
    assert _format_volume(2.3e4) == "2.3万"
    assert _format_volume(500.0) == "500"
    print("_format_volume ✓")

    # 2. 验证 _get_completed_bar_index
    dates = pd.date_range("2026-06-20", periods=5, freq="1D")
    test_df = pd.DataFrame(
        {"open": [10, 11, 12, 13, 14], "high": [11, 12, 13, 14, 15],
         "low": [9, 10, 11, 12, 13], "close": [10.5, 11.5, 12.5, 13.5, 14.5]},
        index=dates,
    )
    idx = _get_completed_bar_index(test_df)
    assert idx in (-1, -2)
    print(f"_get_completed_bar_index: {idx} ✓")

    # 3. 验证 _load_bollinger_module 可调用
    try:
        module = _load_bollinger_module()
        assert hasattr(module, "bollinger")
        print(f"bollinger features 模块加载成功: {module.__name__} ✓")
    except FileNotFoundError as e:
        print(f"bollinger features 模块不可用（跳过）: {e}")

    # 4. 验证 render_monitoring_chart 函数签名（空 df 返回 None）
    import asyncio

    result = asyncio.run(render_monitoring_chart(
        df=pd.DataFrame(),
        bb_mid=pd.Series(dtype=float),
        bb_upper=pd.Series(dtype=float),
        bb_lower=pd.Series(dtype=float),
        profile=None,
        symbol="000001",
        stock_name="测试",
    ))
    assert result is None
    print("render_monitoring_chart(empty_df) → None ✓")

    print("OK")
