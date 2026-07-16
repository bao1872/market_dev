"""Atomic Fact Contract V1 - 纯函数服务（生产实现）。

从 structural_payload.primary.1d + temporal_payload.daily_context 只读计算
14 Core + 10 Auxiliary 原子事实。

设计约束（V4.13 冻结合同，硬性）：
- 生产实现不依赖实验脚本运行；仅以
  backend/app/contracts/atomic_fact_contract_v1.json 为 Canonical Registry
  （计数/顺序/阈值真源），计算公式在本模块以纯函数重写。
- 用户侧（普通会员）返回项仅含稳定 publicKey / 中文 label / visualKind /
  value / valueText / categoryCode / categoryLabel / secondaryText / unit /
  thresholdEnabled，**绝不**含 factId / sourcePath / 公式 / 阈值引用。
  管理员 debug 单独保留 factId / sourcePath / rawValue / thresholdRef / featureFlag。
- 缺失事实直接从用户 core/auxiliary 数组省略（不填 0/空串/中性状态伪装）；
  availability 固定分母 14 并列出缺失 publicKey。
- 所有普通用户文案不含 DSA / SQZMOM / Segment / Active/Developing Swing / bar / raw
  等内部术语，仅描述客观状态，不构成买卖建议。
- T2/M2/M3 显示真实原始值，禁止伪造成 [-1,1] 固定范围。
- T5/V3 阈值 engineering_confirmation_required=true 且值为 null
  → 仅显示比值，标记「分类未启用」（不声称已分类）。
- M3 零值判定：仅按正/负/精确零（raw==0）显示原始变化；不硬编码任何容差
  （Registry m3_zero_tolerance.value=null，THR-001 待工程确认）。
- S3 严格 0.33/0.67 边界（0.63 → 中间）；越界视为缺失，不输出 OUT_OF_RANGE。
- S7/S8 禁止显示负距离：d>=0「尚未到达 |d| ATR」；d<0「已越过 |d| ATR」。
- V1 累计成交量比永不进入用户 API/UI/摘要/可用性计数。
- T3/T6 效率 fact flag 默认关闭，普通用户完全不显示（expanded 也不渲染）。
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

# 产品文案 / UI 类型 来自 presentation 合同（atomic_fact_presentation_v1.json），
# 与冻结研究合同分离：冻结合同只含事实/公式/阈值/路径，不含 publicKey 等产品字段。
_PRES_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "atomic_fact_presentation_v1.json"
)


def _load_presentation() -> dict[str, Any]:
    with open(_PRES_PATH, encoding="utf-8") as f:
        return json.load(f)


_PRES = _load_presentation()
_PRES_FACTS = {f["id"]: f for f in _PRES["facts"]}

# publicKey / publicLabel 映射（仅覆盖普通用户展示的 14 Core + 8 Aux；T3/T6/V1 不在内）
CORE_PUBLIC_KEY: dict[str, str] = {
    f["id"]: f["publicKey"] for f in _PRES["facts"] if f["level"] == "core"
}
CORE_PUBLIC_LABEL: dict[str, str] = {
    f["id"]: f["publicLabel"] for f in _PRES["facts"] if f["level"] == "core"
}
AUX_PUBLIC_KEY: dict[str, str] = {
    f["id"]: f["publicKey"] for f in _PRES["facts"] if f["level"] == "auxiliary"
}
AUX_PUBLIC_LABEL: dict[str, str] = {
    f["id"]: f["publicLabel"] for f in _PRES["facts"] if f["level"] == "auxiliary"
}

# 校验：presentation 恰好覆盖 14 Core + 8 Aux（不含 T3/T6/V1）
assert len(CORE_PUBLIC_KEY) == 14, f"presentation core 必须 14，实际 {len(CORE_PUBLIC_KEY)}"
assert len(AUX_PUBLIC_KEY) == 8, f"presentation aux 必须 8，实际 {len(AUX_PUBLIC_KEY)}"
assert "T3_trend_efficiency" not in _PRES_FACTS
assert "T6_efficiency_delta" not in _PRES_FACTS
assert "V1_cumulative_volume_ratio" not in _PRES_FACTS

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


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


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
# 分类辅助（返回中文 label，禁止内部术语）
# ---------------------------------------------------------------------------

# 事实白名单枚举（仅这些合法值，未知值一律视为缺失）
_BOUNDARY_WHITELIST = {"above_confirmed_high", "below_confirmed_low", "inside"}


def _categorize_position(pos: float | None) -> str | None:
    """S3/S6 位置分类：严格 0.33/0.67 边界；越界（不在 [0,1]）返回 None（缺失）。"""
    if pos is None:
        return None
    if pos < 0 or pos > 1:
        return None  # 越界必须缺失，不输出 OUT_OF_RANGE
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
    """仅白名单枚举：above_confirmed_high / below_confirmed_low / inside。
    未知值（非白名单）不得默认为区间内，直接缺失。"""
    if breakout_state not in _BOUNDARY_WHITELIST:
        return None
    if dsa_dir is None or dsa_dir == 0.0:
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
    """波动收紧状态。双 true 为 data-quality 异常（返回 INCONSISTENT，调用方转缺失+告警）。"""
    on = None if sqz_on is None else bool(sqz_on)
    off = None if sqz_off is None else bool(sqz_off)
    if on is None and off is None:
        return None
    if on is True and off is True:
        return "INCONSISTENT"  # 数据质量异常，非事实值
    if on is True:
        return "ON"
    if off is True:
        return "OFF"
    return "NORMAL"


def _m3_category(raw: float | None) -> str | None:
    """M3 零值判定：仅按正/负/精确零（raw==0）显示原始变化。
    不硬编码任何容差（THR-001 待工程确认）。"""
    if raw is None:
        return None
    if raw > 0:
        return "INCREASE"
    if raw < 0:
        return "DECREASE"
    return "UNCHANGED"  # 仅精确零


# 中文展示映射（通俗，不歪曲事实，禁禁用词）
_DIR_ZH = {1.0: "上行", -1.0: "下行", 0.0: "中性", None: "中性"}
_ALIGN_ZH = {"ALIGNED": "同向", "COUNTER": "逆向", "ZERO": "中性", None: "中性"}
_BOUNDARY_ZH = {
    "BREAK_FAVORABLE": "顺主趋势突破确认边界",
    "INSIDE": "价格在区间内",
    "BREAK_ADVERSE": "逆主趋势边界破坏",
    None: "结构数据不足",
}
_DIRREL_ZH = {"ALIGNED": "一致", "COUNTER": "相反", None: "数据不足"}
_POS_ZH = {"LOWER": "偏低", "MIDDLE": "中间", "UPPER": "偏高", None: "数据不足"}
_SQZ_ZH = {"ON": "挤压中", "OFF": "释放中", "NORMAL": "正常", "INCONSISTENT": "数据质量异常", None: "数据不足"}
_ADV_ZH = {"SAME_DIRECTION": "一致", "OPPOSITE_DIRECTION": "相反", None: "数据不足"}
_M3_ZH = {"INCREASE": "增加", "DECREASE": "减少", "UNCHANGED": "基本不变", None: "数据不足"}


def _fmt_dist(d: float | None) -> str:
    """S7/S8 距离文案：禁止显示负距离。d>=0 尚未到达；d<0 已越过。"""
    if d is None:
        return ""
    if d >= 0:
        return f"尚未到达 {abs(d):.4f} ATR"
    return f"已越过 {abs(d):.4f} ATR"


# ---------------------------------------------------------------------------
# 事实项发射（用户项 / 管理员 debug 项 分离）
# ---------------------------------------------------------------------------


def _emit(
    *,
    fact_id: str,
    public_key: str,
    label: str,
    dimension: str,
    visual_kind: str,
    value: float | None,
    value_text: str,
    source_path: str | None,
    threshold_ref: str | None = None,
    threshold_enabled: bool = True,
    feature_flag: bool = True,
    category_code: str | None = None,
    category_label: str | None = None,
    secondary_text: str | None = None,
    unit: str | None = None,
    missing: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """发射一个事实。

    Returns:
        (public_item, debug_item)
        - public_item：None 表示缺失（不进入用户 core/auxiliary 数组）
        - debug_item：始终构建（管理员可追溯所有事实，含缺失标记）
    """
    public_item: dict[str, Any] | None = None
    if not missing:
        public_item = {
            "publicKey": public_key,
            "dimension": dimension,
            "label": label,
            "visualKind": visual_kind,
            "value": value,
            "valueText": value_text,
            "categoryCode": category_code,
            "categoryLabel": category_label,
            "secondaryText": secondary_text,
            "unit": unit,
            "thresholdEnabled": threshold_enabled,
        }
    debug_item = {
        "factId": fact_id,
        "publicKey": public_key,
        "sourcePath": source_path,
        "rawValue": value,
        "thresholdRef": threshold_ref,
        "thresholdEnabled": threshold_enabled,
        "featureFlag": feature_flag,
        "missing": missing,
    }
    return public_item, debug_item


# 阈值引用映射（合同 thresholds 键，仅未确认项传 None 引用但 thresholdEnabled=False）
_THRESHOLD_REF: dict[str, str | None] = {
    "T5_slope_ratio": "thresholds.t5_slope_ratio",
    "V3_avg_volume_ratio": "thresholds.v3_ratio",
    "S3_active_position": "thresholds.s3_position",
    "M3_aligned_momentum_delta": "thresholds.m3_zero_tolerance",
}


def compute_atomic_facts(
    structural_payload: dict[str, Any] | None,
    temporal_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """纯函数：从 payload 只读计算 14 Core + 10 Auxiliary 原子事实。

    Returns:
        {
          "core": {dimension: [public_item, ...]},  # 仅非缺失项（缺失直接省略）
          "auxiliary": [public_item, ...],           # 仅非缺失 + flag 开启项
          "availability": {
            coreDenominator, corePresent, coreMissing(publicKey),
            auxiliaryAvailable, auxiliaryHidden, v1Present, rejectedPresent,
            warnings  # 数据质量异常（如 m5_inconsistent）
          },
          "debug": [debug_item, ...],                # 管理员可追溯（factId/sourcePath）
        }
    """
    sp = structural_payload or {}
    tp = temporal_payload or {}
    d = _extract_inputs(sp, tp)
    dsa_dir = d["dsa_dir"]
    has_dir = dsa_dir is not None and dsa_dir != 0.0

    core_items: dict[str, dict[str, Any]] = {}
    aux_items: dict[str, dict[str, Any]] = {}
    debug_items: list[dict[str, Any]] = []
    warnings: list[str] = []

    # ---------------- Core ----------------
    # T1 主趋势方向
    t1_cat = {1.0: "UP", -1.0: "DOWN", 0.0: "NONE", None: "NONE"}.get(dsa_dir)
    t1_missing = dsa_dir is None
    p, dbg = _emit(
        fact_id="T1_trend_direction",
        public_key=CORE_PUBLIC_KEY["T1_trend_direction"],
        label=CORE_PUBLIC_LABEL["T1_trend_direction"],
        dimension="trend",
        visual_kind="category",
        value=dsa_dir,
        value_text=f"主趋势方向为{_DIR_ZH.get(dsa_dir, '中性')}",
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_dir",
        category_code=t1_cat,
        category_label=_DIR_ZH.get(dsa_dir, "中性"),
        missing=t1_missing,
    )
    core_items["T1_trend_direction"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # T2 沿主趋势运行速度
    t2_missing = not (has_dir and d["cur_slope_atr"] is not None)
    t2_val = (dsa_dir * d["cur_slope_atr"]) if not t2_missing else None
    p, dbg = _emit(
        fact_id="T2_aligned_slope",
        public_key=CORE_PUBLIC_KEY["T2_aligned_slope"],
        label=CORE_PUBLIC_LABEL["T2_aligned_slope"],
        dimension="trend",
        visual_kind="value",
        value=t2_val,
        value_text=f"沿主趋势运行速度为 {t2_val:.4f}" if t2_val is not None else "沿主趋势运行速度数据不足",
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_slope_atr_per_bar",
        secondary_text="每根日K",
        missing=t2_missing,
    )
    core_items["T2_aligned_slope"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # T4 本轮趋势持续时间
    t4_age = _safe_int(d["cur_age_bars"])
    t4_missing = t4_age is None
    p, dbg = _emit(
        fact_id="T4_trend_age",
        public_key=CORE_PUBLIC_KEY["T4_trend_age"],
        label=CORE_PUBLIC_LABEL["T4_trend_age"],
        dimension="trend",
        visual_kind="value",
        value=t4_age,
        value_text=f"本轮趋势已持续 {t4_age} 根" if t4_age is not None else "本轮趋势持续时间数据不足",
        source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_age_bars",
        secondary_text="根日K",
        missing=t4_missing,
    )
    core_items["T4_trend_age"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # T5 本轮·上一轮速度比（阈值未确认 → 仅比值 + 分类未启用）
    t5_missing = not (
        d["cur_slope_atr"] is not None
        and d["prev_slope_atr"] is not None
        and d["prev_slope_atr"] != 0
    )
    t5_ratio = None
    if not t5_missing:
        t5_ratio = abs(d["cur_slope_atr"]) / abs(d["prev_slope_atr"])
    p, dbg = _emit(
        fact_id="T5_slope_ratio",
        public_key=CORE_PUBLIC_KEY["T5_slope_ratio"],
        label=CORE_PUBLIC_LABEL["T5_slope_ratio"],
        dimension="trend",
        visual_kind="ratio",
        value=t5_ratio,
        value_text=(
            f"本轮·上一轮速度比为 {t5_ratio:.4f}（分类未启用）"
            if t5_ratio is not None
            else "本轮·上一轮速度比数据不足"
        ),
        source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_slope_atr_per_bar",
        threshold_ref=_THRESHOLD_REF["T5_slope_ratio"],
        threshold_enabled=False,
        missing=t5_missing,
    )
    core_items["T5_slope_ratio"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # M1 推动力与主趋势关系
    m1 = _momentum_alignment(d["sqzmom_val"], dsa_dir)
    m1_missing = m1 is None
    p, dbg = _emit(
        fact_id="M1_momentum_alignment",
        public_key=CORE_PUBLIC_KEY["M1_momentum_alignment"],
        label=CORE_PUBLIC_LABEL["M1_momentum_alignment"],
        dimension="momentum",
        visual_kind="relation",
        value=None,
        value_text=f"推动力与主趋势{_ALIGN_ZH.get(m1, '中性')}",
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_val",
        category_code=m1,
        category_label=_ALIGN_ZH.get(m1, "中性"),
        missing=m1_missing,
    )
    core_items["M1_momentum_alignment"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # M2 沿主趋势推动力
    m2_missing = not (has_dir and d["sqzmom_val"] is not None)
    m2_val = (dsa_dir * d["sqzmom_val"]) if not m2_missing else None
    p, dbg = _emit(
        fact_id="M2_aligned_momentum",
        public_key=CORE_PUBLIC_KEY["M2_aligned_momentum"],
        label=CORE_PUBLIC_LABEL["M2_aligned_momentum"],
        dimension="momentum",
        visual_kind="value",
        value=m2_val,
        value_text=f"沿主趋势推动力为 {m2_val:.4f}" if m2_val is not None else "沿主趋势推动力数据不足",
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_val",
        missing=m2_missing,
    )
    core_items["M2_aligned_momentum"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # M3 最近一根日K推动力变化（仅正/负/精确零；不硬编码容差）
    m3_missing = not (has_dir and d["sqzmom_delta_1"] is not None)
    m3_raw = (dsa_dir * d["sqzmom_delta_1"]) if not m3_missing else None
    m3_cat = _m3_category(m3_raw)
    p, dbg = _emit(
        fact_id="M3_aligned_momentum_delta",
        public_key=CORE_PUBLIC_KEY["M3_aligned_momentum_delta"],
        label=CORE_PUBLIC_LABEL["M3_aligned_momentum_delta"],
        dimension="momentum",
        visual_kind="category",
        value=m3_raw,
        value_text=(
            f"最近一根日K推动力变化：{_M3_ZH.get(m3_cat, '数据不足')}"
            if m3_raw is not None
            else "最近一根日K推动力变化数据不足"
        ),
        source_path="structural_payload.primary.1d.volatility_momentum.sqzmom_delta_1",
        threshold_ref=_THRESHOLD_REF["M3_aligned_momentum_delta"],
        threshold_enabled=False,
        category_code=m3_cat,
        category_label=_M3_ZH.get(m3_cat, "数据不足"),
        missing=m3_missing,
    )
    core_items["M3_aligned_momentum_delta"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # M5 波动收紧状态（双 true → 缺失 + 数据质量异常）
    m5 = _squeeze_state(d["sqz_on"], d["sqz_off"])
    m5_inconsistent = m5 == "INCONSISTENT"
    m5_missing = m5 is None or m5_inconsistent
    if m5_inconsistent:
        warnings.append("m5_inconsistent")
    p, dbg = _emit(
        fact_id="M5_squeeze_state",
        public_key=CORE_PUBLIC_KEY["M5_squeeze_state"],
        label=CORE_PUBLIC_LABEL["M5_squeeze_state"],
        dimension="momentum",
        visual_kind="relation",
        value=None,
        value_text=f"波动收紧状态：{_SQZ_ZH.get(m5, '数据不足')}",
        source_path="structural_payload.primary.1d.volatility_momentum.sqz_on",
        category_code=m5,
        category_label=_SQZ_ZH.get(m5, "数据不足"),
        missing=m5_missing,
    )
    core_items["M5_squeeze_state"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S1 价格与已确认区间关系（仅白名单枚举，未知值缺失）
    s1 = _confirmed_boundary_relation(d["breakout_state"], dsa_dir)
    s1_missing = s1 is None
    p, dbg = _emit(
        fact_id="S1_confirmed_boundary_relation",
        public_key=CORE_PUBLIC_KEY["S1_confirmed_boundary_relation"],
        label=CORE_PUBLIC_LABEL["S1_confirmed_boundary_relation"],
        dimension="structure",
        visual_kind="relation",
        value=None,
        value_text=f"价格与已确认区间关系：{_BOUNDARY_ZH.get(s1, '结构数据不足')}",
        source_path="structural_payload.primary.1d.swing_position.confirmed_swing_breakout_state",
        category_code=s1,
        category_label=_BOUNDARY_ZH.get(s1, "结构数据不足"),
        missing=s1_missing,
    )
    core_items["S1_confirmed_boundary_relation"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S2 当前主要波段与主趋势关系
    s2 = _dir_relation(d["active_swing_dir"], dsa_dir)
    s2_missing = s2 is None
    p, dbg = _emit(
        fact_id="S2_active_dir_relation",
        public_key=CORE_PUBLIC_KEY["S2_active_dir_relation"],
        label=CORE_PUBLIC_LABEL["S2_active_dir_relation"],
        dimension="structure",
        visual_kind="relation",
        value=None,
        value_text=f"当前主要波段与主趋势{_DIRREL_ZH.get(s2, '数据不足')}",
        source_path="structural_payload.primary.1d.swing_position.active_swing_dir",
        category_code=s2,
        category_label=_DIRREL_ZH.get(s2, "数据不足"),
        missing=s2_missing,
    )
    core_items["S2_active_dir_relation"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S3 价格在当前主要波段的位置（0.33/0.67；越界缺失）
    s3 = _categorize_position(d["price_pos_active"])
    s3_missing = s3 is None
    p, dbg = _emit(
        fact_id="S3_active_position",
        public_key=CORE_PUBLIC_KEY["S3_active_position"],
        label=CORE_PUBLIC_LABEL["S3_active_position"],
        dimension="structure",
        visual_kind="position",
        value=d["price_pos_active"],
        value_text=(
            f"价格在当前主要波段位置：{_POS_ZH.get(s3, '数据不足')}"
            if s3 is not None
            else "价格在当前主要波段位置数据不足"
        ),
        source_path="structural_payload.primary.1d.swing_position.price_position_in_active_swing_0_1",
        threshold_ref=_THRESHOLD_REF["S3_active_position"],
        category_code=s3,
        category_label=_POS_ZH.get(s3, "数据不足"),
        missing=s3_missing,
    )
    core_items["S3_active_position"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S7 距顺主趋势确认边界（禁止负距离；动态 sourcePath）
    s7_missing = not has_dir
    if not s7_missing:
        if dsa_dir > 0:
            s7_val = d["dist_high_atr"]
            s7_path = "structural_payload.primary.1d.swing_position.distance_to_swing_high_atr"
        else:
            s7_val = d["dist_low_atr"]
            s7_path = "structural_payload.primary.1d.swing_position.distance_to_swing_low_atr"
    else:
        s7_val = None
        s7_path = "structural_payload.primary.1d.swing_position.distance_to_swing_high_atr"
    s7_missing = s7_missing or (s7_val is None)
    p, dbg = _emit(
        fact_id="S7_dist_favorable_boundary",
        public_key=CORE_PUBLIC_KEY["S7_dist_favorable_boundary"],
        label=CORE_PUBLIC_LABEL["S7_dist_favorable_boundary"],
        dimension="structure",
        visual_kind="distance",
        value=s7_val,
        value_text=_fmt_dist(s7_val) if s7_val is not None else "距顺主趋势确认边界数据不足",
        source_path=s7_path,
        unit="ATR",
        missing=s7_missing,
    )
    core_items["S7_dist_favorable_boundary"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S8 距逆主趋势确认边界（禁止负距离；动态 sourcePath）
    s8_missing = not has_dir
    if not s8_missing:
        if dsa_dir > 0:
            s8_val = d["dist_low_atr"]
            s8_path = "structural_payload.primary.1d.swing_position.distance_to_swing_low_atr"
        else:
            s8_val = d["dist_high_atr"]
            s8_path = "structural_payload.primary.1d.swing_position.distance_to_swing_high_atr"
    else:
        s8_val = None
        s8_path = "structural_payload.primary.1d.swing_position.distance_to_swing_low_atr"
    s8_missing = s8_missing or (s8_val is None)
    p, dbg = _emit(
        fact_id="S8_dist_adverse_boundary",
        public_key=CORE_PUBLIC_KEY["S8_dist_adverse_boundary"],
        label=CORE_PUBLIC_LABEL["S8_dist_adverse_boundary"],
        dimension="structure",
        visual_kind="distance",
        value=s8_val,
        value_text=_fmt_dist(s8_val) if s8_val is not None else "距逆主趋势确认边界数据不足",
        source_path=s8_path,
        unit="ATR",
        missing=s8_missing,
    )
    core_items["S8_dist_adverse_boundary"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # V3 本轮·上一轮每根日K平均成交量比（阈值未确认 → 仅比值 + 分类未启用）
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
    p, dbg = _emit(
        fact_id="V3_avg_volume_ratio",
        public_key=CORE_PUBLIC_KEY["V3_avg_volume_ratio"],
        label=CORE_PUBLIC_LABEL["V3_avg_volume_ratio"],
        dimension="volume",
        visual_kind="ratio",
        value=v3_ratio,
        value_text=(
            f"本轮·上一轮每根日K平均成交量比为 {v3_ratio:.4f}（分类未启用）"
            if v3_ratio is not None
            else "本轮·上一轮每根日K平均成交量比数据不足"
        ),
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_volume_sum",
        threshold_ref=_THRESHOLD_REF["V3_avg_volume_ratio"],
        threshold_enabled=False,
        missing=v3_missing,
    )
    core_items["V3_avg_volume_ratio"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # ---------------- Auxiliary（默认隐藏，T3/T6 flag 关时不计算） ----------------
    # T3 趋势效率（flag 关 → 不含）
    if FEATURE_FLAGS.get("T3_trend_efficiency", False):
        t3_missing = d["cur_efficiency"] is None
        p, dbg = _emit(
            fact_id="T3_trend_efficiency",
            public_key="trend_efficiency",
            label="趋势效率",
            dimension="trend",
            visual_kind="value",
            value=d["cur_efficiency"],
            value_text=(
                f"趋势效率为 {d['cur_efficiency']:.4f}"
                if d["cur_efficiency"] is not None
                else "趋势效率数据不足"
            ),
            source_path="structural_payload.primary.1d.dsa_segment.current_dsa_segment_efficiency_0_1",
            feature_flag=False,
            missing=t3_missing,
        )
        aux_items["T3_trend_efficiency"] = p  # type: ignore[assignment]
        debug_items.append(dbg)

    # T6 效率差（flag 关 → 不含）
    if FEATURE_FLAGS.get("T6_efficiency_delta", False):
        t6_delta = None
        if d["cur_efficiency"] is not None and d["prev_efficiency"] is not None:
            t6_delta = d["cur_efficiency"] - d["prev_efficiency"]
        t6_missing = t6_delta is None
        p, dbg = _emit(
            fact_id="T6_efficiency_delta",
            public_key="efficiency_delta",
            label="效率变化",
            dimension="trend",
            visual_kind="value",
            value=t6_delta,
            value_text=(
                f"效率变化为 {t6_delta:.4f}"
                if t6_delta is not None
                else "效率变化数据不足"
            ),
            source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_efficiency_0_1",
            feature_flag=False,
            missing=t6_missing,
        )
        aux_items["T6_efficiency_delta"] = p  # type: ignore[assignment]
        debug_items.append(dbg)

    # M4 段内动量变化
    m4_missing = not (has_dir and d["daily_sqzmom_change"] is not None)
    m4_val = (dsa_dir * d["daily_sqzmom_change"]) if not m4_missing else None
    p, dbg = _emit(
        fact_id="M4_segment_momentum_change",
        public_key=AUX_PUBLIC_KEY["M4_segment_momentum_change"],
        label=AUX_PUBLIC_LABEL["M4_segment_momentum_change"],
        dimension="momentum",
        visual_kind="value",
        value=m4_val,
        value_text=f"段内动量变化为 {m4_val:.4f}" if m4_val is not None else "段内动量变化数据不足",
        source_path="temporal_payload.daily_context.daily_sqzmom_change_since_segment_start",
        feature_flag=False,
        missing=m4_missing,
    )
    aux_items["M4_segment_momentum_change"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S4 形成中波段方向关系
    s4 = _dir_relation(d["developing_swing_dir"], dsa_dir)
    s4_missing = s4 is None
    p, dbg = _emit(
        fact_id="S4_developing_dir_relation",
        public_key=AUX_PUBLIC_KEY["S4_developing_dir_relation"],
        label=AUX_PUBLIC_LABEL["S4_developing_dir_relation"],
        dimension="structure",
        visual_kind="relation",
        value=None,
        value_text=f"形成中波段与主趋势{_DIRREL_ZH.get(s4, '数据不足')}",
        source_path="structural_payload.primary.1d.swing_position.developing_swing_dir",
        category_code=s4,
        category_label=_DIRREL_ZH.get(s4, "数据不足"),
        feature_flag=False,
        missing=s4_missing,
    )
    aux_items["S4_developing_dir_relation"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S5 主波段与形成中波段方向关系
    s5 = _active_vs_developing(d["active_swing_dir"], d["developing_swing_dir"])
    s5_missing = s5 is None
    p, dbg = _emit(
        fact_id="S5_active_vs_developing",
        public_key=AUX_PUBLIC_KEY["S5_active_vs_developing"],
        label=AUX_PUBLIC_LABEL["S5_active_vs_developing"],
        dimension="structure",
        visual_kind="relation",
        value=None,
        value_text=f"主波段与形成中波段{_ADV_ZH.get(s5, '数据不足')}",
        source_path="structural_payload.primary.1d.swing_position.developing_swing_dir",
        category_code=s5,
        category_label=_ADV_ZH.get(s5, "数据不足"),
        feature_flag=False,
        missing=s5_missing,
    )
    aux_items["S5_active_vs_developing"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # S6 价格在形成中波段的位置
    s6 = _categorize_position(d["price_pos_developing"])
    s6_missing = s6 is None
    p, dbg = _emit(
        fact_id="S6_developing_position",
        public_key=AUX_PUBLIC_KEY["S6_developing_position"],
        label=AUX_PUBLIC_LABEL["S6_developing_position"],
        dimension="structure",
        visual_kind="position",
        value=d["price_pos_developing"],
        value_text=(
            f"价格在形成中波段位置：{_POS_ZH.get(s6, '数据不足')}"
            if s6 is not None
            else "价格在形成中波段位置数据不足"
        ),
        source_path="structural_payload.primary.1d.swing_position.price_position_in_developing_swing_0_1",
        category_code=s6,
        category_label=_POS_ZH.get(s6, "数据不足"),
        feature_flag=False,
        missing=s6_missing,
    )
    aux_items["S6_developing_position"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # V2 本轮每根日K平均成交量
    _cur_age = _safe_int(d["cur_age_bars"])
    v2_missing = not (d["cur_vol_sum"] is not None and _cur_age is not None and _cur_age > 0)
    v2_val = (d["cur_vol_sum"] / _cur_age) if (_cur_age is not None and not v2_missing) else None
    p, dbg = _emit(
        fact_id="V2_current_avg_volume",
        public_key=AUX_PUBLIC_KEY["V2_current_avg_volume"],
        label=AUX_PUBLIC_LABEL["V2_current_avg_volume"],
        dimension="volume",
        visual_kind="value",
        value=v2_val,
        value_text=f"本轮每根日K平均成交量为 {v2_val:.2f}" if v2_val is not None else "本轮每根日K平均成交量数据不足",
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_volume_sum",
        feature_flag=False,
        missing=v2_missing,
    )
    aux_items["V2_current_avg_volume"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # V4 本轮·上一轮持续时间比
    _cur_age = _safe_int(d["cur_age_bars"])
    _prev_age = _safe_int(d["prev_age_bars"])
    v4_missing = not (_cur_age is not None and _prev_age is not None and _prev_age > 0)
    v4_val = (
        _cur_age / _prev_age
    ) if (_cur_age is not None and _prev_age is not None and not v4_missing) else None
    p, dbg = _emit(
        fact_id="V4_age_ratio_raw",
        public_key=AUX_PUBLIC_KEY["V4_age_ratio_raw"],
        label=AUX_PUBLIC_LABEL["V4_age_ratio_raw"],
        dimension="volume",
        visual_kind="ratio",
        value=v4_val,
        value_text=f"本轮·上一轮持续时间比为 {v4_val:.4f}" if v4_val is not None else "本轮·上一轮持续时间比数据不足",
        source_path="structural_payload.primary.1d.dsa_segment.prev_dsa_segment_age_bars",
        feature_flag=False,
        missing=v4_missing,
    )
    aux_items["V4_age_ratio_raw"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # V5 本轮收益率与成交量比
    v5_missing = d["current_segment_return_per_volume"] is None
    p, dbg = _emit(
        fact_id="V5_return_per_volume",
        public_key=AUX_PUBLIC_KEY["V5_return_per_volume"],
        label=AUX_PUBLIC_LABEL["V5_return_per_volume"],
        dimension="volume",
        visual_kind="value",
        value=d["current_segment_return_per_volume"],
        value_text=(
            f"本轮收益率与成交量比为 {d['current_segment_return_per_volume']:.6f}"
            if d["current_segment_return_per_volume"] is not None
            else "本轮收益率与成交量比数据不足"
        ),
        source_path="structural_payload.primary.1d.dsa_segment.current_segment_return_per_volume",
        feature_flag=False,
        missing=v5_missing,
    )
    aux_items["V5_return_per_volume"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # V5_ratio 本轮收益率与量比
    v5r_missing = d["return_per_volume_ratio"] is None
    p, dbg = _emit(
        fact_id="V5_return_per_volume_ratio",
        public_key=AUX_PUBLIC_KEY["V5_return_per_volume_ratio"],
        label=AUX_PUBLIC_LABEL["V5_return_per_volume_ratio"],
        dimension="volume",
        visual_kind="ratio",
        value=d["return_per_volume_ratio"],
        value_text=(
            f"本轮收益率与量比为 {d['return_per_volume_ratio']:.4f}"
            if d["return_per_volume_ratio"] is not None
            else "本轮收益率与量比数据不足"
        ),
        source_path="structural_payload.primary.1d.dsa_segment.return_per_volume_ratio",
        feature_flag=False,
        missing=v5r_missing,
    )
    aux_items["V5_return_per_volume_ratio"] = p  # type: ignore[assignment]
    debug_items.append(dbg)

    # ---------------- 分组（固定四组顺序，仅非缺失项） ----------------
    core_grouped: dict[str, list[dict[str, Any]]] = {dim: [] for dim in _DIMENSION_ORDER}
    for fid in CORE_FACT_IDS:
        item = core_items[fid]
        if item is not None:  # 缺失项直接省略
            core_grouped[item["dimension"]].append(item)

    auxiliary_list = [aux_items[fid] for fid in AUX_FACT_IDS if fid in aux_items and aux_items[fid] is not None]

    # ---------------- 可用性 ----------------
    core_present = sum(1 for it in core_grouped.values() for _ in it)
    core_missing = [CORE_PUBLIC_KEY[fid] for fid in CORE_FACT_IDS if core_items[fid] is None]
    aux_available = [AUX_PUBLIC_KEY[fid] for fid in AUX_FACT_IDS if aux_items.get(fid) is not None]
    # 默认隐藏：响应中实际返回的 Auxiliary publicKey（全部默认隐藏，不在用户 UI 展示）
    aux_hidden = [it["publicKey"] for it in auxiliary_list]

    availability = {
        "coreDenominator": len(CORE_FACT_IDS),
        "corePresent": core_present,
        "coreMissing": core_missing,
        "auxiliaryAvailable": aux_available,
        "auxiliaryHidden": aux_hidden,
        "v1Present": False,
        "rejectedPresent": False,
        "warnings": warnings,
    }

    return {
        "core": core_grouped,
        "auxiliary": auxiliary_list,
        "availability": availability,
        "debug": debug_items,
    }


# T3/T6 效率 fact flag：默认关闭（EFF-001/EFF-002 未修复前，普通用户完全不显示）。
FEATURE_FLAGS: dict[str, bool] = {
    "T3_trend_efficiency": False,
    "T6_efficiency_delta": False,
}


# ---------------------------------------------------------------------------
# 近期变化（一次查询 ≤10 快照，只读计算，不写 stock_state_events）
# ---------------------------------------------------------------------------


def _quantize_value(v: float | None) -> float | None:
    """按公开显示精度量化（4 位小数），避免 float 精确不等制造噪声。"""
    if v is None:
        return None
    return round(v, 4)


def compute_recent_changes(
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 ≤10 个已发布兼容快照（按 trade_date 升序）只读计算 Core 事实变化。

    比较按各事实公开显示精度（value 量化 4 位小数 + categoryLabel），
    返回 fromText / toText / deltaText，明确非 Core 且不解释利好利空。

    Args:
        snapshots: 每个元素 {trade_date: str(ISO), structural_payload, temporal_payload}
                   必须已是升序（调用方按 trade_date 排序）。

    Returns:
        变化记录列表（按时间升序），每条：
        {publicKey, dimension, fromText, toText, deltaText, asOf}
    """
    if len(snapshots) < 2:
        return []

    def _text_of(result: dict[str, Any], fid: str) -> tuple[str | None, str | None]:
        """返回 (value_text_or_None, category_label_or_None)。"""
        for _dim, items in result["core"].items():
            for it in items:
                if it["publicKey"] == CORE_PUBLIC_KEY[fid]:
                    return it.get("valueText"), it.get("categoryLabel")
        return None, None

    changes: list[dict[str, Any]] = []
    # 预计算每个快照的事实结果（只读计算）
    results = [
        compute_atomic_facts(s.get("structural_payload"), s.get("temporal_payload"))
        for s in snapshots
    ]

    def _texts_of(result: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
        return {fid: _text_of(result, fid) for fid in CORE_FACT_IDS}

    def _values_of(result: dict[str, Any]) -> dict[str, float | None]:
        return {fid: _value_of(result, fid) for fid in CORE_FACT_IDS}

    prev_texts = _texts_of(results[0])
    prev_values = _values_of(results[0])
    for idx in range(1, len(results)):
        cur = results[idx]
        cur_texts = _texts_of(cur)
        cur_values = _values_of(cur)
        for fid in CORE_FACT_IDS:
            p_text, p_cat = prev_texts[fid]
            c_text, c_cat = cur_texts[fid]
            p_qv = _quantize_value(prev_values[fid])
            c_qv = _quantize_value(cur_values[fid])
            if (p_text, p_cat, p_qv) == (c_text, c_cat, c_qv):
                continue
            # deltaText 仅描述变化类型，不解释利好利空
            if p_cat is not None or c_cat is not None:
                delta_text = "分类调整"
            elif p_qv is not None or c_qv is not None:
                delta_text = "数值变动"
            else:
                delta_text = "状态更新"
            changes.append({
                "publicKey": CORE_PUBLIC_KEY[fid],
                "dimension": _dim_of(cur, fid),
                "fromText": p_text or "—",
                "toText": c_text or "—",
                "deltaText": delta_text,
                "asOf": snapshots[idx]["trade_date"],
            })
        prev_texts = cur_texts
        prev_values = cur_values

    # 限制体积（最多 30 条，避免超大 payload）
    if len(changes) > 30:
        changes = changes[-30:]
    return changes


def _value_of(result: dict[str, Any], fid: str) -> float | None:
    for _dim, items in result["core"].items():
        for it in items:
            if it["publicKey"] == CORE_PUBLIC_KEY[fid]:
                return it.get("value")
    return None


def _dim_of(result: dict[str, Any], fid: str) -> str:
    for _dim, items in result["core"].items():
        for it in items:
            if it["publicKey"] == CORE_PUBLIC_KEY[fid]:
                return _dim
    return "trend"


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
        for _dim, items in res["core"].items():
            for it in items:
                if it["publicKey"] == CORE_PUBLIC_KEY[fid]:
                    return it
        raise AssertionError(fid)

    # 计数严格 14/10/1
    assert len(CORE_FACT_IDS) == 14
    assert len(AUX_FACT_IDS) == 10
    assert len(REJECTED_FACT_IDS) == 1

    # 用户项无 factId / sourcePath / missing / hiddenByDefault
    for _dim, items in res["core"].items():
        for it in items:
            assert "factId" not in it
            assert "sourcePath" not in it
            assert "missing" not in it
            assert "hiddenByDefault" not in it
            assert it["valueText"]
            assert it["label"]

    # S3 0.63 → 中间
    s3 = _get("S3_active_position")
    assert s3["categoryLabel"] == "中间", f"0.63 必须映射中间，实际 {s3['categoryLabel']}"

    # S7 顺向（dsa_dir>0 → dist_high=2.5）：尚未到达；S8 逆向（dist_low=-1.2）：已越过
    s7 = _get("S7_dist_favorable_boundary")
    assert "尚未到达" in s7["valueText"], s7["valueText"]
    s8 = _get("S8_dist_adverse_boundary")
    assert "已越过" in s8["valueText"], s8["valueText"]

    # T2/M2/M3 真实值
    t2 = _get("T2_aligned_slope")
    assert abs(t2["value"] - 0.0123) < 1e-9, t2["value"]
    m2 = _get("M2_aligned_momentum")
    assert abs(m2["value"] - 0.002) < 1e-9, m2["value"]
    m3 = _get("M3_aligned_momentum_delta")
    assert abs(m3["value"] - 0.0003) < 1e-9, m3["value"]
    assert m3["categoryLabel"] == "增加"

    # M3 无硬编码容差：raw 极小正数仍判"增加"
    sp2 = json.loads(json.dumps(sample_sp))
    sp2["primary"]["1d"]["volatility_momentum"]["sqzmom_delta_1"] = 1e-12
    r2 = compute_atomic_facts(sp2, sample_tp)
    m3b = None
    for _dim, items in r2["core"].items():
        for it in items:
            if it["publicKey"] == CORE_PUBLIC_KEY["M3_aligned_momentum_delta"]:
                m3b = it
    assert m3b is not None
    assert m3b["categoryLabel"] == "增加", "1e-12 不应被容差吞掉，应判增加"

    # T5/V3 分类未启用
    t5 = _get("T5_slope_ratio")
    assert t5["thresholdEnabled"] is False
    assert "分类未启用" in t5["valueText"]
    v3 = _get("V3_avg_volume_ratio")
    assert v3["thresholdEnabled"] is False
    assert "分类未启用" in v3["valueText"]

    # V1 永不出现
    flat = json.dumps(res, ensure_ascii=False)
    assert "V1_cumulative_volume_ratio" not in flat
    assert res["availability"]["v1Present"] is False

    # T3/T6 默认隐藏（flag 关 → 不在 auxiliary 中，也不出现在 auxiliaryHidden）
    aux_keys = [it["publicKey"] for it in res["auxiliary"]]
    assert "trend_efficiency" not in aux_keys
    assert "efficiency_delta" not in aux_keys
    assert "trend_efficiency" not in res["availability"]["auxiliaryHidden"]
    assert "efficiency_delta" not in res["availability"]["auxiliaryHidden"]
    assert set(res["availability"]["auxiliaryHidden"]) == set(aux_keys)
    assert len(aux_keys) == 8  # 10 aux - T3 - T6

    # 分母固定 14；core 仅含非缺失项
    assert res["availability"]["coreDenominator"] == 14

    # 文案无内部术语
    for _dim, items in res["core"].items():
        for it in items:
            for bad in ("DSA", "SQZMOM", "Segment", "Active Swing", "raw=", "bar"):
                assert bad not in it["valueText"], f"{it['publicKey']} 含内部术语: {it['valueText']}"
            assert bad not in it["label"]

    # 普通用户项无内部术语于 label
    for _dim, items in res["core"].items():
        for it in items:
            for bad in ("DSA", "SQZMOM", "Segment", "Active", "Developing"):
                assert bad not in it["label"], it["label"]

    print("OK: compute_atomic_facts 验证通过")
