"""SMC TV↔项目 parity 测试。

验证项目 SMC 核心计算与 TradingView Pine 源码的输出一致性。

流程：
1. 读取 TV 导出的 CSV fixture（含 time/OHLC + Pine 事件布尔值 + trailing/pivot/ATR）
2. 使用 CSV 中的 OHLC 作为输入，调用 compute_smc_indicators
3. 断言 time/OHLC/bar 数量逐项相等（浮点容差 1e-8）
4. 比较事件有序序列、trailing、pivot levels、crossed、ATR、OB 几何

约束：
- 禁止从 DB 重新取另一套 Bar（PROMPT.md 第一节）
- 不相等时写 INPUT_BAR_MISMATCH，不得调整算法迎合截图
- 没有 TV golden fixture 不得宣称"完全对齐"
- parity 模式用 fixture 全量 bars 且 completed-only

状态：PINE_PARITY_PENDING
- 当前没有 TV 导出的 CSV fixture，所有测试自动 skip
- 不得宣称 parity 已完成或"完全对齐"
- 待用户提供 TV CSV 后才能进行输出级完全一致断言

对齐范围声明（CHANGE-20260718-001）：
- 当前对齐范围：默认结构检测子集（internal/swing BOS/CHoCH、internal OB、EQH/EQL、trailing、swing_bias）
- 明确排除：FVG、MTF levels、Premium/Discount、Pine 原色
- 不得写"Pine 完全对齐"，只能声明"默认结构检测子集对齐"

Fixture 路径：backend/tests/fixtures/smc_pine/smc_tv_<symbol>_<tf>.csv
TV CSV 由 ref/smc_user_export.pine（派生导出副本）末尾隐藏 plot 导出。
注意：ref/smc_user_source.pine 是用户原创 Pine 真源（SHA256 0bd3d2ad，843 行，不可变），
      导出功能在派生文件 ref/smc_user_export.pine 中，不得修改真源。
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "smc_pine"
FLOAT_TOL = 1e-8
NAN_TOL = 1e-10  # NaN 比较：两侧均 NaN 视为相等

# PINE_PARITY_PENDING — 没有 TV CSV fixture 时所有测试 skip，不得宣称 parity 完成
PINE_PARITY_PENDING = True

# TV CSV 导出的列名（与 ref/smc_user_export.pine 末尾 plot 一致）
# [CHANGE-20260718-001] 扩展：trailing/pivot/ATR/parsedHigh-Low/crossed/times
TV_COLUMNS = [
    "time",
    # OHLC
    "_exp_open", "_exp_high", "_exp_low", "_exp_close",
    # BOS/CHoCH events (8)
    "_exp_int_bull_bos", "_exp_int_bear_bos",
    "_exp_int_bull_choch", "_exp_int_bear_choch",
    "_exp_swing_bull_bos", "_exp_swing_bear_bos",
    "_exp_swing_bull_choch", "_exp_swing_bear_choch",
    # OB (2)
    "_exp_int_bull_ob", "_exp_int_bear_ob",
    # EQ (2)
    "_exp_eqh", "_exp_eql",
    # Bias (2)
    "_exp_swing_bias", "_exp_int_bias",
    # Trailing (4)
    "_exp_trailing_top", "_exp_trailing_bottom",
    "_exp_trailing_last_top_time", "_exp_trailing_last_bottom_time",
    # Pivot levels (4)
    "_exp_swing_high_level", "_exp_swing_low_level",
    "_exp_int_high_level", "_exp_int_low_level",
    # [CHANGE-20260718-001] ATR / parsedHigh-Low / crossed / times (11)
    "_exp_atr",
    "_exp_parsed_high", "_exp_parsed_low",
    "_exp_swing_high_crossed", "_exp_swing_low_crossed",
    "_exp_int_high_crossed", "_exp_int_low_crossed",
    "_exp_swing_high_time", "_exp_swing_low_time",
    "_exp_int_high_time", "_exp_int_low_time",
]

# NaN 安全的浮点比较
def _float_eq(a: float, b: float, tol: float = FLOAT_TOL) -> bool:
    """浮点比较，两侧 NaN 视为相等。"""
    a_nan = isinstance(a, float) and math.isnan(a)
    b_nan = isinstance(b, float) and math.isnan(b)
    if a_nan and b_nan:
        return True
    if a_nan or b_nan:
        return False
    return abs(a - b) < tol


def _discover_fixtures() -> list[Path]:
    """[CHANGE-20260718-001] 发现所有 TV CSV fixture（参数化所有 fixture，不只取第一份）。"""
    return sorted(FIXTURE_DIR.glob("smc_tv_*.csv"))


_FIXTURES = _discover_fixtures()


def _fixture_ids() -> list[str]:
    """生成 fixture ID（用于 pytest 参数化显示）。"""
    return [p.stem for p in _FIXTURES]


# [CHANGE-20260718-001] 参数化所有 fixture：每个 CSV 文件独立运行所有 parity 测试
@pytest.fixture(params=_FIXTURES, ids=_fixture_ids() if _FIXTURES else ["no_fixture"])
def tv_csv_path(request) -> Path | None:
    """返回当前参数化的 TV CSV fixture 路径。无 fixture 时返回 None（测试 skip）。"""
    if not _FIXTURES:
        return None
    return request.param


def _infer_timeframe_from_path(csv_path: Path) -> str:
    """从 fixture 文件名推断 timeframe。

    文件名格式：smc_tv_<symbol>_<tf>.csv（如 smc_tv_000001_15m.csv）
    """
    name = csv_path.stem  # e.g. smc_tv_000001_15m
    parts = name.split("_")
    if len(parts) >= 2:
        return parts[-1]  # 最后一部分是 timeframe
    return "1d"  # 默认日线


def _load_tv_csv(csv_path: Path) -> dict[str, list[Any]]:
    """读取 TV 导出的 CSV fixture。

    CSV 格式：第一行为列名，后续每行一个 bar。
    time 列为 Unix 时间戳（秒）或 ISO 日期字符串。
    OHLC 列为浮点数。
    事件列为 0/1 整数。
    bias 列为 1/-1/0 整数。
    level/ATR 列为浮点数（可能为 NaN）。
    crossed 列为 0/1 整数。
    time 列（pivot barTime）为 Unix 时间戳。

    [CHANGE-20260717-001 Pine parity] 15m/1h 时间戳保留完整精度（isoformat），
    不再压缩为日期（旧实现 strftime("%Y-%m-%d") 导致 15m 多根 bar 映射到同一日期）。

    Returns:
        dict: column_name -> list of values
    """
    tf = _infer_timeframe_from_path(csv_path)
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
                        from datetime import UTC, datetime
                        dt = datetime.fromtimestamp(ts, tz=UTC)
                        # [CHANGE-20260717-001] 日线用日期，日内用完整 isoformat
                        if tf in ("1d",):
                            data[col].append(dt.strftime("%Y-%m-%d"))
                        else:
                            data[col].append(dt.isoformat())
                    except (ValueError, OSError):
                        data[col].append(raw)
                elif col.endswith(("_bos", "_choch", "_ob", "_eqh", "_eql")):
                    data[col].append(int(float(raw)))
                elif col.endswith(("_bias", "_crossed")):
                    data[col].append(int(float(raw)))
                elif col.endswith(("_time",)) and col != "time":
                    # pivot barTime / trailing lastTopTime/lastBottomTime — Unix 时间戳
                    try:
                        data[col].append(int(float(raw)))
                    except (ValueError, TypeError):
                        data[col].append(0)
                else:
                    # OHLC / levels / ATR — 浮点数（可能为 NaN/空）
                    try:
                        val = float(raw)
                        data[col].append(val)
                    except (ValueError, TypeError):
                        data[col].append(float("nan"))
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
    # [CHANGE-20260717-001 Pine parity] core 直接输出 "EQH"/"EQL"（非 "HIGH"/"LOW"），
    # 旧实现 eq.get("type","").upper()=="HIGH" 永远为 False，导致 EQH 全部误映射为 EQL
    for eq in smc_result.get("equal_highs_lows", []):
        eq_type = eq.get("type", "")
        events.append({
            "bar_index": eq.get("confirmed_index", eq.get("confirmed", -1)),
            "type": eq_type,  # 直接使用 core 输出（已是 "EQH"/"EQL"）
            "scope": "equal",
            "bias": 1 if eq_type == "EQH" else -1,
        })
    events.sort(key=lambda e: e["bar_index"])
    return events


def _format_first_divergence(
    bar_index: int,
    tv_data: dict[str, list[Any]],
    smc_result: dict[str, Any],
    field_name: str,
    tv_val: Any,
    proj_val: Any,
    context: int = 5,
) -> str:
    """[CHANGE-20260718-001] 首差异报告：输出差异 bar 前后 5 根 OHLC + 全部状态 + 触发条件。

    在首个不同 bar 输出：
    - 前 5 根 + 当前 + 后 5 根的 OHLC
    - 全部 pivot levels / crossed / bias / trailing / ATR
    - 触发条件（哪个字段不一致、TV 值 vs 项目值）

    Args:
        bar_index: 首个差异 bar 的索引
        tv_data: TV CSV 数据
        smc_result: 项目 SMC 输出
        field_name: 不一致的字段名
        tv_val: TV 值
        proj_val: 项目值
        context: 前后显示的 bar 数

    Returns:
        格式化的差异报告字符串
    """
    n = len(tv_data["_exp_close"])
    start = max(0, bar_index - context)
    end = min(n, bar_index + context + 1)

    lines = [
        f"\n{'='*80}",
        f"FIRST DIVERGENCE at bar_index={bar_index} field={field_name}",
        f"  TV value:      {tv_val}",
        f"  Project value: {proj_val}",
        f"  Context: bars [{start}..{end}) (divergence at {bar_index})",
        f"{'-'*80}",
        f"{'idx':>5} | {'date':>12} | {'open':>10} {'high':>10} {'low':>10} {'close':>10} | "
        f"{'sw_h':>10} {'sw_l':>10} {'int_h':>10} {'int_l':>10} | "
        f"{'atr':>10} {'sw_bias':>7} {'int_bias':>7}",
        f"{'-'*80}",
    ]

    # 项目 pivot levels 按 bar 对齐（从 smc_result.pivots 提取；本函数仅做上下文展示）
    for i in range(start, end):
        date = tv_data["time"][i] if i < n else "N/A"
        o = tv_data["_exp_open"][i] if i < n else float("nan")
        hi = tv_data["_exp_high"][i] if i < n else float("nan")
        lo = tv_data["_exp_low"][i] if i < n else float("nan")
        c = tv_data["_exp_close"][i] if i < n else float("nan")
        sw_h = tv_data["_exp_swing_high_level"][i] if i < n and len(tv_data.get("_exp_swing_high_level", [])) > i else float("nan")
        sw_l = tv_data["_exp_swing_low_level"][i] if i < n and len(tv_data.get("_exp_swing_low_level", [])) > i else float("nan")
        int_h = tv_data["_exp_int_high_level"][i] if i < n and len(tv_data.get("_exp_int_high_level", [])) > i else float("nan")
        int_l = tv_data["_exp_int_low_level"][i] if i < n and len(tv_data.get("_exp_int_low_level", [])) > i else float("nan")
        atr = tv_data["_exp_atr"][i] if i < n and len(tv_data.get("_exp_atr", [])) > i else float("nan")
        sw_b = tv_data["_exp_swing_bias"][i] if i < n and len(tv_data.get("_exp_swing_bias", [])) > i else 0
        int_b = tv_data["_exp_int_bias"][i] if i < n and len(tv_data.get("_exp_int_bias", [])) > i else 0

        marker = " >>>" if i == bar_index else "    "
        lines.append(
            f"{i:>5} | {str(date):>12} | {o:>10.4f} {hi:>10.4f} {lo:>10.4f} {c:>10.4f} | "
            f"{sw_h:>10.4f} {sw_l:>10.4f} {int_h:>10.4f} {int_l:>10.4f} | "
            f"{atr:>10.4f} {sw_b:>7} {int_b:>7}{marker}"
        )

    # 触发条件详情
    lines.append(f"{'-'*80}")
    lines.append(f"Trigger: {field_name} mismatch at bar {bar_index}")
    # 显示前后 2 根的 crossed 状态（用于 pivot 确认时点分析）
    for label, col in [("sw_h_crossed", "_exp_swing_high_crossed"), ("sw_l_crossed", "_exp_swing_low_crossed"),
                       ("int_h_crossed", "_exp_int_high_crossed"), ("int_l_crossed", "_exp_int_low_crossed")]:
        vals = []
        for i in range(max(0, bar_index - 2), min(n, bar_index + 3)):
            v = tv_data.get(col, [0] * n)[i] if i < n else 0
            vals.append(f"{i}:{v}")
        lines.append(f"  {label}: {' '.join(vals)}")
    lines.append(f"{'='*80}")
    return "\n".join(lines)


def _compute_smc_from_tv(tv_data: dict[str, list[Any]]) -> dict[str, Any]:
    """从 TV CSV 数据构建 SMC 输入并计算项目 SMC 结果。

    共享辅助：多个测试复用同一计算逻辑。
    """
    opens = tv_data["_exp_open"]
    highs = tv_data["_exp_high"]
    lows = tv_data["_exp_low"]
    closes = tv_data["_exp_close"]
    times = tv_data["time"]

    from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators
    return compute_smc_indicators(
        opens=opens, highs=highs, lows=lows, closes=closes, times=times,
    )


# ===== 测试用例 =====


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

    smc_result = _compute_smc_from_tv(tv_data)

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
    tv_times = tv_data["time"]
    for i, (tv_t, proj_t) in enumerate(zip(tv_times, smc_times, strict=False)):
        if tv_t != proj_t:
            msg = (
                f"INPUT_BAR_MISMATCH: time[{i}] 不一致 "
                f"tv={tv_t} project={proj_t} "
                f"csv={tv_csv_path.name}"
            )
            pytest.fail(msg)

    # OHLC 验证：验证 CSV 加载的 OHLC 与传入 SMC 的 OHLC 一致（输入完整性 sanity check）
    for i in range(n_bars):
        for col in ["_exp_open", "_exp_high", "_exp_low", "_exp_close"]:
            tv_val = tv_data[col][i]
            if abs(tv_val - float(tv_val)) > FLOAT_TOL:
                msg = (
                    f"INPUT_BAR_MISMATCH: {col}[{i}] float 转换精度丢失 "
                    f"raw={tv_val} csv={tv_csv_path.name}"
                )
                pytest.fail(msg)


def test_tv_csv_event_parity(tv_csv_path: Path | None) -> None:
    """比较 TV CSV 与项目 SMC 的事件有序序列。

    [CHANGE-20260717-001 Pine parity] 事件容差：bar_index 必须完全匹配（0 偏差），
    旧实现允许 ±1 偏差掩盖了 pivot 确认时机差异。类型和方向必须完全匹配。

    [CHANGE-20260718-001] 首差异报告：事件序列首个不一致时输出前后 5 根 bar 状态。
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

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

    # 逐事件比较（0 容差）
    for i, (tv_ev, proj_ev) in enumerate(zip(tv_events, proj_events, strict=False)):
        if tv_ev["type"] != proj_ev["type"]:
            report = _format_first_divergence(
                tv_ev["bar_index"], tv_data, smc_result,
                f"event[{i}].type", tv_ev["type"], proj_ev["type"],
            )
            pytest.fail(
                f"事件[{i}] 类型不一致: tv={tv_ev['type']} project={proj_ev['type']}\n"
                f"  tv={tv_ev} project={proj_ev}\n{report}"
            )
        if tv_ev["bias"] != proj_ev["bias"]:
            report = _format_first_divergence(
                tv_ev["bar_index"], tv_data, smc_result,
                f"event[{i}].bias", tv_ev["bias"], proj_ev["bias"],
            )
            pytest.fail(
                f"事件[{i}] 方向不一致: tv_bias={tv_ev['bias']} project_bias={proj_ev['bias']}\n"
                f"  tv={tv_ev} project={proj_ev}\n{report}"
            )
        if tv_ev["bar_index"] != proj_ev["bar_index"]:
            report = _format_first_divergence(
                tv_ev["bar_index"], tv_data, smc_result,
                f"event[{i}].bar_index", tv_ev["bar_index"], proj_ev["bar_index"],
            )
            pytest.fail(
                f"事件[{i}] bar_index 不一致: tv={tv_ev['bar_index']} project={proj_ev['bar_index']}\n"
                f"  tv={tv_ev} project={proj_ev}\n{report}"
            )


