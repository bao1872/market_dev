"""SMC TV↔项目 parity 测试。

验证项目 SMC 核心计算与 TradingView Pine 源码的输出一致性。

流程：
1. 读取 TV 导出的 CSV fixture（含 time/OHLC + Pine 事件布尔值）
2. 使用 CSV 中的 OHLC 作为输入，调用 compute_smc_indicators
3. 断言 time/OHLC/bar 数量逐项相等（浮点容差 1e-8）
4. 比较事件有序序列

约束：
- 禁止从 DB 重新取另一套 Bar（PROMPT.md 第一节）
- 不相等时写 INPUT_BAR_MISMATCH，不得调整算法迎合截图
- 没有 TV golden fixture 不得宣称"完全对齐"

状态：PINE_PARITY_PENDING
- 当前没有 TV 导出的 CSV fixture，所有测试自动 skip
- 不得宣称 parity 已完成或"完全对齐"
- 待用户提供 TV CSV 后才能进行输出级完全一致断言

Fixture 路径：backend/tests/fixtures/smc_pine/smc_tv_<symbol>_<tf>.csv
TV CSV 由 ref/smc_user_export.pine（派生导出副本）末尾隐藏 plot 导出。
注意：ref/smc_user_source.pine 是用户原创 Pine 真源（SHA256 0bd3d2ad，843 行，不可变），
      导出功能在派生文件 ref/smc_user_export.pine 中，不得修改真源。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "smc_pine"
FLOAT_TOL = 1e-8

# PINE_PARITY_PENDING — 没有 TV CSV fixture 时所有测试 skip，不得宣称 parity 完成
PINE_PARITY_PENDING = True

# TV CSV 导出的列名（与 ref/smc_user_export.pine 末尾 plot 一致）
TV_COLUMNS = [
    "time",
    "_exp_open", "_exp_high", "_exp_low", "_exp_close",
    "_exp_int_bull_bos", "_exp_int_bear_bos",
    "_exp_int_bull_choch", "_exp_int_bear_choch",
    "_exp_swing_bull_bos", "_exp_swing_bear_bos",
    "_exp_swing_bull_choch", "_exp_swing_bear_choch",
    "_exp_int_bull_ob", "_exp_int_bear_ob",
    "_exp_eqh", "_exp_eql",
    "_exp_swing_bias", "_exp_int_bias",
]


def _load_tv_csv(csv_path: Path) -> dict[str, list[Any]]:
    """读取 TV 导出的 CSV fixture。

    CSV 格式：第一行为列名，后续每行一个 bar。
    time 列为 Unix 时间戳（秒）或 ISO 日期字符串。
    OHLC 列为浮点数。
    事件列为 0/1 整数。
    bias 列为 1/-1/0 整数。

    Returns:
        dict: column_name -> list of values
    """
    data: dict[str, list[Any]] = {col: [] for col in TV_COLUMNS}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for col in TV_COLUMNS:
                raw = row.get(col, "")
                if col == "time":
                    # TV 导出 time 为 Unix 时间戳（秒），转为 ISO 字符串
                    try:
                        ts = int(float(raw))
                        # 转为 ISO 日期（日线）或 ISO datetime（日内）
                        from datetime import UTC, datetime
                        dt = datetime.fromtimestamp(ts, tz=UTC)
                        data[col].append(dt.strftime("%Y-%m-%d"))
                    except (ValueError, OSError):
                        data[col].append(raw)
                elif col.startswith("_exp_") and col.endswith(("_bos", "_choch", "_ob", "_eqh", "_eql")):
                    data[col].append(int(float(raw)))
                elif col.startswith("_exp_") and col.endswith("_bias"):
                    data[col].append(int(float(raw)))
                else:
                    data[col].append(float(raw))
    return data


def _extract_tv_events(tv_data: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """从 TV CSV 数据提取事件序列（按 bar 顺序）。

    Returns:
        list of {bar_index, type, scope, bias} — 每个 TV 事件
    """
    events: list[dict[str, Any]] = []
    n = len(tv_data["_exp_close"])
    event_cols = [
        ("_exp_int_bull_bos", "BOS", "internal", 1),
        ("_exp_int_bear_bos", "BOS", "internal", -1),
        ("_exp_int_bull_choch", "CHoCH", "internal", 1),
        ("_exp_int_bear_choch", "CHoCH", "internal", -1),
        ("_exp_swing_bull_bos", "BOS", "swing", 1),
        ("_exp_swing_bear_bos", "BOS", "swing", -1),
        ("_exp_swing_bull_choch", "CHoCH", "swing", 1),
        ("_exp_swing_bear_choch", "CHoCH", "swing", -1),
        ("_exp_eqh", "EQH", "equal", 1),
        ("_exp_eql", "EQL", "equal", -1),
    ]
    for i in range(n):
        for col, etype, scope, bias in event_cols:
            if tv_data[col][i] == 1:
                events.append({
                    "bar_index": i,
                    "type": etype,
                    "scope": scope,
                    "bias": bias,
                })
    return events


def _extract_project_events(smc_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从项目 SMC 输出提取事件序列（按 confirmed_index 排序）。

    Returns:
        list of {bar_index, type, scope, bias}
    """
    events: list[dict[str, Any]] = []
    for ev in smc_result.get("events", []):
        events.append({
            "bar_index": ev.get("confirmed_index", ev.get("confirmed", -1)),
            "type": ev.get("type", ""),
            "scope": "internal" if ev.get("internal", True) else "swing",
            "bias": ev.get("bias", 0),
        })
    # EQH/EQL
    for eq in smc_result.get("equal_highs_lows", []):
        events.append({
            "bar_index": eq.get("confirmed_index", eq.get("confirmed", -1)),
            "type": "EQH" if eq.get("type", "").upper() == "HIGH" else "EQL",
            "scope": "equal",
            "bias": 1 if eq.get("type", "").upper() == "HIGH" else -1,
        })
    events.sort(key=lambda e: e["bar_index"])
    return events


