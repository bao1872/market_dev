"""展示帧（display_frame）构建器 - 展示窗口与算法输入帧分离。

背景（PROMPT.md §一、§二 V2）：
    前端 ChartRenderFrame 之前严格比较 bars.source_bar_hash 与
    indicators.source_bar_hash，但两者来源不同：
      - bars API 的 source_bar_hash 来自展示窗口（page_size 根）
      - indicators API 的 source_bar_hash 来自算法输入（Node 250 根日线）
    导致 1d 周期永久 mismatch，指标图层被屏蔽，页面持续显示"指标加载中"。

    V1 修复仍硬编码 _display_window=100，与 bars API 实际 page_size
    （1d=250/15m=4000/1h=1200/1w=260/1mo=120）不一致，导致 4/5 周期 mismatch。
    Capture 单 Snapshot 直接渲染，未参与帧比对门禁，能出图只是绕过详情页门禁。

V2 解决方案（PROMPT.md §二.1 DisplayWindowSpec V2）：
    抽出唯一 DisplayWindowSpec，bars/indicators/capture 必须基于同一 Spec 和
    同一最终展示 DataFrame 生成 frame。删除所有展示窗口 100 硬编码；
    indicators 按请求 bars 生成展示窗口。DisplayFrame 增加 requested_count、
    actual_count、first_time、last_time、include_realtime、is_partial、adjustment_as_of。
    Capture Snapshot 返回服务端校验后的 render_frame.matched；false 不得 Ready。

display_frame 字段：
    - instrument_id: 标的 UUID
    - timeframe: 1d | 15m | 1h | 1w | 1mo
    - adj: qfq | none
    - display_times: 展示窗口 bar 时间数组（首末用于范围 key）
    - display_hash: 展示窗口 OHLCV SHA256 前 16 字符
    - completed_through: 已完成到的时间（来自 MDAS 诊断）
    - requested_count: 请求的 bar 数量（Spec.requested_count）
    - actual_count: 实际返回的 bar 数量（len(display_df)）
    - first_time: 展示窗口首根 bar 时间
    - last_time: 展示窗口末根 bar 时间
    - include_realtime: 是否包含实时 bar
    - is_partial: 末根 bar 是否为 partial（未完成）
    - adjustment_as_of: 复权锚点（None=最新）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.chart_bars_service import compute_source_bar_hash, compute_source_bar_times


@dataclass(frozen=True)
class DisplayWindowSpec:
    """展示窗口规格（PROMPT.md §二.1 DisplayWindowSpec V2）。

    bars/indicators/capture 必须基于同一 Spec 和同一最终展示 DataFrame 生成 frame。
    任一字段不同即视为不同窗口，display_hash 必然不同，前端据此判定 mismatch。

    Attributes:
        instrument_id: 标的 UUID 字符串
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo
        adj: 复权方式 qfq | none
        requested_count: 请求的 bar 数量（bars API page_size / indicators API bars）
        end_time: 请求截止时间（end_date 或 end_time ISO 字符串），None 表示最新
        include_realtime: 是否包含实时 partial bar
        completed_only: 是否只返回已完成 bar（True 时强制 include_realtime=False）
        adjustment_as_of: 复权锚点 YYYY-MM-DD（None=最新；历史回算传业务日）
    """

    instrument_id: str
    timeframe: str
    adj: str
    requested_count: int
    end_time: str | None = None
    include_realtime: bool = False
    completed_only: bool = False
    adjustment_as_of: str | None = None

    def to_cache_suffix(self) -> str:
        """生成用于缓存键的紧凑后缀（保证不同 Spec 产生不同键）。

        格式：rt{0/1}co{0/1}ao{as_of_or_None}
        默认值（include_realtime=True, completed_only=False, adjustment_as_of=None）
        产生 "rt1co0aoNone"，与历史行为等价。
        """
        return (
            f"rt{1 if self.include_realtime else 0}"
            f"co{1 if self.completed_only else 0}"
            f"ao{self.adjustment_as_of or 'None'}"
        )


def build_display_frame(
    instrument_id: str,
    timeframe: str,
    adj: str,
    display_df: pd.DataFrame,
    completed_through: str | None = None,
    *,
    spec: DisplayWindowSpec | None = None,
    is_partial: bool | None = None,
) -> dict[str, Any]:
    """构建展示帧（display_frame）。

    bars API 与 indicators API 必须调用本函数生成 display_frame，保证同一展示
    窗口产生同一 display_hash。display_df 必须是真正交给前端绘制的 K线窗口
    （bars API 的 page_df，或 indicators API 的 macd_bars 末尾 N 根）。

    V2（PROMPT.md §二.1）：传入 spec 时附加 requested_count/actual_count/
    first_time/last_time/include_realtime/is_partial/adjustment_as_of 字段，
    供前端 mismatch 时显示两端 count/time/hash/as_of 差异和重试按钮。
    未传 spec 时保持 V1 行为（向后兼容，仅返回基础 6 字段）。

    [CH-01/CH-02 fix] PRD §3.3: strict hash 只比较 completed frame；
    partial bar 使用独立 live_revision，不触发 completed mismatch。
    当 is_partial=True 且 display_df 至少 2 根时，display_hash 和 display_times
    只基于 completed bars（排除末根 partial bar）。partial bar 信息放入
    live_revision 字段（独立于 completed frame，可随 realtime 更新）。
    这确保 bars API 和 indicators API 在不同时间调用 MDAS 时，即使 partial bar
    OHLCV 已更新，completed frame hash 仍一致，消除 display_frame 反复失败。

    Args:
        instrument_id: 标的 UUID 字符串
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo
        adj: 复权方式 qfq | none
        display_df: 展示窗口 DataFrame（index 为 DatetimeIndex，含 OHLCV 列）。
            空 DataFrame 时返回 display_hash="" 和 display_times=[]，不阻塞。
        completed_through: 已完成到的时间字符串（来自 MDAS completed_through 诊断），
            透传到 display_frame 供前端展示"数据截止"。
        spec: 展示窗口规格（V2）。传入时附加新字段；None 时保持 V1 行为。
        is_partial: 末根 bar 是否为 partial（未完成）。None 时不写入该字段。

    Returns:
        display_frame 字典（V2 含 spec 时附加新字段）
    """
    # [CH-01/CH-02 fix] 区分 completed frame 和 full display frame
    # 当 is_partial=True 时，display_hash/times 只基于 completed bars（排除末根 partial）
    # partial bar 信息放入 live_revision（独立字段，不参与 frame match）
    _has_partial = bool(is_partial) and not display_df.empty
    if _has_partial and len(display_df) > 1:
        completed_df = display_df.iloc[:-1]
    else:
        completed_df = display_df

    if display_df.empty:
        base: dict[str, Any] = {
            "instrument_id": str(instrument_id),
            "timeframe": timeframe,
            "adj": adj,
            "display_times": [],
            "display_hash": "",
            "completed_through": completed_through,
        }
    else:
        base = {
            "instrument_id": str(instrument_id),
            "timeframe": timeframe,
            "adj": adj,
            # [CH-01/CH-02 fix] display_hash/times 基于 completed frame（排除 partial）
            "display_times": compute_source_bar_times(completed_df, timeframe),
            "display_hash": compute_source_bar_hash(completed_df, timeframe),
            "completed_through": completed_through,
        }

    # [CH-01/CH-02 fix] live_revision: partial bar 独立字段（不参与 frame match）
    # 前端可读取此字段显示 realtime 价格，但不影响 display_hash 比较
    if _has_partial and not display_df.empty:
        last_bar = display_df.iloc[-1]
        last_ts = display_df.index[-1]
        try:
            base["live_revision"] = {
                "time": last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts),
                "open": float(last_bar["open"]),
                "high": float(last_bar["high"]),
                "low": float(last_bar["low"]),
                "close": float(last_bar["close"]),
                "volume": float(last_bar["volume"]),
            }
        except (KeyError, ValueError, TypeError):
            # OHLCV 列缺失或类型异常时跳过 live_revision（不阻塞 display_frame）
            pass

    # V2: 传入 spec 时附加新字段
    if spec is not None:
        base["requested_count"] = spec.requested_count
        base["actual_count"] = len(display_df)
        # [CH-01/CH-02 fix] completed_count: completed bars 数量（排除 partial）
        base["completed_count"] = len(completed_df)
        if not display_df.empty:
            first_ts = display_df.index[0]
            last_ts = display_df.index[-1]
            base["first_time"] = (
                first_ts.isoformat() if hasattr(first_ts, "isoformat") else str(first_ts)
            )
            base["last_time"] = (
                last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts)
            )
        else:
            base["first_time"] = None
            base["last_time"] = None
        base["include_realtime"] = spec.include_realtime
        if is_partial is not None:
            base["is_partial"] = bool(is_partial)
        base["adjustment_as_of"] = spec.adjustment_as_of

    return base


def is_display_frame_match(
    bars_frame: dict[str, Any] | None,
    indicators_frame: dict[str, Any] | None,
) -> bool:
    """判断 bars 与 indicators 的 display_frame 是否匹配（V2 Capture render_frame.matched）。

    比对规则：
        - 任一为 None：mismatch（保护性拒绝）
        - instrument_id / timeframe / adj：必须完全一致
        - display_hash：双侧非空时必须一致；双侧都空视为匹配（均为空数据）
        - display_range_key（display_times 首末）：双侧非空时必须一致

    Args:
        bars_frame: bars API 的 display_frame 字典
        indicators_frame: indicators API 的 display_frame 字典

    Returns:
        True 表示匹配；False 表示 mismatch
    """
    if bars_frame is None or indicators_frame is None:
        return False
    # 严格字段
    for key in ("instrument_id", "timeframe", "adj"):
        if bars_frame.get(key) != indicators_frame.get(key):
            return False
    bars_hash = bars_frame.get("display_hash") or ""
    ind_hash = indicators_frame.get("display_hash") or ""
    # 双侧空 hash 视为匹配（均为空数据）
    if not bars_hash and not ind_hash:
        return True
    # 任一非空时必须一致
    if bars_hash != ind_hash:
        return False
    # display_times 首末比对
    bars_times = bars_frame.get("display_times") or []
    ind_times = indicators_frame.get("display_times") or []
    if bars_times and ind_times:
        if bars_times[0] != ind_times[0] or bars_times[-1] != ind_times[-1]:
            return False
    return True


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

    # V2 自测：DisplayWindowSpec 附加新字段
    spec = DisplayWindowSpec(
        instrument_id="id-1",
        timeframe="1d",
        adj="qfq",
        requested_count=250,
        include_realtime=True,
        completed_only=False,
        adjustment_as_of=None,
    )
    frame_v2 = build_display_frame(
        "id-1", "1d", "qfq", df, completed_through="2026-07-02",
        spec=spec, is_partial=False,
    )
    assert frame_v2["requested_count"] == 250
    assert frame_v2["actual_count"] == 2
    assert frame_v2["first_time"] == "2026-07-01T00:00:00"
    assert frame_v2["last_time"] == "2026-07-02T00:00:00"
    assert frame_v2["include_realtime"] is True
    assert frame_v2["is_partial"] is False
    assert frame_v2["adjustment_as_of"] is None
    print(f"DisplayWindowSpec V2 附加字段: PASS (suffix={spec.to_cache_suffix()})")

    # V2 自测：spec 缺失时保持 V1 行为（无新字段）
    frame_v1 = build_display_frame("id-1", "1d", "qfq", df, completed_through="2026-07-02")
    assert "requested_count" not in frame_v1
    assert "actual_count" not in frame_v1
    print("V1 向后兼容: PASS")

    # V2 自测：is_display_frame_match
    bars_frame = build_display_frame(
        "id-1", "1d", "qfq", df, completed_through="2026-07-02", spec=spec,
    )
    ind_frame_match = build_display_frame(
        "id-1", "1d", "qfq", df, completed_through="2026-07-02", spec=spec,
    )
    assert is_display_frame_match(bars_frame, ind_frame_match) is True
    # 不同 display_df → 不同 hash → mismatch
    df2 = df.copy()
    df2.loc["2026-07-02", "close"] = 99.9
    ind_frame_mismatch = build_display_frame(
        "id-1", "1d", "qfq", df2, completed_through="2026-07-02", spec=spec,
    )
    assert is_display_frame_match(bars_frame, ind_frame_mismatch) is False
    print("is_display_frame_match: PASS")

    # =================================================================
    # [CH-01/CH-02 fix] partial bar → completed frame hash 稳定性测试
    # =================================================================
    # 场景：交易时段内，bars API 和 indicators API 各自调用 MDAS，
    # 末根 partial bar 的 OHLCV 可能在两次调用间更新，导致 display_hash 不一致。
    # 修复后：display_hash 只基于 completed bars（排除末根 partial），
    # partial bar 信息放入独立 live_revision 字段。

    # 三根 bar：前两根 completed，第三根 partial（OHLCV 随 realtime 变化）
    df_partial_v1 = pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.2, 11.1, 12.3],
            "volume": [1000, 2000, 1500],
            "amount": [10200.0, 22200.0, 18450.0],
        },
        index=pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]),
    )
    # 同一交易日，partial bar OHLCV 更新（close 从 12.3 → 12.8）
    df_partial_v2 = df_partial_v1.copy()
    df_partial_v2.loc["2026-07-03", "close"] = 12.8
    df_partial_v2.loc["2026-07-03", "high"] = 13.0
    df_partial_v2.loc["2026-07-03", "volume"] = 1800

    # 两次调用都标记 is_partial=True（同一交易日 partial bar）
    frame_partial_v1 = build_display_frame(
        "id-1", "1d", "qfq", df_partial_v1, completed_through="2026-07-02",
        spec=spec, is_partial=True,
    )
    frame_partial_v2 = build_display_frame(
        "id-1", "1d", "qfq", df_partial_v2, completed_through="2026-07-02",
        spec=spec, is_partial=True,
    )
    # [CH-01 fix] 核心：completed frame hash 必须一致（partial bar 不参与 hash）
    assert frame_partial_v1["display_hash"] == frame_partial_v2["display_hash"], (
        "CH-01 fix: partial bar 更新后 completed frame hash 必须保持稳定"
    )
    # [CH-01 fix] display_times 也只基于 completed bars（2 根，不含 partial）
    assert len(frame_partial_v1["display_times"]) == 2
    assert frame_partial_v1["display_times"][0] == "2026-07-01"
    assert frame_partial_v1["display_times"][1] == "2026-07-02"
    print(f"CH-01 partial bar 排除: PASS (hash={frame_partial_v1['display_hash']})")

    # [CH-02 fix] live_revision 字段：partial bar OHLCV 独立存放
    assert "live_revision" in frame_partial_v1
    assert frame_partial_v1["live_revision"]["time"] == "2026-07-03T00:00:00"
    assert frame_partial_v1["live_revision"]["close"] == 12.3
    assert frame_partial_v2["live_revision"]["close"] == 12.8
    # live_revision 不影响 display_hash
    assert frame_partial_v1["live_revision"] != frame_partial_v2["live_revision"]
    print("CH-02 live_revision 独立字段: PASS")

    # [CH-01/CH-02 fix] completed_count 字段：V2 spec 时附加
    assert frame_partial_v1["completed_count"] == 2  # 排除 partial
    assert frame_partial_v1["actual_count"] == 3  # 含 partial
    print("CH-01 completed_count 字段: PASS")

    # [CH-01 fix] is_partial=False 时无 live_revision，display_hash 基于完整 df
    frame_full = build_display_frame(
        "id-1", "1d", "qfq", df_partial_v1, completed_through="2026-07-03",
        spec=spec, is_partial=False,
    )
    assert "live_revision" not in frame_full
    assert len(frame_full["display_times"]) == 3  # 完整 3 根
    assert frame_full["completed_count"] == 3
    print("CH-01 is_partial=False 无 live_revision: PASS")

    # [CH-01 fix] is_partial=None（V1 行为）时无 live_revision
    frame_v1_partial = build_display_frame(
        "id-1", "1d", "qfq", df_partial_v1, completed_through="2026-07-03",
        spec=spec,  # 不传 is_partial
    )
    assert "live_revision" not in frame_v1_partial
    assert len(frame_v1_partial["display_times"]) == 3  # 完整 3 根
    print("CH-01 is_partial=None V1 行为: PASS")

    # [CH-01 fix] 单根 partial bar：len==1 时不能排除（completed_df = display_df）
    df_single_partial = df_partial_v1.iloc[-1:].copy()
    frame_single = build_display_frame(
        "id-1", "1d", "qfq", df_single_partial, completed_through="2026-07-02",
        spec=spec, is_partial=True,
    )
    # 单根时 completed_df = display_df（无法排除唯一 bar）
    assert len(frame_single["display_times"]) == 1
    assert frame_single["completed_count"] == 1
    # 但 live_revision 仍存在（partial bar 信息）
    assert "live_revision" in frame_single
    print("CH-01 单根 partial 不排除: PASS")

    # [CH-01 fix] is_display_frame_match：相同 completed bars + 不同 partial → 匹配
    assert is_display_frame_match(frame_partial_v1, frame_partial_v2) is True, (
        "CH-01 fix: 相同 completed bars + 不同 partial bar 必须匹配"
    )
    # [CH-01 fix] is_display_frame_match：不同 completed bars → 不匹配
    df_different_completed = df_partial_v1.copy()
    df_different_completed.loc["2026-07-02", "close"] = 99.9  # 修改 completed bar
    df_different_completed_v2 = pd.concat([
        df_different_completed.iloc[:-1],
        df_partial_v2.iloc[-1:],
    ])
    frame_diff_completed = build_display_frame(
        "id-1", "1d", "qfq", df_different_completed_v2, completed_through="2026-07-02",
        spec=spec, is_partial=True,
    )
    assert is_display_frame_match(frame_partial_v1, frame_diff_completed) is False, (
        "CH-01 fix: 不同 completed bars 必须不匹配"
    )
    print("CH-01 is_display_frame_match partial 稳定性: PASS")

    print("OK")