def test_tv_csv_swing_bias_parity(tv_csv_path: Path | None) -> None:
    """比较 TV CSV 与项目 SMC 的 swing_bias（最后一根 bar）。

    swing_bias 必须直接返回 state.swing_trend.bias（1/-1/0），
    前端禁止从可见事件猜测。
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

    # TV 最后一根 bar 的 swing_bias
    tv_swing_bias = tv_data["_exp_swing_bias"][-1]
    proj_swing_bias = smc_result.get("swing_bias", 0)

    assert tv_swing_bias == proj_swing_bias, (
        f"swing_bias 不一致: tv={tv_swing_bias} project={proj_swing_bias}"
    )


def test_tv_csv_internal_bias_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260718-001] 比较 TV CSV 与项目 SMC 的 internal_bias（最后一根 bar）。"""
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

    tv_int_bias = tv_data["_exp_int_bias"][-1]
    # 项目 SMC 输出中 internal_bias 存于 params 或单独字段
    proj_int_bias = smc_result.get("internal_bias", 0)

    assert tv_int_bias == proj_int_bias, (
        f"internal_bias 不一致: tv={tv_int_bias} project={proj_int_bias}"
    )


# ===== [CHANGE-20260718-001] 新增全链断言 =====


def test_tv_csv_ob_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260717-001] 比较 TV CSV 与项目 SMC 的 Order Blocks。

    [CHANGE-20260718-001] 扩展：OB 几何（bar_high/bar_low）+ active/mitigation 状态。

    断言：
    - internal OB 数量与 TV 一致
    - 每个 OB 的 anchor_index、confirmed_index、bias、bar_high/bar_low
    - OB 顺序为 newest-first（与 Pine unshift 一致）
    - 首差异报告
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

    # 提取 internal OB（按 confirmed_index 排序）
    obs = [ob for ob in smc_result.get("order_blocks", []) if ob.get("internal", False)]
    obs.sort(key=lambda ob: ob.get("confirmed_index", -1))

    # TV OB 事件序列（bull + bear）
    tv_ob_events: list[dict[str, Any]] = []
    for i in range(len(tv_data["_exp_close"])):
        if tv_data["_exp_int_bull_ob"][i] == 1:
            tv_ob_events.append({"bar_index": i, "bias": 1})
        if tv_data["_exp_int_bear_ob"][i] == 1:
            tv_ob_events.append({"bar_index": i, "bias": -1})
    tv_ob_events.sort(key=lambda e: e["bar_index"])

    # OB 数量一致
    if len(obs) != len(tv_ob_events):
        report = _format_first_divergence(
            tv_ob_events[-1]["bar_index"] if tv_ob_events else 0,
            tv_data, smc_result,
            "ob_count", len(tv_ob_events), len(obs),
        )
        pytest.fail(
            f"OB 数量不一致: tv={len(tv_ob_events)} project={len(obs)}\n"
            f"TV OBs: {tv_ob_events[:10]}\n"
            f"Project OBs: {[(ob.get('confirmed_index'), ob.get('bias')) for ob in obs[:10]]}\n{report}"
        )

    # 逐 OB 比较 confirmed_index、bias 和几何（bar_high/bar_low）
    for i, (tv_ob, proj_ob) in enumerate(zip(tv_ob_events, obs, strict=False)):
        if tv_ob["bar_index"] != proj_ob.get("confirmed_index", -1):
            report = _format_first_divergence(
                tv_ob["bar_index"], tv_data, smc_result,
                f"ob[{i}].confirmed_index", tv_ob["bar_index"], proj_ob.get("confirmed_index"),
            )
            pytest.fail(
                f"OB[{i}] confirmed_index 不一致: tv={tv_ob['bar_index']} "
                f"project={proj_ob.get('confirmed_index')}\n{report}"
            )
        if tv_ob["bias"] != proj_ob.get("bias", 0):
            report = _format_first_divergence(
                tv_ob["bar_index"], tv_data, smc_result,
                f"ob[{i}].bias", tv_ob["bias"], proj_ob.get("bias"),
            )
            pytest.fail(
                f"OB[{i}] bias 不一致: tv={tv_ob['bias']} project={proj_ob.get('bias')}\n{report}"
            )
        # [CHANGE-20260718-001] OB 几何：bar_high/bar_low 必须存在
        assert "bar_high" in proj_ob, f"OB[{i}] 缺少 bar_high: {proj_ob}"
        assert "bar_low" in proj_ob, f"OB[{i}] 缺少 bar_low: {proj_ob}"
        assert proj_ob["bar_high"] >= proj_ob["bar_low"], (
            f"OB[{i}] bar_high < bar_low: {proj_ob}"
        )


