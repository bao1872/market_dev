"""展示帧（display_frame）构建器 - 展示窗口与算法输入帧分离。

背景（PROMPT.md §一、§二）：
    前端 ChartRenderFrame 之前严格比较 bars.source_bar_hash 与
    indicators.source_bar_hash，但两者来源不同：
      - bars API 的 source_bar_hash 来自展示窗口（默认 100 根）
      - indicators API 的 source_bar_hash 来自算法输入（Node 250 根日线）
    导致 1d 周期永久 mismatch，指标图层被屏蔽，页面持续显示"指标加载中"。

解决方案（PROMPT.md §二.1）：
    新增 display_frame：只描述真正交给前端绘制的 K线窗口。
    bars API 与 indicators API 调用同一个 build_display_frame() 生成它。
    算法输入 hash（source_bar_hash/daily_hash/15m_hash/profile_hash）
    移入 calculation_diagnostics，不参与展示帧匹配。

display_frame 字段：
    - instrument_id: 标的 UUID
    - timeframe: 1d | 15m | 1h | 1w | 1mo
    - adj: qfq | none
    - display_times: 展示窗口 bar 时间数组（首末用于范围 key）
    - display_hash: 展示窗口 OHLCV SHA256 前 16 字符
    - completed_through: 已完成到的时间（来自 MDAS 诊断）
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.chart_bars_service import compute_source_bar_hash, compute_source_bar_times


def build_display_frame(
    instrument_id: str,
    timeframe: str,
    adj: str,
    display_df: pd.DataFrame,
    completed_through: str | None = None,
) -> dict[str, Any]:
    """构建展示帧（display_frame）。

    bars API 与 indicators API 必须调用本函数生成 display_frame，保证同一展示
    窗口产生同一 display_hash。display_df 必须是真正交给前端绘制的 K线窗口
    （bars API 的 page_df，或 indicators API 的 macd_bars 末尾 N 根）。

    Args:
        instrument_id: 标的 UUID 字符串
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo
        adj: 复权方式 qfq | none
        display_df: 展示窗口 DataFrame（index 为 DatetimeIndex，含 OHLCV 列）。
            空 DataFrame 时返回 display_hash="" 和 display_times=[]，不阻塞。
        completed_through: 已完成到的时间字符串（来自 MDAS completed_through 诊断），
            透传到 display_frame 供前端展示"数据截止"。

    Returns:
        display_frame 字典：
            {
              "instrument_id": str,
              "timeframe": str,
              "adj": str,
              "display_times": list[str],
              "display_hash": str,
              "completed_through": str | None,
            }
    """
    if display_df.empty:
        return {
            "instrument_id": str(instrument_id),
            "timeframe": timeframe,
            "adj": adj,
            "display_times": [],
            "display_hash": "",
            "completed_through": completed_through,
        }
    return {
        "instrument_id": str(instrument_id),
        "timeframe": timeframe,
        "adj": adj,
        "display_times": compute_source_bar_times(display_df, timeframe),
        "display_hash": compute_source_bar_hash(display_df, timeframe),
        "completed_through": completed_through,
    }


def build_calculation_diagnostics(
    *,
    source_bar_hash: str | None = None,
    source_bar_times: list[str] | None = None,
    warmup_bars: int | None = None,
    calculation_window: int | None = None,
    smc_source_bar_hash: str | None = None,
    smc_source_bars: int | None = None,
    smc_source_first_time: str | None = None,
    smc_source_last_time: str | None = None,
    node_daily_hash: str | None = None,
    node_15m_hash: str | None = None,
    node_profile_hash: str | None = None,
    algorithm_version: str | None = None,
    contract_fingerprint: str | None = None,
    market_data_contract_version: str | None = None,
    adj_factor_hash: str | None = None,
    adjustment_as_of: str | None = None,
) -> dict[str, Any]:
    """构建算法输入诊断（calculation_diagnostics）。

    所有算法输入侧的 hash/版本/warmup 信息放在这里，**不参与展示帧匹配**。
    前端只读不阻塞。用于审计和排查算法输入与展示窗口的偏差。

    传入 None 的字段会被忽略（不出现在结果中），保持响应紧凑。
    """
    fields: dict[str, Any] = {
        "source_bar_hash": source_bar_hash,
        "source_bar_times": source_bar_times,
        "warmup_bars": warmup_bars,
        "calculation_window": calculation_window,
        "smc_source_bar_hash": smc_source_bar_hash,
        "smc_source_bars": smc_source_bars,
        "smc_source_first_time": smc_source_first_time,
        "smc_source_last_time": smc_source_last_time,
        "node_daily_hash": node_daily_hash,
        "node_15m_hash": node_15m_hash,
        "node_profile_hash": node_profile_hash,
        "algorithm_version": algorithm_version,
        "contract_fingerprint": contract_fingerprint,
        "market_data_contract_version": market_data_contract_version,
        "adj_factor_hash": adj_factor_hash,
        "adjustment_as_of": adjustment_as_of,
    }
    return {k: v for k, v in fields.items() if v is not None}


if __name__ == "__main__":
    # 自测：空 DataFrame
    empty_frame = build_display_frame("id-1", "1d", "qfq", pd.DataFrame())
    assert empty_frame["display_hash"] == ""
    assert empty_frame["display_times"] == []
    assert empty_frame["instrument_id"] == "id-1"
    print("空 DataFrame: PASS")

    # 自测：有数据
    df = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.2, 11.1],
            "volume": [1000, 2000],
            "amount": [10200.0, 22200.0],
        },
        index=pd.to_datetime(["2026-07-01", "2026-07-02"]),
    )
    frame = build_display_frame("id-1", "1d", "qfq", df, completed_through="2026-07-02")
    assert frame["display_hash"] != ""
    assert len(frame["display_times"]) == 2
    assert frame["display_times"][0] == "2026-07-01"
    assert frame["display_times"][1] == "2026-07-02"
    assert frame["completed_through"] == "2026-07-02"
    # 幂等：相同输入产生相同 hash
    frame2 = build_display_frame("id-1", "1d", "qfq", df, completed_through="2026-07-02")
    assert frame["display_hash"] == frame2["display_hash"]
    print(f"display_hash 幂等: PASS ({frame['display_hash']})")

    # 诊断
    diag = build_calculation_diagnostics(
        source_bar_hash="abc",
        node_profile_hash="def",
        warmup_bars=60,
    )
    assert "source_bar_hash" in diag
    assert "node_profile_hash" in diag
    assert "warmup_bars" in diag
    assert "adjustment_as_of" not in diag  # None 被过滤
    print("calculation_diagnostics 过滤 None: PASS")
    print("OK")
