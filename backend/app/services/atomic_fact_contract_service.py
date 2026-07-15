"""Atomic Fact Contract V1 - 纯函数服务（生产实现）。

从 structural_payload.primary.1d + temporal_payload.daily_context 只读计算
14 Core + 10 Auxiliary 原子事实。

设计约束（V4.13 冻结合同，硬性）：
- 生产实现不依赖实验脚本运行；仅以
  backend/app/contracts/atomic_fact_contract_v1.json 为 Canonical Registry
  （计数/顺序/阈值真源），计算公式在本模块以纯函数重写。
- T2/M2/M3 显示真实原始值，禁止伪造成 [-1,1] 固定范围。
- T5/V3 阈值 engineering_confirmation_required=true 且值为 null
  → 仅显示比值，标记「分类未启用」。
- M3 零值容差统一（1e-6，对应 sqzmom_delta_1 存储精度，非数据分位数）。
- S3 严格 0.33/0.67 边界（0.63 → 中间）。
- S7/S8 禁止显示负距离：d>=0「尚未到达 |d| ATR」；d<0「已越过 |d| ATR」。
- V1 累计成交量比永不进入用户 API/UI/摘要/可用性计数。
- T3/T6 效率 fact flag 默认关闭，普通用户完全不显示。
- 缺失事实直接省略（不填 0/空串/中性状态伪装）。
- 不查库、不联网、不复制底层指标公式、不使用未来数据。

用法：
    from app.services.atomic_fact_contract_service import compute_atomic_facts
    result = compute_atomic_facts(structural_payload, temporal_payload)

模块自测：
    python -m app.services.atomic_fact_contract_service
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Canonical Registry 加载与计数校验
# ---------------------------------------------------------------------------

_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "atomic_fact_contract_v1.json"
)

# 阈值：S3 来自合同（已确认）。T5/V3 阈值 null + engineering_confirmation_required=true
# → 分类未启用。M3 零值容差统一（存储精度 1e-6，非数据分位数；THR-001 待确认）。
M3_ZERO_TOLERANCE = 1e-6

# T3/T6 效率 fact flag：默认关闭（EFF-001/EFF-002 未修复前，普通用户完全不显示）。
FEATURE_FLAGS: dict[str, bool] = {
    "T3_trend_efficiency": False,
    "T6_efficiency_delta": False,
}


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH, encoding="utf-8") as f:
        return json.load(f)


_CONTRACT = _load_contract()

# 顺序与计数严格与 Canonical Registry 一致（导入即校验，不一致直接失败而非静默）
CORE_FACT_IDS: list[str] = [f["id"] for f in _CONTRACT["core_facts"]]
AUX_FACT_IDS: list[str] = [f["id"] for f in _CONTRACT["auxiliary_facts"]]
REJECTED_FACT_IDS: list[str] = [f["id"] for f in _CONTRACT["rejected_facts"]]

assert len(CORE_FACT_IDS) == 14, f"core 必须 14 项，实际 {len(CORE_FACT_IDS)}"
assert len(AUX_FACT_IDS) == 10, f"auxiliary 必须 10 项，实际 {len(AUX_FACT_IDS)}"
assert len(REJECTED_FACT_IDS) == 1, f"rejected 必须 1 项，实际 {len(REJECTED_FACT_IDS)}"
assert "V1_cumulative_volume_ratio" in REJECTED_FACT_IDS
assert "V1_cumulative_volume_ratio" not in CORE_FACT_IDS + AUX_FACT_IDS
# 全量 ID 唯一
_ALL_IDS = set(CORE_FACT_IDS) | set(AUX_FACT_IDS) | set(REJECTED_FACT_IDS)
assert len(_ALL_IDS) == 25, f"fact ID 必须唯一，实际去重后 {len(_ALL_IDS)}"

# S3 边界（来自合同 thresholds.s3_position，已确认）
_S3_LOWER = float(_CONTRACT["thresholds"]["s3_position"]["lower"])
_S3_UPPER = float(_CONTRACT["thresholds"]["s3_position"]["upper"])

CONTRACT_VERSION = _CONTRACT.get("contract_version", "Atomic Fact Contract V1")

# dimension 分组顺序（用于 UI 固定四组顺序）
_DIMENSION_ORDER = ["trend", "momentum", "structure", "volume"]


# ---------------------------------------------------------------------------
# 取值辅助
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    """安全转 float，None/NaN/Inf/非数值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _norm_dir(v: Any) -> float | None:
    """方向归一化：>0→1, <0→-1, ==0→0, 缺失→None。"""
    f = _safe_float(v)
    if f is None:
        return None
    if f > 0:
        return 1.0
    if f < 0:
        return -1.0
    return 0.0