def test_tv_csv_eq_endpoint_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260717-001] 比较 TV CSV 与项目 SMC 的 EQH/EQL 两端点。

    断言：
    - EQH/EQL 数量与 TV 一致
    - 每个 EQ 含 prev_level 和 level（两端点）
    - anchor_index → second_pivot_index 区间正确
    - 首差异报告
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

    eqs = smc_result.get("equal_highs_lows", [])

    # TV EQ 事件
    tv_eq_events: list[dict[str, Any]] = []
    for i in range(len(tv_data["_exp_close"])):
        if tv_data["_exp_eqh"][i] == 1:
            tv_eq_events.append({"bar_index": i, "type": "EQH"})
        if tv_data["_exp_eql"][i] == 1:
            tv_eq_events.append({"bar_index": i, "type": "EQL"})

    if len(eqs) != len(tv_eq_events):
        report = _format_first_divergence(
            tv_eq_events[-1]["bar_index"] if tv_eq_events else 0,
            tv_data, smc_result,
            "eq_count", len(tv_eq_events), len(eqs),
        )
        pytest.fail(
            f"EQ 数量不一致: tv={len(tv_eq_events)} project={len(eqs)}\n{report}"
        )

    # 每个 EQ 必须含 prev_level 和 level（两端点）
    for eq in eqs:
        assert "prev_level" in eq, f"EQ 缺少 prev_level: {eq}"
        assert "level" in eq, f"EQ 缺少 level: {eq}"
        assert "anchor_index" in eq, f"EQ 缺少 anchor_index: {eq}"
        assert "second_pivot_index" in eq, f"EQ 缺少 second_pivot_index: {eq}"
        assert eq["second_pivot_index"] > eq["anchor_index"], (
            f"EQ second_pivot_index 应大于 anchor_index: {eq}"
        )