# ===== 测试用例 =====

@pytest.fixture
def tv_csv_path() -> Path | None:
    """查找可用的 TV CSV fixture。如果不存在返回 None（测试 skip）。"""
    # 查找 smc_tv_*.csv 格式的 fixture
    for p in FIXTURE_DIR.glob("smc_tv_*.csv"):
        return p
    return None


def test_tv_csv_bar_parity(tv_csv_path: Path | None) -> None:
    """断言 TV CSV 与项目 SMC 输入的 time/OHLC/bar 数量逐项相等。

    测试流程：
    1. 读取 TV CSV（含 time/OHLC + Pine 事件布尔值）
    2. 使用 CSV 中的 OHLC 作为输入，调用 compute_smc_indicators
    3. 断言输入 bar 数量、时间、OHLC 完全一致（浮点容差 1e-8）

    如果 bar 不一致，写 INPUT_BAR_MISMATCH 错误信息，
    不得调整算法迎合截图（PROMPT.md 第一节）。
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    n_bars = len(tv_data["_exp_close"])
    assert n_bars > 0, f"TV CSV 无数据: {tv_csv_path}"

    # 使用 CSV 中的 OHLC 作为 SMC 输入（禁止从 DB 取另一套 Bar）
    opens = tv_data["_exp_open"]
    highs = tv_data["_exp_high"]
    lows = tv_data["_exp_low"]
    closes = tv_data["_exp_close"]
    times = tv_data["time"]

    # 调用项目 SMC 核心（与 indicator_service 相同的入口）
    from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators
    smc_result = compute_smc_indicators(
        opens=opens, highs=highs, lows=lows, closes=closes, times=times,
    )

    # 断言 bar 数量一致
    smc_times = smc_result.get("time", [])
    if len(smc_times) != n_bars:
        msg = (
            f"INPUT_BAR_MISMATCH: bar 数量不一致 "
            f"tv_bars={n_bars} project_bars={len(smc_times)} "
            f"csv={tv_csv_path.name}"
        )
        pytest.fail(msg)

    # 断言时间逐项相等
    for i, (tv_t, proj_t) in enumerate(zip(times, smc_times, strict=False)):
        if tv_t != proj_t:
            msg = (
                f"INPUT_BAR_MISMATCH: time[{i}] 不一致 "
                f"tv={tv_t} project={proj_t} "
                f"csv={tv_csv_path.name}"
            )
            pytest.fail(msg)

    # 断言 OHLC 逐项相等（浮点容差 1e-8）
    for i in range(n_bars):
        for col, proj_key in [
            ("_exp_open", "open"), ("_exp_high", "high"),
            ("_exp_low", "low"), ("_exp_close", "close"),
        ]:
            tv_val = tv_data[col][i]
            proj_val = smc_result.get(proj_key, [None] * n_bars)[i]
            if proj_val is None:
                continue
            if abs(tv_val - float(proj_val)) > FLOAT_TOL:
                msg = (
                    f"INPUT_BAR_MISMATCH: {proj_key}[{i}] 不一致 "
                    f"tv={tv_val} project={proj_val} "
                    f"diff={abs(tv_val - float(proj_val))} "
                    f"csv={tv_csv_path.name}"
                )
                pytest.fail(msg)


def test_tv_csv_event_parity(tv_csv_path: Path | None) -> None:
    """比较 TV CSV 与项目 SMC 的事件有序序列。

    测试流程：
    1. 读取 TV CSV 中的事件布尔值，提取 TV 事件序列
    2. 使用 CSV 中的 OHLC 作为输入，调用 compute_smc_indicators
    3. 提取项目事件序列
    4. 比较两个序列（类型、方向、bar 位置）

    事件容差：bar_index 允许 ±1 偏差（pivot 确认时机可能因
    bar_index 定义差异偏移 1 根）。类型和方向必须完全匹配。
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    n_bars = len(tv_data["_exp_close"])
    assert n_bars > 0

    opens = tv_data["_exp_open"]
    highs = tv_data["_exp_high"]
    lows = tv_data["_exp_low"]
    closes = tv_data["_exp_close"]
    times = tv_data["time"]

    from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators
    smc_result = compute_smc_indicators(
        opens=opens, highs=highs, lows=lows, closes=closes, times=times,
    )

    tv_events = _extract_tv_events(tv_data)
    proj_events = _extract_project_events(smc_result)

    # 比较事件序列长度
    if len(tv_events) != len(proj_events):
        msg = (
            f"事件数量不一致: tv={len(tv_events)} project={len(proj_events)}\n"
            f"TV events: {[(e['type'], e['scope'], e['bias'], e['bar_index']) for e in tv_events[:20]]}\n"
            f"Project events: {[(e['type'], e['scope'], e['bias'], e['bar_index']) for e in proj_events[:20]]}"
        )
        pytest.fail(msg)

    # 逐事件比较（允许 bar_index ±1 偏差）
    for i, (tv_ev, proj_ev) in enumerate(zip(tv_events, proj_events, strict=False)):
        if tv_ev["type"] != proj_ev["type"]:
            pytest.fail(
                f"事件[{i}] 类型不一致: tv={tv_ev['type']} project={proj_ev['type']}\n"
                f"  tv={tv_ev} project={proj_ev}"
            )
        if tv_ev["bias"] != proj_ev["bias"]:
            pytest.fail(
                f"事件[{i}] 方向不一致: tv_bias={tv_ev['bias']} project_bias={proj_ev['bias']}\n"
                f"  tv={tv_ev} project={proj_ev}"
            )
        if abs(tv_ev["bar_index"] - proj_ev["bar_index"]) > 1:
            pytest.fail(
                f"事件[{i}] bar_index 偏差>1: tv={tv_ev['bar_index']} project={proj_ev['bar_index']}\n"
                f"  tv={tv_ev} project={proj_ev}"
            )


def test_tv_csv_swing_bias_parity(tv_csv_path: Path | None) -> None:
    """比较 TV CSV 与项目 SMC 的 swing_bias（最后一根 bar）。

    swing_bias 必须直接返回 state.swing_trend.bias（1/-1/0），
    前端禁止从可见事件猜测。
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)

    opens = tv_data["_exp_open"]
    highs = tv_data["_exp_high"]
    lows = tv_data["_exp_low"]
    closes = tv_data["_exp_close"]
    times = tv_data["time"]

    from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators
    smc_result = compute_smc_indicators(
        opens=opens, highs=highs, lows=lows, closes=closes, times=times,
    )

    # TV 最后一根 bar 的 swing_bias
    tv_swing_bias = tv_data["_exp_swing_bias"][-1]
    # 项目输出的 swing_bias
    proj_swing_bias = smc_result.get("swing_bias", 0)

    assert tv_swing_bias == proj_swing_bias, (
        f"swing_bias 不一致: tv={tv_swing_bias} project={proj_swing_bias}"
    )