def _safe_get(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _extract_inputs(
    structural_payload: dict[str, Any],
    temporal_payload: dict[str, Any],
) -> dict[str, Any]:
    """从 payload 提取所有原始输入字段（只读，不复制底层公式）。"""
    primary_1d = _safe_get(structural_payload, "primary", "1d", default={}) or {}
    dsa = primary_1d.get("dsa_segment") or {}
    vol = primary_1d.get("volatility_momentum") or {}
    swing = primary_1d.get("swing_position") or {}
    daily = _safe_get(temporal_payload, "daily_context", default={}) or {}

    return {
        "dsa_dir": _norm_dir(dsa.get("current_dsa_segment_dir")),
        "cur_slope_atr": _safe_float(dsa.get("current_dsa_segment_slope_atr_per_bar")),
        "prev_slope_atr": _safe_float(dsa.get("prev_dsa_segment_slope_atr_per_bar")),
        "cur_age_bars": dsa.get("current_dsa_segment_age_bars"),
        "prev_age_bars": dsa.get("prev_dsa_segment_age_bars"),
        "cur_vol_sum": _safe_float(dsa.get("current_segment_volume_sum")),
        "prev_vol_sum": _safe_float(dsa.get("prev_segment_volume_sum")),
        "cur_efficiency": _safe_float(dsa.get("current_dsa_segment_efficiency_0_1")),
        "prev_efficiency": _safe_float(dsa.get("prev_dsa_segment_efficiency_0_1")),
        "sqzmom_val": _safe_float(vol.get("sqzmom_val")),
        "sqzmom_delta_1": _safe_float(vol.get("sqzmom_delta_1")),
        "sqz_on": vol.get("sqz_on"),
        "sqz_off": vol.get("sqz_off"),
        "breakout_state": swing.get("confirmed_swing_breakout_state"),
        "active_swing_dir": _norm_dir(swing.get("active_swing_dir")),
        "developing_swing_dir": _norm_dir(swing.get("developing_swing_dir")),
        "price_pos_active": _safe_float(swing.get("price_position_in_active_swing_0_1")),
        "price_pos_developing": _safe_float(swing.get("price_position_in_developing_swing_0_1")),
        "dist_high_atr": _safe_float(swing.get("distance_to_swing_high_atr")),
        "dist_low_atr": _safe_float(swing.get("distance_to_swing_low_atr")),
        "daily_sqzmom_change": _safe_float(daily.get("daily_sqzmom_change_since_segment_start")),
        "current_segment_return_per_volume": _safe_float(dsa.get("current_segment_return_per_volume")),
        "return_per_volume_ratio": _safe_float(dsa.get("return_per_volume_ratio")),
        # V1 拒绝项（仅 DB 调试值，永不作为事实输出）
        "v1_cumulative_volume_ratio": _safe_float(dsa.get("current_vs_prev_volume_ratio")),
    }


# ---------------------------------------------------------------------------
# 分类辅助
# ---------------------------------------------------------------------------


def _categorize_position(pos: float | None) -> str | None:
    """S3/S6 位置分类：严格 0.33/0.67 边界。"""
    if pos is None:
        return None
    if pos < 0 or pos > 1:
        return "OUT_OF_RANGE"
    if pos < _S3_LOWER:
        return "LOWER"
    if pos <= _S3_UPPER:
        return "MIDDLE"
    return "UPPER"


def _momentum_alignment(sqz_val: float | None, dsa_dir: float | None) -> str | None:
    if sqz_val is None or dsa_dir is None:
        return None
    if dsa_dir == 0.0 or sqz_val == 0.0:
        return "ZERO"
    if (sqz_val > 0 and dsa_dir > 0) or (sqz_val < 0 and dsa_dir < 0):
        return "ALIGNED"
    return "COUNTER"


def _confirmed_boundary_relation(
    breakout_state: str | None, dsa_dir: float | None
) -> str | None:
    if breakout_state is None or dsa_dir is None or dsa_dir == 0.0:
        return None
    if dsa_dir > 0:
        if breakout_state == "above_confirmed_high":
            return "BREAK_FAVORABLE"
        if breakout_state == "below_confirmed_low":
            return "BREAK_ADVERSE"
        return "INSIDE"
    else:
        if breakout_state == "below_confirmed_low":
            return "BREAK_FAVORABLE"
        if breakout_state == "above_confirmed_high":
            return "BREAK_ADVERSE"
        return "INSIDE"


def _dir_relation(active_dir: float | None, dsa_dir: float | None) -> str | None:
    if active_dir is None or dsa_dir is None or dsa_dir == 0.0 or active_dir == 0.0:
        return None
    return "ALIGNED" if active_dir == dsa_dir else "COUNTER"


def _active_vs_developing(active_dir: float | None, dev_dir: float | None) -> str | None:
    if active_dir is None or dev_dir is None or active_dir == 0.0 or dev_dir == 0.0:
        return None
    return "SAME_DIRECTION" if active_dir == dev_dir else "OPPOSITE_DIRECTION"


def _squeeze_state(sqz_on: Any, sqz_off: Any) -> str | None:
    on = None if sqz_on is None else bool(sqz_on)
    off = None if sqz_off is None else bool(sqz_off)
    if on is None and off is None:
        return None
    if on is True and off is True:
        return "INCONSISTENT"
    if on is True:
        return "ON"
    if off is True:
        return "OFF"
    return "NORMAL"


def _m3_category(raw: float | None) -> str | None:
    """M3 统一零值容差分类：增加/减少/基本不变。"""
    if raw is None:
        return None
    if abs(raw) <= M3_ZERO_TOLERANCE:
        return "基本不变"
    return "增加" if raw > 0 else "减少"


# 中文展示映射（通俗，不歪曲事实，禁禁用词）
_DIR_ZH = {1.0: "上行", -1.0: "下行", 0.0: "中性", None: "中性"}
_ALIGN_ZH = {"ALIGNED": "同向", "COUNTER": "逆向", "ZERO": "中性", None: "中性"}
_BOUNDARY_ZH = {
    "BREAK_FAVORABLE": "顺DSA方向突破确认边界",
    "INSIDE": "价格在区间内",
    "BREAK_ADVERSE": "逆DSA方向边界破坏",
    None: "结构数据不足",
}
_DIRREL_ZH = {"ALIGNED": "一致", "COUNTER": "相反", None: "数据不足"}
_POS_ZH = {"LOWER": "偏低", "MIDDLE": "中间", "UPPER": "偏高", "OUT_OF_RANGE": "区间外", None: "数据不足"}
_SQZ_ZH = {"ON": "挤压中", "OFF": "释放中", "NORMAL": "正常", "INCONSISTENT": "数据质量异常", None: "数据不足"}
_ADV_ZH = {"SAME_DIRECTION": "一致", "OPPOSITE_DIRECTION": "相反", None: "数据不足"}


def _fmt_dist(d: float | None) -> str:
    """S7/S8 距离文案：禁止显示负距离。d>=0 尚未到达；d<0 已越过。

    仅在 d 非 None 时调用（缺失由调用方 fallback 文案处理），故返回 str。
    """
    if d is None:
        return ""
    if d >= 0:
        return f"尚未到达 {abs(d):.4f} ATR"
    return f"已越过 {abs(d):.4f} ATR"


# ---------------------------------------------------------------------------
# 核心计算
# ---------------------------------------------------------------------------


def _build_fact_item(
    *,
    fact_id: str,
    dimension: str,
    label: str,
    value: float | None,
    unit: str | None,
    category: str | None,
    display_text: str,
    threshold_enabled: bool = True,
    missing: bool = False,
    hidden_by_default: bool = False,
    source_path: str | None = None,
) -> dict[str, Any]:
    return {
        "factId": fact_id,
        "dimension": dimension,
        "label": label,
        "value": value,
        "unit": unit,
        "category": category,
        "displayText": display_text,
        "thresholdEnabled": threshold_enabled,
        "missing": missing,
        "hiddenByDefault": hidden_by_default,
        "sourcePath": source_path,
    }


def compute_atomic_facts(
    structural_payload: dict[str, Any] | None,
    temporal_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """纯函数：从 payload 只读计算 14 Core + 10 Auxiliary 原子事实。

    Returns:
        {
          "core": {dimension: [fact_item, ...]},  # 固定四组顺序
          "auxiliary": [fact_item, ...],           # 默认隐藏项（T3/T6 flag 关时不含）
          "availability": {...},
        }
    """
    sp = structural_payload or {}
    tp = temporal_payload or {}
    d = _extract_inputs(sp, tp)
    dsa_dir = d["dsa_dir"]
    has_dir = dsa_dir is not None and dsa_dir != 0.0

    core_items: dict[str, dict[str, Any]] = {}
    aux_items: dict[str, dict[str, Any]] = {}

    # ---------------- Core ----------------
    # T1 趋势方向
    t1_cat = {1.0: "UP", -1.0: "DOWN", 0.0: "NONE", None: "NONE"}.get(dsa_dir)
    core_items["T1_trend_direction"] = _build_fact_item(
        fact_id="T1_trend_direction",
        dimension="trend",
        label="趋势方向",
        value=dsa_dir,
        unit=None,
        category=t1_cat,
        display_text=f"DSA当前趋势方向为{_DIR_ZH.get(dsa_dir, '中性')}",
        missing=dsa_dir is None,
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_dir",
    )

    # T2 对齐斜率（真实值，单位 ATR/bar）
    t2_missing = not (has_dir and d["cur_slope_atr"] is not None)
    t2_val = (dsa_dir * d["cur_slope_atr"]) if not t2_missing else None
    core_items["T2_aligned_slope"] = _build_fact_item(
        fact_id="T2_aligned_slope",
        dimension="trend",
        label="方向对齐斜率",
        value=t2_val,
        unit="ATR/bar",
        category=None,
        display_text=(
            f"方向对齐斜率为{t2_val:.4f} ATR/bar" if t2_val is not None else "方向对齐斜率数据不足"
        ),
        missing=t2_missing,
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_slope_atr_per_bar",
    )

    # T4 段龄（int）
    t4_age = None
    if d["cur_age_bars"] is not None:
        try:
            t4_age = int(d["cur_age_bars"])
        except (ValueError, TypeError):
            t4_age = None
    core_items["T4_trend_age"] = _build_fact_item(
        fact_id="T4_trend_age",
        dimension="trend",
        label="段龄",
        value=t4_age,
        unit="bar",
        category=None,
        display_text=f"当前Segment已持续{t4_age}根bar" if t4_age is not None else "段龄数据不足",
        missing=t4_age is None,
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_age_bars",
    )

    # T5 斜率比（阈值未确认 → 仅比值 + 分类未启用）
    t5_missing = not (
        d["cur_slope_atr"] is not None
        and d["prev_slope_atr"] is not None
        and d["prev_slope_atr"] != 0
    )
    t5_ratio = None
    if not t5_missing:
        t5_ratio = abs(d["cur_slope_atr"]) / abs(d["prev_slope_atr"])
    core_items["T5_slope_ratio"] = _build_fact_item(
        fact_id="T5_slope_ratio",
        dimension="trend",
        label="斜率相对前段比",
        value=t5_ratio,
        unit=None,
        category=None,
        threshold_enabled=False,
        display_text=(
            f"斜率相对前段比值为{t5_ratio:.4f}（分类未启用）"
            if t5_ratio is not None
            else "斜率比数据不足（前段斜率为0或缺失）"
        ),
        missing=t5_missing,
        source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_slope_atr_per_bar",
    )

    # M1 动量对齐
    m1 = _momentum_alignment(d["sqzmom_val"], dsa_dir)
    core_items["M1_momentum_alignment"] = _build_fact_item(
        fact_id="M1_momentum_alignment",
        dimension="momentum",
        label="动量对齐",
        value=None,
        unit=None,
        category=m1,
        display_text=f"SQZMOM动量与趋势{_ALIGN_ZH.get(m1, '中性')}",
        missing=m1 is None,
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_val",
    )

    # M2 对齐动量（真实值）
    m2_missing = not (has_dir and d["sqzmom_val"] is not None)
    m2_val = (dsa_dir * d["sqzmom_val"]) if not m2_missing else None
    core_items["M2_aligned_momentum"] = _build_fact_item(
        fact_id="M2_aligned_momentum",
        dimension="momentum",
        label="方向对齐动量",
        value=m2_val,
        unit=None,
        category=None,
        display_text=(
            f"方向对齐动量值为{m2_val:.4f}" if m2_val is not None else "方向对齐动量数据不足"
        ),
        missing=m2_missing,
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_val",
    )

    # M3 对齐动量变化（真实值 + 统一零值容差分类）
    m3_missing = not (has_dir and d["sqzmom_delta_1"] is not None)
    m3_raw = (dsa_dir * d["sqzmom_delta_1"]) if not m3_missing else None
    m3_cat = _m3_category(m3_raw)
    core_items["M3_aligned_momentum_delta"] = _build_fact_item(
        fact_id="M3_aligned_momentum_delta",
        dimension="momentum",
        label="对齐动量变化",
        value=m3_raw,
        unit=None,
        category=m3_cat,
        display_text=(
            f"最近一Bar对齐动量变化：{m3_cat}（raw={m3_raw:.6f}）"
            if m3_raw is not None
            else "对齐动量变化数据不足"
        ),
        missing=m3_missing,
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_delta_1",
    )

    # M5 挤压状态
    m5 = _squeeze_state(d["sqz_on"], d["sqz_off"])
    core_items["M5_squeeze_state"] = _build_fact_item(
        fact_id="M5_squeeze_state",
        dimension="momentum",
        label="挤压状态",
        value=None,
        unit=None,
        category=m5,
        display_text=f"波动率挤压状态：{_SQZ_ZH.get(m5, '数据不足')}",
        missing=m5 is None,
        source_path="structural_payload.primary.1d.volatility_momentum.sqz_on",
    )

    # S1 确认边界关系
    s1 = _confirmed_boundary_relation(d["breakout_state"], dsa_dir)
    core_items["S1_confirmed_boundary_relation"] = _build_fact_item(
        fact_id="S1_confirmed_boundary_relation",
        dimension="structure",
        label="确认边界关系",
        value=None,
        unit=None,
        category=s1,
        display_text=f"{_BOUNDARY_ZH.get(s1, '结构数据不足')}",
        missing=s1 is None,
        source_path="structural_payload.primary.1d.swing_position.confirmed_swing_breakout_state",
    )

    # S2 active 方向关系
    s2 = _dir_relation(d["active_swing_dir"], dsa_dir)
    core_items["S2_active_dir_relation"] = _build_fact_item(
        fact_id="S2_active_dir_relation",
        dimension="structure",
        label="Active方向关系",
        value=None,
        unit=None,
        category=s2,
        display_text=f"Active Swing方向与DSA{_DIRREL_ZH.get(s2, '数据不足')}",
        missing=s2 is None,
        source_path="structural_payload.primary.1d.swing_position.active_swing_dir",
    )

    # S3 active 位置（0.33/0.67）
    s3 = _categorize_position(d["price_pos_active"])
    core_items["S3_active_position"] = _build_fact_item(
        fact_id="S3_active_position",
        dimension="structure",
        label="Active区间位置",
        value=d["price_pos_active"],
        unit=None,
        category=s3,
        display_text=(
            f"价格在Active Swing区间内位置：{_POS_ZH.get(s3, '数据不足')}"
            if s3 is not None
            else "Active区间位置数据不足"
        ),
        missing=s3 is None,
        source_path="structural_payload.primary.1d.swing_position.price_position_in_active_swing_0_1",
    )

    # S7 顺向距离 / S8 逆向距离（禁止负距离显示）
    s7_missing = not has_dir
    if not s7_missing:
        if dsa_dir > 0:
            s7_val = d["dist_high_atr"]
        else:
            s7_val = d["dist_low_atr"]
    else:
        s7_val = None
    s7_missing = s7_missing or (s7_val is None)
    core_items["S7_dist_favorable_boundary"] = _build_fact_item(
        fact_id="S7_dist_favorable_boundary",
        dimension="structure",
        label="顺向边界距离",
        value=s7_val,
        unit="ATR",
        category=None,
        display_text=_fmt_dist(s7_val) if s7_val is not None else "顺向边界距离数据不足",
        missing=s7_missing,
        source_path="structural_payload.primary.1d.swing_position.distance_to_swing_high_atr",
    )

    s8_missing = not has_dir
    if not s8_missing:
        if dsa_dir > 0:
            s8_val = d["dist_low_atr"]
        else:
            s8_val = d["dist_high_atr"]
    else:
        s8_val = None
    s8_missing = s8_missing or (s8_val is None)
    core_items["S8_dist_adverse_boundary"] = _build_fact_item(
        fact_id="S8_dist_adverse_boundary",
        dimension="structure",
        label="逆向边界距离",
        value=s8_val,
        unit="ATR",
        category=None,
        display_text=_fmt_dist(s8_val) if s8_val is not None else "逆向边界距离数据不足",
        missing=s8_missing,
        source_path="structural_payload.primary.1d.swing_position.distance_to_swing_low_atr",
    )

    # V3 段均量比（阈值未确认 → 仅比值 + 分类未启用）
    _cur_age = _safe_int(d["cur_age_bars"])
    _prev_age = _safe_int(d["prev_age_bars"])
    v3_missing = not (
        d["cur_vol_sum"] is not None
        and _cur_age is not None
        and _cur_age > 0
        and d["prev_vol_sum"] is not None
        and _prev_age is not None
        and _prev_age > 0
    )
    v3_ratio = None
    if not v3_missing and _cur_age is not None and _prev_age is not None:
        _cur_avg = d["cur_vol_sum"] / _cur_age
        _prev_avg = d["prev_vol_sum"] / _prev_age
        if _prev_avg == 0:
            v3_missing = True
        else:
            v3_ratio = _cur_avg / _prev_avg
    core_items["V3_avg_volume_ratio"] = _build_fact_item(
        fact_id="V3_avg_volume_ratio",
        dimension="volume",
        label="段均量比",
        value=v3_ratio,
        unit=None,
        category=None,
        threshold_enabled=False,
        display_text=(
            f"Segment均量与前段比值为{v3_ratio:.4f}（分类未启用）"
            if v3_ratio is not None
            else "段均量比数据不足（前段均量为0或缺失）"
        ),
        missing=v3_missing,
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_volume_sum",
    )

    # ---------------- Auxiliary（默认隐藏） ----------------
    # T3 趋势效率（flag 关 → 不含）
    if FEATURE_FLAGS.get("T3_trend_efficiency", False):
        aux_items["T3_trend_efficiency"] = _build_fact_item(
            fact_id="T3_trend_efficiency",
            dimension="trend",
            label="趋势效率",
            value=d["cur_efficiency"],
            unit=None,
            category=None,
            hidden_by_default=True,
            display_text=(
                f"趋势效率为{d['cur_efficiency']:.4f}（仅修复后启用）"
                if d["cur_efficiency"] is not None
                else "趋势效率数据不足"
            ),
            missing=d["cur_efficiency"] is None,
            source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_efficiency_0_1",
        )

    # T6 效率差（flag 关 → 不含）
    if FEATURE_FLAGS.get("T6_efficiency_delta", False):
        t6_delta = None
        if d["cur_efficiency"] is not None and d["prev_efficiency"] is not None:
            t6_delta = d["cur_efficiency"] - d["prev_efficiency"]
        aux_items["T6_efficiency_delta"] = _build_fact_item(
            fact_id="T6_efficiency_delta",
            dimension="trend",
            label="效率差",
            value=t6_delta,
            unit=None,
            category=None,
            hidden_by_default=True,
            display_text=(
                f"效率差为{t6_delta:.4f}（仅修复后启用）"
                if t6_delta is not None
                else "效率差数据不足"
            ),
            missing=t6_delta is None,
            source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_efficiency_0_1",
        )

    # M4 Segment 起点动量变化
    m4_missing = not (has_dir and d["daily_sqzmom_change"] is not None)
    m4_val = (dsa_dir * d["daily_sqzmom_change"]) if not m4_missing else None
    aux_items["M4_segment_momentum_change"] = _build_fact_item(
        fact_id="M4_segment_momentum_change",
        dimension="momentum",
        label="Segment起点动量变化",
        value=m4_val,
        unit=None,
        category=None,
        hidden_by_default=True,
        display_text=(
            f"Segment起点动量变化：{m4_val:.4f}"
            if m4_val is not None
            else "Segment起点动量变化数据不足"
        ),
        missing=m4_missing,
        source_path="temporal_payload.daily_context.daily_sqzmom_change_since_segment_start",
    )

    # S4 developing 方向关系
    s4 = _dir_relation(d["developing_swing_dir"], dsa_dir)
    aux_items["S4_developing_dir_relation"] = _build_fact_item(
        fact_id="S4_developing_dir_relation",
        dimension="structure",
        label="Developing方向关系",
        value=None,
        unit=None,
        category=s4,
        hidden_by_default=True,
        display_text=f"Developing Swing方向与DSA{_DIRREL_ZH.get(s4, '数据不足')}",
        missing=s4 is None,
        source_path="structural_payload.primary.1d.swing_position.developing_swing_dir",
    )

    # S5 active vs developing
    s5 = _active_vs_developing(d["active_swing_dir"], d["developing_swing_dir"])
    aux_items["S5_active_vs_developing"] = _build_fact_item(
        fact_id="S5_active_vs_developing",
        dimension="structure",
        label="Active/Developing关系",
        value=None,
        unit=None,
        category=s5,
        hidden_by_default=True,
        display_text=f"Active与Developing{_ADV_ZH.get(s5, '数据不足')}",
        missing=s5 is None,
        source_path="structural_payload.primary.1d.swing_position.developing_swing_dir",
    )

    # S6 developing 位置
    s6 = _categorize_position(d["price_pos_developing"])
    aux_items["S6_developing_position"] = _build_fact_item(
        fact_id="S6_developing_position",
        dimension="structure",
        label="Developing区间位置",
        value=d["price_pos_developing"],
        unit=None,
        category=s6,
        hidden_by_default=True,
        display_text=(
            f"价格在Developing区间位置：{_POS_ZH.get(s6, '数据不足')}"
            if s6 is not None
            else "Developing区间位置数据不足"
        ),
        missing=s6 is None,
        source_path="structural_payload.primary.1d.swing_position.price_position_in_developing_swing_0_1",
    )

    # V2 当前段平均量
    _cur_age = _safe_int(d["cur_age_bars"])
    v2_missing = not (
        d["cur_vol_sum"] is not None
        and _cur_age is not None
        and _cur_age > 0
    )
    v2_val = (d["cur_vol_sum"] / _cur_age) if (_cur_age is not None and not v2_missing) else None
    aux_items["V2_current_avg_volume"] = _build_fact_item(
        fact_id="V2_current_avg_volume",
        dimension="volume",
        label="当前段平均量",
        value=v2_val,
        unit=None,
        category=None,
        hidden_by_default=True,
        display_text=(
            f"当前Segment平均每Bar量：{v2_val:.2f}"
            if v2_val is not None
            else "当前段平均量数据不足"
        ),
        missing=v2_missing,
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_volume_sum",
    )

    # V4 段年龄比
    _cur_age = _safe_int(d["cur_age_bars"])
    _prev_age = _safe_int(d["prev_age_bars"])
    v4_missing = not (
        _cur_age is not None and _prev_age is not None and _prev_age > 0
    )
    v4_val = (
        _cur_age / _prev_age
    ) if (_cur_age is not None and _prev_age is not None and not v4_missing) else None
    aux_items["V4_age_ratio_raw"] = _build_fact_item(
        fact_id="V4_age_ratio_raw",
        dimension="volume",
        label="段年龄比",
        value=v4_val,
        unit=None,
        category=None,
        hidden_by_default=True,
        display_text=(
            f"当前段相对前段年龄比：{v4_val:.4f}"
            if v4_val is not None
            else "段年龄比数据不足"
        ),
        missing=v4_missing,
        source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_age_bars",
    )

    # V5 收益率/量
    aux_items["V5_return_per_volume"] = _build_fact_item(
        fact_id="V5_return_per_volume",
        dimension="volume",
        label="段收益率/量",
        value=d["current_segment_return_per_volume"],
        unit=None,
        category=None,
        hidden_by_default=True,
        display_text=(
            f"当前Segment收益率/成交量：{d['current_segment_return_per_volume']:.6f}"
            if d["current_segment_return_per_volume"] is not None
            else "段收益率/量数据不足"
        ),
        missing=d["current_segment_return_per_volume"] is None,
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_return_per_volume",
    )

    # V5_ratio 收益率/量比
    aux_items["V5_return_per_volume_ratio"] = _build_fact_item(
        fact_id="V5_return_per_volume_ratio",
        dimension="volume",
        label="收益率/量比",
        value=d["return_per_volume_ratio"],
        unit=None,
        category=None,
        hidden_by_default=True,
        display_text=(
            f"收益率/量比：{d['return_per_volume_ratio']:.4f}"
            if d["return_per_volume_ratio"] is not None
            else "收益率/量比数据不足"
        ),
        missing=d["return_per_volume_ratio"] is None,
        source_path="structural_payload.primary.1d.dsa_segment.return_per_volume_ratio",
    )

    # ---------------- 分组（固定四组顺序） ----------------
    core_grouped: dict[str, list[dict[str, Any]]] = {dim: [] for dim in _DIMENSION_ORDER}
    # 按 Canonical Registry 顺序放入对应 dimension 组
    for fid in CORE_FACT_IDS:
        item = core_items[fid]
        core_grouped.setdefault(item["dimension"], []).append(item)

    auxiliary_list = [aux_items[fid] for fid in AUX_FACT_IDS if fid in aux_items]

    # ---------------- 可用性 ----------------
    core_present = sum(1 for it in core_items.values() if not it["missing"])
    core_missing = [fid for fid, it in core_items.items() if it["missing"]]
    aux_available = [fid for fid, it in aux_items.items() if not it["missing"]]
    # 默认隐藏：响应中实际返回的 Auxiliary ID（全部 hiddenByDefault=true，不在用户 UI 展示）
    aux_hidden = [it["factId"] for it in auxiliary_list]

    availability = {
        "coreDenominator": len(CORE_FACT_IDS),
        "corePresent": core_present,
        "coreMissing": core_missing,
        "auxiliaryAvailable": aux_available,
        "auxiliaryHidden": aux_hidden,
        "v1Present": False,
        "rejectedPresent": False,
    }

    return {
        "core": core_grouped,
        "auxiliary": auxiliary_list,
        "availability": availability,
    }


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 近期变化（一次查询 ≤10 快照，只读计算，不写 stock_state_events）
# ---------------------------------------------------------------------------


def compute_recent_changes(
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 ≤10 个已发布兼容快照（按 trade_date 升序）只读计算 Core 事实变化。

    Args:
        snapshots: 每个元素 {trade_date: str(ISO), structural_payload, temporal_payload}
                   必须已是升序（调用方按 trade_date 排序）。

    Returns:
        变化记录列表（按时间升序），每条：
        {factId, dimension, fromCategory, toCategory, fromValue, toValue, asOf}
    """
    if len(snapshots) < 2:
        return []

    changes: list[dict[str, Any]] = []
    prev = None
    for snap in snapshots:
        cur = compute_atomic_facts(snap.get("structural_payload"), snap.get("temporal_payload"))
        if prev is not None:
            for fid in CORE_FACT_IDS:
                p_item = prev["core_items"].get(fid)
                c_item = cur_core_item(cur, fid)
                if p_item is None or c_item is None:
                    continue
                if p_item["category"] != c_item["category"] or p_item["value"] != c_item["value"]:
                    changes.append({
                        "factId": fid,
                        "dimension": c_item["dimension"],
                        "fromCategory": p_item["category"],
                        "toCategory": c_item["category"],
                        "fromValue": p_item["value"],
                        "toValue": c_item["value"],
                        "asOf": snap["trade_date"],
                    })
        prev = {"core_items": {fid: cur_core_item(cur, fid) for fid in CORE_FACT_IDS}}

    # 限制体积（最多 30 条，避免超大 payload）
    if len(changes) > 30:
        changes = changes[-30:]
    return changes


def cur_core_item(result: dict[str, Any], fact_id: str) -> dict[str, Any] | None:
    """从 compute_atomic_facts 结果提取单个 core fact item（跨 dimension 查找）。"""
    for _dim, items in result["core"].items():
        for it in items:
            if it["factId"] == fact_id:
                return it
    return None


# ---------------------------------------------------------------------------
# 模块自测
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    print("atomic_fact_contract_service 自测...")

    sample_sp = {
        "primary": {
            "1d": {
                "dsa_segment": {
                    "current_dsa_segment_dir": 1,
                    "current_dsa_segment_slope_atr_per_bar": 0.0123,
                    "prev_dsa_segment_slope_atr_per_bar": 0.0100,
                    "current_dsa_segment_age_bars": 12,
                    "prev_dsa_segment_age_bars": 10,
                    "current_segment_volume_sum": 1200000.0,
                    "prev_segment_volume_sum": 900000.0,
                    "current_dsa_segment_efficiency_0_1": 0.7,
                    "prev_dsa_segment_efficiency_0_1": 0.6,
                    "current_segment_return_per_volume": 0.000123,
                    "return_per_volume_ratio": 0.5,
                },
                "volatility_momentum": {
                    "sqzmom_val": 0.002,
                    "sqzmom_delta_1": 0.0003,
                    "sqz_on": False,
                    "sqz_off": True,
                },
                "swing_position": {
                    "confirmed_swing_breakout_state": "inside",
                    "active_swing_dir": 1,
                    "developing_swing_dir": 1,
                    "price_position_in_active_swing_0_1": 0.63,
                    "price_position_in_developing_swing_0_1": 0.5,
                    "distance_to_swing_high_atr": 2.5,
                    "distance_to_swing_low_atr": -1.2,
                },
            }
        }
    }
    sample_tp = {"daily_context": {"daily_sqzmom_change_since_segment_start": 0.001}}

    res = compute_atomic_facts(sample_sp, sample_tp)

    def _get(fid: str) -> dict[str, Any]:
        item = cur_core_item(res, fid)
        assert item is not None, fid
        return item

    # 计数严格 14/10/1
    assert len(CORE_FACT_IDS) == 14
    assert len(AUX_FACT_IDS) == 10
    assert len(REJECTED_FACT_IDS) == 1

    # S3 0.63 → 中间
    s3 = _get("S3_active_position")
    assert s3["category"] == "MIDDLE", f"0.63 必须映射中间，实际 {s3['category']}"

    # S7 顺向（dsa_dir>0 → dist_high=2.5）：尚未到达；S8 逆向（dist_low=-1.2）：已越过
    s7 = _get("S7_dist_favorable_boundary")
    assert "尚未到达" in s7["displayText"], s7["displayText"]
    s8 = _get("S8_dist_adverse_boundary")
    assert "已越过" in s8["displayText"], s8["displayText"]

    # T2/M2/M3 真实值
    t2 = _get("T2_aligned_slope")
    assert abs(t2["value"] - 0.0123) < 1e-9, t2["value"]
    m2 = _get("M2_aligned_momentum")
    assert abs(m2["value"] - 0.002) < 1e-9, m2["value"]
    m3 = _get("M3_aligned_momentum_delta")
    assert abs(m3["value"] - 0.0003) < 1e-9, m3["value"]
    assert m3["category"] == "增加"

    # T5/V3 分类未启用
    t5 = _get("T5_slope_ratio")
    assert t5["thresholdEnabled"] is False
    assert "分类未启用" in t5["displayText"]
    v3 = _get("V3_avg_volume_ratio")
    assert v3["thresholdEnabled"] is False
    assert "分类未启用" in v3["displayText"]

    # V1 永不出现
    flat = json.dumps(res, ensure_ascii=False)
    assert "V1_cumulative_volume_ratio" not in flat
    assert res["availability"]["v1Present"] is False

    # T3/T6 默认隐藏（flag 关 → 不在 auxiliary 中，也不出现在 auxiliaryHidden）
    aux_ids = [it["factId"] for it in res["auxiliary"]]
    assert "T3_trend_efficiency" not in aux_ids
    assert "T6_efficiency_delta" not in aux_ids
    assert "T3_trend_efficiency" not in res["availability"]["auxiliaryHidden"]
    assert "T6_efficiency_delta" not in res["availability"]["auxiliaryHidden"]
    # auxiliaryHidden 为响应中实际返回的 Auxiliary ID（全部默认隐藏）
    assert set(res["availability"]["auxiliaryHidden"]) == set(aux_ids)
    assert len(aux_ids) == 8  # 10 aux - T3 - T6

    # 分母固定 14
    assert res["availability"]["coreDenominator"] == 14

    print("OK: compute_atomic_facts 验证通过")