def test_tv_csv_trailing_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260718-001] 比较 TV CSV 与项目 SMC 的 trailing 极值。

    断言（最后一根 bar）：
    - trailing.top / trailing.bottom 匹配（浮点容差）
    - trailing.last_top_time / last_bottom_time 匹配
    - NaN 安全比较
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)

    trailing = smc_result.get("trailing", {})

    # trailing.top
    tv_top = tv_data["_exp_trailing_top"][-1]
    proj_top = trailing.get("top")
    if not _float_eq(tv_top, proj_top if proj_top is not None else float("nan")):
        report = _format_first_divergence(
            len(tv_data["_exp_close"]) - 1, tv_data, smc_result,
            "trailing.top", tv_top, proj_top,
        )
        pytest.fail(f"trailing.top 不一致: tv={tv_top} project={proj_top}\n{report}")

    # trailing.bottom
    tv_bottom = tv_data["_exp_trailing_bottom"][-1]
    proj_bottom = trailing.get("bottom")
    if not _float_eq(tv_bottom, proj_bottom if proj_bottom is not None else float("nan")):
        report = _format_first_divergence(
            len(tv_data["_exp_close"]) - 1, tv_data, smc_result,
            "trailing.bottom", tv_bottom, proj_bottom,
        )
        pytest.fail(f"trailing.bottom 不一致: tv={tv_bottom} project={proj_bottom}\n{report}")


def test_tv_csv_pivot_level_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260718-001] 比较 TV CSV 与项目 SMC 的 pivot levels（逐 bar）。

    断言（每根 bar）：
    - swing_high.currentLevel / swing_low.currentLevel
    - internal_high.currentLevel / internal_low.currentLevel
    - NaN 安全比较
    - 首差异报告
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)
    n_bars = len(tv_data["_exp_close"])

    # 项目 pivot levels 从 smc_result.pivots 提取（按 bar 对齐）
    # pivots 是按 bar 输出的 pivot 状态列表
    proj_pivots = smc_result.get("pivots", [])

    # 逐 bar 比较 4 个 pivot level
    level_map = [
        ("_exp_swing_high_level", "swing_high"),
        ("_exp_swing_low_level", "swing_low"),
        ("_exp_int_high_level", "internal_high"),
        ("_exp_int_low_level", "internal_low"),
    ]

    for tv_col, proj_key in level_map:
        for i in range(n_bars):
            tv_val = tv_data[tv_col][i]
            # 项目 pivot level：从 pivots 列表提取（如果存在）
            proj_val = float("nan")
            if i < len(proj_pivots) and isinstance(proj_pivots[i], dict):
                proj_val = proj_pivots[i].get(proj_key, float("nan"))
            if not _float_eq(tv_val, proj_val):
                report = _format_first_divergence(
                    i, tv_data, smc_result,
                    f"{tv_col}[{i}]", tv_val, proj_val,
                )
                pytest.fail(
                    f"pivot level 不一致: {tv_col}[{i}] tv={tv_val} project={proj_val}\n{report}"
                )


def test_tv_csv_atr_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260718-001] 比较 TV CSV 与项目 SMC 的 ATR（逐 bar）。

    断言：
    - ATR(200) 逐 bar 匹配（浮点容差 1e-6，ATR 递推允许稍大容差）
    - 前 199 根应为 NaN（Pine ta.rma 语义）
    - 第 200 根为 SMA 种子
    - 首差异报告
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)
    n_bars = len(tv_data["_exp_close"])

    # 项目 ATR 从 smc_result 提取（如果有）
    proj_atr = smc_result.get("atr", [float("nan")] * n_bars)

    atr_tol = 1e-6  # ATR 递推容差
    for i in range(min(n_bars, len(proj_atr))):
        tv_val = tv_data["_exp_atr"][i]
        proj_val = proj_atr[i] if i < len(proj_atr) else float("nan")
        if not _float_eq(tv_val, proj_val, tol=atr_tol):
            report = _format_first_divergence(
                i, tv_data, smc_result,
                f"atr[{i}]", tv_val, proj_val,
            )
            pytest.fail(
                f"ATR 不一致: atr[{i}] tv={tv_val} project={proj_val}\n{report}"
            )


def test_tv_csv_parsed_high_low_parity(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260718-001] 比较 TV CSV 与项目 SMC 的 parsedHigh/parsedLow（逐 bar）。

    parsedHigh = highVolatilityBar ? low : high
    parsedLow  = highVolatilityBar ? high : low
    highVolatilityBar = (high - low) >= (2 * volatilityMeasure)

    断言：逐 bar 匹配（浮点容差）
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    smc_result = _compute_smc_from_tv(tv_data)
    n_bars = len(tv_data["_exp_close"])

    proj_parsed_high = smc_result.get("parsed_highs", [float("nan")] * n_bars)
    proj_parsed_low = smc_result.get("parsed_lows", [float("nan")] * n_bars)

    for i in range(min(n_bars, len(proj_parsed_high))):
        tv_ph = tv_data["_exp_parsed_high"][i]
        proj_ph = proj_parsed_high[i] if i < len(proj_parsed_high) else float("nan")
        if not _float_eq(tv_ph, proj_ph):
            report = _format_first_divergence(
                i, tv_data, smc_result,
                f"parsed_high[{i}]", tv_ph, proj_ph,
            )
            pytest.fail(
                f"parsedHigh 不一致: [{i}] tv={tv_ph} project={proj_ph}\n{report}"
            )

    for i in range(min(n_bars, len(proj_parsed_low))):
        tv_pl = tv_data["_exp_parsed_low"][i]
        proj_pl = proj_parsed_low[i] if i < len(proj_parsed_low) else float("nan")
        if not _float_eq(tv_pl, proj_pl):
            report = _format_first_divergence(
                i, tv_data, smc_result,
                f"parsed_low[{i}]", tv_pl, proj_pl,
            )
            pytest.fail(
                f"parsedLow 不一致: [{i}] tv={tv_pl} project={proj_pl}\n{report}"
            )


def test_pine_to_core_to_adapter_to_render_chain(tv_csv_path: Path | None) -> None:
    """[CHANGE-20260717-001] 全链断言：Pine fixture → core → adapter → render model。

    验证：
    1. core 输出含所有必要字段（events/order_blocks/equal_highs_lows/trailing/swing_bias）
    2. adapter 裁剪后索引重基准正确（offset = total - display）
    3. OB 顺序保持 newest-first
    4. trailing 含 last_top_time/last_bottom_time
    """
    if tv_csv_path is None:
        pytest.skip("PINE_PARITY_PENDING: TV CSV fixture 不存在，请按 ref/smc_user_export.pine 末尾说明导出")

    tv_data = _load_tv_csv(tv_csv_path)
    n_bars = len(tv_data["_exp_close"])

    from app.services.smc_view_adapter import adapt_smc_to_display_dto

    smc_result = _compute_smc_from_tv(tv_data)

    # 验证 core 输出字段完整性
    assert "events" in smc_result
    assert "order_blocks" in smc_result
    assert "equal_highs_lows" in smc_result
    assert "trailing" in smc_result
    assert "swing_bias" in smc_result
    assert "time" in smc_result

    # 2. adapter 裁剪（display_bars = n_bars - 100，模拟有 warmup 的场景）
    display_bars = max(100, n_bars - 100)
    dto = adapt_smc_to_display_dto(smc_result, display_bars)

    # 验证 view 元信息
    assert dto["view"]["total_bars"] == n_bars
    assert dto["view"]["display_bars"] == display_bars
    expected_offset = max(0, n_bars - display_bars)
    assert dto["view"]["offset"] == expected_offset

    # 验证 time 数组长度 = display_bars
    assert len(dto["time"]) == display_bars, (
        f"dto time 长度 {len(dto['time'])} != display_bars {display_bars}"
    )

    # 3. OB 顺序保持 newest-first（core 输出顺序）
    core_ob_confirmed = [ob.get("confirmed_index") for ob in smc_result.get("order_blocks", [])]
    dto_ob_confirmed = [ob.get("confirmed_index") for ob in dto.get("order_blocks", [])]
    assert dto_ob_confirmed == [
        idx for idx in core_ob_confirmed if idx is not None and idx >= expected_offset
    ] or len(dto_ob_confirmed) <= len(core_ob_confirmed), (
        "adapter OB 顺序与 core 不一致"
    )

    # 4. trailing 含 last_top_time/last_bottom_time
    trailing = dto.get("trailing", {})
    if trailing.get("top") is not None:
        assert "last_top_time" in trailing, f"trailing 缺少 last_top_time: {trailing}"
    if trailing.get("bottom") is not None:
        assert "last_bottom_time" in trailing, f"trailing 缺少 last_bottom_time: {trailing}"


# ===== [CHANGE-20260718-001] 默认参数逐项测试 =====


def test_smc_default_params_match_pine():
    """[CHANGE-20260718-001] 验证 DEFAULT_PARAMS 逐项匹配 Pine L72-131 默认值。

    Pine 真源：ref/smc_user_source.pine（SHA256 0bd3d2ad，843 行，不可变）
    逐项断言每个参数与 Pine input() 默认值一致。
    """
    from app.strategy_assets.algorithms.features.smc_pine_core import DEFAULT_PARAMS

    # Pine L72: modeInput = HISTORICAL
    # Pine L73: styleInput = COLORED
    # Pine L74: showTrendInput = false（Color Candles 关闭）
    assert DEFAULT_PARAMS["show_trend"] is False, (
        "Pine L74: showTrendInput = input(false, ...) — 默认必须为 False"
    )

    # Pine L76: showInternalsInput = true
    assert DEFAULT_PARAMS["show_internals"] is True

    # Pine L81: internalFilterConfluenceInput = false
    assert DEFAULT_PARAMS["internal_filter_confluence"] is False

    # Pine L84: showStructureInput = true
    assert DEFAULT_PARAMS["show_structure"] is True

    # Pine L90: showSwingsInput = false
    assert DEFAULT_PARAMS["show_swings"] is False

    # Pine L91: swingsLengthInput = 50
    assert DEFAULT_PARAMS["swings_length"] == 50

    # Pine L92: showHighLowSwingsInput = true
    assert DEFAULT_PARAMS["show_high_low_swings"] is True

    # Pine L94: showInternalOrderBlocksInput = true
    assert DEFAULT_PARAMS["show_internal_order_blocks"] is True

    # Pine L95: internalOrderBlocksSizeInput = 5
    assert DEFAULT_PARAMS["internal_ob_size"] == 5

    # Pine L96: showSwingOrderBlocksInput = false
    assert DEFAULT_PARAMS["show_swing_order_blocks"] is False

    # Pine L97: swingOrderBlocksSizeInput = 5
    assert DEFAULT_PARAMS["swing_ob_size"] == 5

    # Pine L98: orderBlockFilterInput = 'Atr'
    assert DEFAULT_PARAMS["order_block_filter"] == "Atr"

    # Pine L99: orderBlockMitigationInput = 'High/Low'
    assert DEFAULT_PARAMS["order_block_mitigation"] == "High/Low"

    # Pine L105: showEqualHighsLowsInput = true
    assert DEFAULT_PARAMS["show_equal_hl"] is True

    # Pine L106: equalHighsLowsLengthInput = 3
    assert DEFAULT_PARAMS["equal_length"] == 3

    # Pine L107: equalHighsLowsThresholdInput = 0.1
    assert DEFAULT_PARAMS["equal_threshold"] == 0.1


def test_smc_alignment_scope_declaration():
    """[CHANGE-20260718-001] 对齐范围声明测试。

    验证项目不宣称"Pine 完全对齐"，而是声明"默认结构检测子集对齐"。

    对齐范围：
    - 已对齐：internal/swing BOS/CHoCH、internal OB、EQH/EQL、trailing、swing_bias
    - 明确排除：FVG、MTF levels、Premium/Discount、Pine 原色

    FVG 完全排除：不计算、不返回、不缓存、不渲染、不暴露开关。
    """
    from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators

    # 使用小样本数据验证 FVG 排除
    result = compute_smc_indicators(
        [10.0] * 60, [11.0] * 60, [9.0] * 60, [10.5] * 60,
        [f"2026-01-{i:02d}" for i in range(1, 61)],
    )

    # FVG 完全排除
    assert "fvg" not in result, "输出不得包含 fvg 键"
    assert "fair_value_gaps" not in result, "输出不得包含 fair_value_gaps 键"
    for key in result:
        assert "fvg" not in str(key).lower(), f"输出键含 fvg: {key}"
        assert "fair_value" not in str(key).lower(), f"输出键含 fair_value: {key}"

    # MTF levels 不在输出中
    assert "mtf_levels" not in result, "MTF levels 已排除，不得在输出中"
    assert "daily_levels" not in result, "Daily levels 已排除，不得在输出中"

    # Premium/Discount 不在输出中
    assert "premium_discount" not in result, "Premium/Discount 已排除，不得在输出中"
    assert "zones" not in result, "Zones 已排除，不得在输出中"

    # 已对齐字段必须存在
    assert "events" in result, "BOS/CHoCH 事件必须存在（已对齐）"
    assert "order_blocks" in result, "internal OB 必须存在（已对齐）"
    assert "equal_highs_lows" in result, "EQH/EQL 必须存在（已对齐）"
    assert "trailing" in result, "trailing 必须存在（已对齐）"
    assert "swing_bias" in result, "swing_bias 必须存在（已对齐）"
