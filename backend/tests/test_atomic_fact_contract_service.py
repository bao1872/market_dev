"""Atomic Fact Contract V1 - 纯函数/服务级单元测试（无 DB，快速）。

覆盖 PROMPT 一/三 硬性验证：
1. Registry 数量严格 14/10/1、顺序和 ID 唯一
2. V1 永久缺席（不进 any fact、不进 availability）
3. T3/T6 默认隐藏（feature flag 关 → 不在 auxiliary）
4. T2/M2/M3 真实原始值（不伪造成 [-1,1]）
5. T5/V3 阈值未确认 → 仅比值 + 分类未启用
6. M5 三态；双 true → 缺失 + 数据质量异常 warning
7. S3 边界（0.33/0.67；0.63 → 中间；越界缺失）
8. S7/S8 非负距离展示；动态 sourcePath（随 dsa_dir 选 high/low）
9. S1 仅白名单枚举，未知值缺失（不默认区间内）
10. M3 零值判定无硬编码容差（1e-12 仍判增加）
11. 缺失 Core 直接从用户 core 数组省略；availability.coreMissing 用 publicKey
12. 用户项无 factId / sourcePath / missing / hiddenByDefault 泄露
13. 用户文案无内部术语（DSA/SQZMOM/Segment/Active/Developing/bar/raw）
14. compact 含完整 14 项且 S2 存在；分母固定 14
15. persisted 与 fallback 同一纯函数结果确定性一致
16. recentChanges 按展示精度比较，无 float 噪声；返回 fromText/toText/deltaText

用法：
    cd backend && pytest tests/test_atomic_fact_contract_service.py -v
"""

from __future__ import annotations

import json

import pytest

from app.schemas.atomic_fact_contract import AtomicFactsContextResponse, AtomicFactsMeta
from app.schemas.stock_state import StockContextDataQuality
from app.services import atomic_fact_contract_service as svc
from app.services.atomic_fact_contract_service import (
    AUX_FACT_IDS,
    CORE_FACT_IDS,
    CORE_PUBLIC_KEY,
    REJECTED_FACT_IDS,
    compute_atomic_fact_debug,
    compute_atomic_facts,
    compute_recent_changes,
)

# ---------------------------------------------------------------------------
# 构造 payload 辅助
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    """构造完整 structural_payload.primary.1d + temporal_payload。"""
    dsa = {
        "current_dsa_segment_dir": 1,
        "current_dsa_segment_slope_atr_per_bar": 0.0123,
        "prev_dsa_segment_slope_atr_per_bar": 0.0100,
        "current_dsa_segment_age_bars": 12,
        "prev_dsa_segment_age_bars": 10,
        "current_segment_volume_sum": 1_200_000.0,
        "prev_segment_volume_sum": 900_000.0,
        "current_dsa_segment_efficiency_0_1": 0.70,
        "prev_dsa_segment_efficiency_0_1": 0.60,
        "current_segment_return_per_volume": 0.000123,
        "return_per_volume_ratio": 0.50,
        "current_vs_prev_volume_ratio": 1.33,  # V1 拒绝项，永不作为事实
    }
    dsa.update(overrides.pop("dsa", {}))
    vol = {
        "sqzmom_val": 0.002,
        "sqzmom_delta_1": 0.0003,
        "sqz_on": False,
        "sqz_off": True,
    }
    vol.update(overrides.pop("vol", {}))
    swing = {
        "confirmed_swing_breakout_state": "inside",
        "active_swing_dir": 1,
        "developing_swing_dir": 1,
        "price_position_in_active_swing_0_1": 0.63,
        "price_position_in_developing_swing_0_1": 0.50,
        "distance_to_swing_high_atr": 2.5,
        "distance_to_swing_low_atr": -1.2,
    }
    swing.update(overrides.pop("swing", {}))
    sp = {
        "primary": {
            "1d": {
                "dsa_segment": dsa,
                "volatility_momentum": vol,
                "swing_position": swing,
            }
        }
    }
    tp = {"daily_context": {"daily_sqzmom_change_since_segment_start": 0.001}}
    sp.update(overrides.get("sp_extra", {}))
    return sp, tp


def _find_core(res: dict, public_key: str) -> dict:
    for items in res["core"].values():
        for it in items:
            if it["publicKey"] == public_key:
                return it
    raise AssertionError(f"core fact {public_key} not found (缺失未省略?)")


def _core_present(res: dict, public_key: str) -> bool:
    try:
        _find_core(res, public_key)
        return True
    except AssertionError:
        return False


def _debug_map(sp: dict, tp: dict) -> dict[str, dict]:
    # 公开结果不再含 debug；管理员 debug 由 compute_atomic_fact_debug 即时生成
    return {d["factId"]: d for d in compute_atomic_fact_debug(sp, tp)}


# ---------------------------------------------------------------------------
# 1. Registry 14/10/1
# ---------------------------------------------------------------------------


def test_registry_counts_and_order():
    assert len(CORE_FACT_IDS) == 14
    assert len(AUX_FACT_IDS) == 10
    assert len(REJECTED_FACT_IDS) == 1
    assert CORE_FACT_IDS == [
        "T1_trend_direction", "T2_aligned_slope", "T4_trend_age", "T5_slope_ratio",
        "M1_momentum_alignment", "M2_aligned_momentum", "M3_aligned_momentum_delta",
        "M5_squeeze_state", "S1_confirmed_boundary_relation", "S2_active_dir_relation",
        "S3_active_position", "S7_dist_favorable_boundary", "S8_dist_adverse_boundary",
        "V3_avg_volume_ratio",
    ]
    all_ids = set(CORE_FACT_IDS) | set(AUX_FACT_IDS) | set(REJECTED_FACT_IDS)
    assert len(all_ids) == 25
    # public_key 唯一
    assert len(set(CORE_PUBLIC_KEY.values())) == 14


# ---------------------------------------------------------------------------
# 2. V1 永久缺席
# ---------------------------------------------------------------------------


def test_v1_permanently_absent():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    flat = json.dumps(res, ensure_ascii=False)
    assert "V1_cumulative_volume_ratio" not in flat
    assert "V1_cumulative" not in flat
    assert res["availability"]["v1Present"] is False
    assert res["availability"]["rejectedPresent"] is False
    aux_keys = [it["publicKey"] for it in res["auxiliary"]]
    assert "V1_cumulative_volume_ratio" not in aux_keys


# ---------------------------------------------------------------------------
# 3. T3/T6 默认隐藏
# ---------------------------------------------------------------------------


def test_t3_t6_default_hidden():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    aux_keys = [it["publicKey"] for it in res["auxiliary"]]
    assert "trend_efficiency" not in aux_keys
    assert "efficiency_delta" not in aux_keys
    assert "trend_efficiency" not in res["availability"]["auxiliaryHidden"]
    assert "efficiency_delta" not in res["availability"]["auxiliaryHidden"]
    assert set(res["availability"]["auxiliaryHidden"]) == set(aux_keys)
    assert len(aux_keys) == 8  # 10 aux 减去 T3/T6


# ---------------------------------------------------------------------------
# 4. T2/M2/M3 真实原始值
# ---------------------------------------------------------------------------


def test_t2_m2_m3_real_values():
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1, "current_dsa_segment_slope_atr_per_bar": 0.0123},
        vol={"sqzmom_val": 0.002, "sqzmom_delta_1": 0.0003},
    )
    res = compute_atomic_facts(sp, tp)
    t2 = _find_core(res, "aligned_slope")
    assert abs(t2["value"] - 0.0123) < 1e-9
    assert t2["visualKind"] == "metric"
    m2 = _find_core(res, "aligned_momentum")
    assert abs(m2["value"] - 0.002) < 1e-9
    m3 = _find_core(res, "momentum_delta")
    assert abs(m3["value"] - 0.0003) < 1e-9
    assert m3["categoryLabel"] == "增加"


# ---------------------------------------------------------------------------
# 5. T5/V3 阈值未确认 → 仅比值 + 分类未启用
# ---------------------------------------------------------------------------


def test_t5_v3_threshold_disabled():
    sp, tp = _base_payload(
        dsa={
            "current_dsa_segment_slope_atr_per_bar": 0.0123,
            "prev_dsa_segment_slope_atr_per_bar": 0.0100,
            "current_segment_volume_sum": 1_200_000.0,
            "prev_segment_volume_sum": 900_000.0,
            "current_dsa_segment_age_bars": 12,
            "prev_dsa_segment_age_bars": 10,
        },
    )
    res = compute_atomic_facts(sp, tp)
    t5 = _find_core(res, "slope_ratio")
    assert t5["thresholdEnabled"] is False
    assert t5["secondaryText"] == "分类未启用", t5.get("secondaryText")
    assert t5["valueText"] == "1.23×", t5["valueText"]
    assert abs(t5["value"] - 1.23) < 1e-9  # 0.0123/0.0100
    v3 = _find_core(res, "volume_ratio")
    assert v3["thresholdEnabled"] is False
    assert v3["secondaryText"] == "分类未启用", v3.get("secondaryText")
    assert v3["valueText"] == "1.11×", v3["valueText"]
    assert abs(v3["value"] - (100000.0 / 90000.0)) < 1e-9


# ---------------------------------------------------------------------------
# 6. M5 三态；双 true → 缺失 + 数据质量异常 warning
# ---------------------------------------------------------------------------


def test_m5_states_and_inconsistent_missing():
    # ON
    sp, tp = _base_payload(vol={"sqz_on": True, "sqz_off": False})
    assert _find_core(compute_atomic_facts(sp, tp), "squeeze_state")["categoryLabel"] == "正在收紧"
    # OFF
    sp, tp = _base_payload(vol={"sqz_on": False, "sqz_off": True})
    assert _find_core(compute_atomic_facts(sp, tp), "squeeze_state")["categoryLabel"] == "正在释放"
    # NORMAL
    sp, tp = _base_payload(vol={"sqz_on": False, "sqz_off": False})
    assert _find_core(compute_atomic_facts(sp, tp), "squeeze_state")["categoryLabel"] == "正常"
    # INCONSISTENT（双 true → 缺失 + warning）
    sp, tp = _base_payload(vol={"sqz_on": True, "sqz_off": True})
    res = compute_atomic_facts(sp, tp)
    assert not _core_present(res, "squeeze_state")  # 缺失（不进入用户数组）
    assert "m5_inconsistent" in res["availability"]["warnings"]


@pytest.mark.parametrize(
    "sqz_on,sqz_off",
    [
        (None, False),
        (False, None),
        (None, True),
        (True, None),
    ],
)
def test_m5_single_side_missing(sqz_on, sqz_off):
    """任一输入缺失即缺失，禁止伪装 NORMAL，禁止进入 Core。"""
    sp, tp = _base_payload(vol={"sqz_on": sqz_on, "sqz_off": sqz_off})
    res = compute_atomic_facts(sp, tp)
    assert not _core_present(res, "squeeze_state")
    assert "squeeze_state" in res["availability"]["coreMissing"]
    # 单侧缺失不应触发 m5_inconsistent warning
    assert "m5_inconsistent" not in res["availability"]["warnings"]


# ---------------------------------------------------------------------------
# 7. S3 边界；越界缺失
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pos,expected_present,expected_label",
    [
        (0.10, True, "偏低"),
        (0.33, True, "中间"),
        (0.50, True, "中间"),
        (0.63, True, "中间"),  # 关键：0.63 → 中间
        (0.67, True, "中间"),
        (0.68, True, "偏高"),
        (0.95, True, "偏高"),
        (-0.2, False, None),   # 越界 → 缺失
        (1.5, False, None),    # 越界 → 缺失
    ],
)
def test_s3_boundary_and_out_of_range_missing(pos, expected_present, expected_label):
    sp, tp = _base_payload(swing={"price_position_in_active_swing_0_1": pos})
    res = compute_atomic_facts(sp, tp)
    if expected_present:
        s3 = _find_core(res, "active_position")
        assert s3["categoryLabel"] == expected_label
        assert "OUT_OF_RANGE" not in json.dumps(res, ensure_ascii=False)
    else:
        assert not _core_present(res, "active_position")
        assert "active_position" in res["availability"]["coreMissing"]


# ---------------------------------------------------------------------------
# 8. S7/S8 非负距离展示 + 动态 sourcePath
# ---------------------------------------------------------------------------


def test_s7_s8_no_negative_distance_and_dynamic_source_path():
    # dsa_dir>0：S7=dist_high=2.5（正，尚未到达）；S8=dist_low=-1.2（负，已越过）
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1},
        swing={"distance_to_swing_high_atr": 2.5, "distance_to_swing_low_atr": -1.2},
    )
    res = compute_atomic_facts(sp, tp)
    s7 = _find_core(res, "dist_favorable")
    assert s7["categoryLabel"] == "尚未到达", s7["categoryLabel"]
    assert s7["valueText"] == "2.50 ATR", s7["valueText"]
    assert s7["value"] == 2.5
    s8 = _find_core(res, "dist_adverse")
    assert s8["categoryLabel"] == "已越过", s8["categoryLabel"]
    assert s8["valueText"] == "1.20 ATR", s8["valueText"]
    assert s8["value"] == -1.2  # 原始值保留符号（admin 可追溯），但展示文案不显示负距离
    assert "负" not in s8["valueText"]
    dbg = _debug_map(sp, tp)
    assert "distance_to_swing_high_atr" in dbg["S7_dist_favorable_boundary"]["sourcePath"]
    assert "distance_to_swing_low_atr" in dbg["S8_dist_adverse_boundary"]["sourcePath"]

    # dsa_dir<0：S7=dist_low=-1.2（负，已越过）；S8=dist_high=2.5（正，尚未到达）
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": -1},
        swing={"distance_to_swing_high_atr": 2.5, "distance_to_swing_low_atr": -1.2},
    )
    res = compute_atomic_facts(sp, tp)
    s7 = _find_core(res, "dist_favorable")
    assert s7["categoryLabel"] == "已越过", s7["categoryLabel"]
    s8 = _find_core(res, "dist_adverse")
    assert s8["categoryLabel"] == "尚未到达", s8["categoryLabel"]
    dbg = _debug_map(sp, tp)
    # 动态：dsa_dir<0 时 S7 选 low，S8 选 high
    assert "distance_to_swing_low_atr" in dbg["S7_dist_favorable_boundary"]["sourcePath"]
    assert "distance_to_swing_high_atr" in dbg["S8_dist_adverse_boundary"]["sourcePath"]


# ---------------------------------------------------------------------------
# 9. S1 仅白名单枚举，未知值缺失
# ---------------------------------------------------------------------------


def test_s1_whitelist_only_unknown_missing():
    sp, tp = _base_payload(swing={"confirmed_swing_breakout_state": "weird_unknown"})
    res = compute_atomic_facts(sp, tp)
    assert not _core_present(res, "boundary_relation")
    assert "boundary_relation" in res["availability"]["coreMissing"]
    # 白名单值正常
    sp, tp = _base_payload(swing={"confirmed_swing_breakout_state": "above_confirmed_high"})
    res = compute_atomic_facts(sp, tp)
    assert _core_present(res, "boundary_relation")


# ---------------------------------------------------------------------------
# 10. M3 无硬编码容差（1e-12 仍判增加，不吞掉）
# ---------------------------------------------------------------------------


def test_m3_no_hardcoded_tolerance():
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1},
        vol={"sqzmom_delta_1": 1e-12},
    )
    res = compute_atomic_facts(sp, tp)
    m3 = _find_core(res, "momentum_delta")
    assert m3["categoryLabel"] == "增加"
    # 精确零 → 基本不变
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1},
        vol={"sqzmom_delta_1": 0.0},
    )
    res = compute_atomic_facts(sp, tp)
    assert _find_core(res, "momentum_delta")["categoryLabel"] == "基本不变"


# ---------------------------------------------------------------------------
# 11. 缺失 Core 直接省略 + coreMissing 用 publicKey
# ---------------------------------------------------------------------------


def test_missing_core_omitted_and_listed_by_public_key():
    # T1 缺失（dsa_dir None）
    sp, tp = _base_payload(dsa={"current_dsa_segment_dir": None})
    res = compute_atomic_facts(sp, tp)
    assert not _core_present(res, "trend_direction")
    assert "trend_direction" in res["availability"]["coreMissing"]
    # 分母仍 14；缺失项直接从数组省略（corePresent < 14）
    assert res["availability"]["coreDenominator"] == 14
    assert res["availability"]["corePresent"] < 14


# ---------------------------------------------------------------------------
# 12. 用户项无 factId/sourcePath/missing/hiddenByDefault 泄露
# ---------------------------------------------------------------------------


def test_user_items_no_internal_leak():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    for _dim, items in res["core"].items():
        for it in items:
            assert "factId" not in it
            assert "sourcePath" not in it
            assert "missing" not in it
            assert "hiddenByDefault" not in it
    for it in res["auxiliary"]:
        assert "factId" not in it
        assert "sourcePath" not in it
        assert "missing" not in it


# ---------------------------------------------------------------------------
# 13. 用户文案无内部术语
# ---------------------------------------------------------------------------


FORBIDDEN_WORDS = [
    "DSA", "SQZMOM", "Segment", "Active", "Developing", "Active Swing",
    "bar", "raw", "raw=",
    "买入", "卖出", "加仓", "减仓", "止损", "安全", "买点", "卖点", "持仓",
    "趋势形成", "趋势反转", "成熟", "衰竭", "便宜", "昂贵",
    "突破方向", "放量", "缩量", "累计成交量比",
    "MACD", "布林", "关键证据",
]


def test_no_forbidden_words_and_no_legacy_status():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    # 仅校验普通用户文案（core/auxiliary 的展示字段），不含 admin debug 的 sourcePath
    for _dim, items in res["core"].items():
        for it in items:
            blob = " ".join(
                str(x) for x in (it.get("valueText"), it.get("label"),
                                 it.get("secondaryText"), it.get("categoryLabel"))
                if x is not None
            )
            for w in FORBIDDEN_WORDS:
                assert w not in blob, f"用户文案出现禁用词: {w} -> {blob}"
            for bad in ("DSA", "SQZMOM", "Segment", "Active", "Developing"):
                assert bad not in it["label"], it["label"]
    for it in res["auxiliary"]:
        blob = " ".join(
            str(x) for x in (it.get("valueText"), it.get("label"),
                             it.get("secondaryText"), it.get("categoryLabel"))
            if x is not None
        )
        for w in FORBIDDEN_WORDS:
            assert w not in blob, f"用户文案出现禁用词: {w} -> {blob}"
    assert "state" not in res
    assert "events" not in res


# ---------------------------------------------------------------------------
# 14. compact 完整 14 + S2 存在；分母固定 14
# ---------------------------------------------------------------------------


def test_compact_full_14_and_s2_present():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    all_core_keys = [it["publicKey"] for items in res["core"].values() for it in items]
    assert len(all_core_keys) == 14
    assert set(all_core_keys) == set(CORE_PUBLIC_KEY.values())
    assert "active_dir_relation" in all_core_keys
    assert res["availability"]["coreDenominator"] == 14
    assert res["availability"]["corePresent"] == 14
    assert res["availability"]["coreMissing"] == []


# ---------------------------------------------------------------------------
# 15. persisted 与 fallback 同一纯函数确定性一致
# ---------------------------------------------------------------------------


def test_persisted_equals_fallback_deterministic():
    sp, tp = _base_payload()
    fallback = compute_atomic_facts(sp, tp)
    persisted = compute_atomic_facts(sp, tp)
    assert json.dumps(fallback, ensure_ascii=False, sort_keys=True) == json.dumps(
        persisted, ensure_ascii=False, sort_keys=True
    )
    assert fallback["availability"] == persisted["availability"]


# ---------------------------------------------------------------------------
# 16. recentChanges 按展示精度比较，无 float 噪声；返回文本
# ---------------------------------------------------------------------------


def test_recent_changes_precision_and_text():
    # 仅 float 噪声（差 1e-9）不应产生变化
    sp_a, tp_a = _base_payload(
        dsa={"current_dsa_segment_slope_atr_per_bar": 0.012300001},
        vol={"sqzmom_val": 0.002},
    )
    sp_b, tp_b = _base_payload(
        dsa={"current_dsa_segment_slope_atr_per_bar": 0.012300002},
        vol={"sqzmom_val": 0.002},
    )
    snap_a = {"trade_date": "2026-07-10", "structural_payload": sp_a, "temporal_payload": tp_a}
    snap_b = {"trade_date": "2026-07-11", "structural_payload": sp_b, "temporal_payload": tp_b}
    changes = compute_recent_changes([snap_a, snap_b])
    # 差值仅 1e-9，量化到 4 位小数相同 → 不应有变化
    assert changes == []

    # 方向反转应产生变化（fromText/toText/deltaText 齐全）
    sp_c, tp_c = _base_payload(dsa={"current_dsa_segment_dir": 1})
    sp_d, tp_d = _base_payload(dsa={"current_dsa_segment_dir": -1})
    snap_c = {"trade_date": "2026-07-12", "structural_payload": sp_c, "temporal_payload": tp_c}
    snap_d = {"trade_date": "2026-07-14", "structural_payload": sp_d, "temporal_payload": tp_d}
    changes = compute_recent_changes([snap_c, snap_d])
    assert len(changes) > 0
    for c in changes:
        assert "fromText" in c and "toText" in c and "deltaText" in c
        assert "label" in c and c["label"], "recentChanges 必须含中文 label"
        assert c["asOf"] in ("2026-07-12", "2026-07-14")
    # 单快照无变化
    assert compute_recent_changes([snap_c]) == []


def test_recent_changes_per_fact_precision_boundaries():
    """各事实按自身 presentation valuePrecision 量化，禁止统一 round(..., 4)。

    - T5 valuePrecision=2：第 3、4 位差异不应产生变化
    - M3 valuePrecision=6：第 5、6 位真实差异必须保留（不被 4 位吞掉）
    """
    # T5 (slope_ratio) precision=2：1.231 与 1.232 量化到 2 位都为 1.23 → 无变化
    sp_a, tp_a = _base_payload(dsa={
        "current_dsa_segment_slope_atr_per_bar": 1.0,
        "prev_dsa_segment_slope_atr_per_bar": 0.8127,
    })
    sp_b, tp_b = _base_payload(dsa={
        "current_dsa_segment_slope_atr_per_bar": 1.0,
        "prev_dsa_segment_slope_atr_per_bar": 0.8128,
    })
    snap_a = {"trade_date": "2026-07-10", "structural_payload": sp_a, "temporal_payload": tp_a}
    snap_b = {"trade_date": "2026-07-11", "structural_payload": sp_b, "temporal_payload": tp_b}
    changes = compute_recent_changes([snap_a, snap_b])
    slope_changes = [c for c in changes if c["publicKey"] == "slope_ratio"]
    assert slope_changes == [], "T5 precision=2：1.231/1.232 量化相同不应产生变化"

    # M3 (momentum_delta) precision=6：1e-5 差异必须产生变化（4 位会吞掉）
    sp_c, tp_c = _base_payload(vol={"sqzmom_delta_1": 0.000100})
    sp_d, tp_d = _base_payload(vol={"sqzmom_delta_1": 0.000110})
    snap_c = {"trade_date": "2026-07-12", "structural_payload": sp_c, "temporal_payload": tp_c}
    snap_d = {"trade_date": "2026-07-13", "structural_payload": sp_d, "temporal_payload": tp_d}
    changes = compute_recent_changes([snap_c, snap_d])
    m3_changes = [c for c in changes if c["publicKey"] == "momentum_delta"]
    assert len(m3_changes) == 1, "M3 precision=6：1e-5 差异必须保留产生变化"
    # M3 同时有 valueText 与 categoryLabel，组合文本应同时包含两者
    assert "·" in m3_changes[0]["fromText"]
    assert "·" in m3_changes[0]["toText"]


def test_recent_changes_dimension_when_fact_disappears():
    """事实由存在变缺失时 dimension 必须来自 FACT_DIMENSION_BY_ID（禁止默认 trend）。"""
    # S3 存在 → S3 缺失（position 越界）
    sp_a, tp_a = _base_payload(swing={"price_position_in_active_swing_0_1": 0.50})
    sp_b, tp_b = _base_payload(swing={"price_position_in_active_swing_0_1": 1.5})  # 越界 → 缺失
    snap_a = {"trade_date": "2026-07-10", "structural_payload": sp_a, "temporal_payload": tp_a}
    snap_b = {"trade_date": "2026-07-11", "structural_payload": sp_b, "temporal_payload": tp_b}
    changes = compute_recent_changes([snap_a, snap_b])
    s3_changes = [c for c in changes if c["publicKey"] == "active_position"]
    assert len(s3_changes) == 1, "S3 由存在变缺失应产生变化"
    assert s3_changes[0]["dimension"] == "structure", (
        "事实消失时 dimension 必须来自 FACT_DIMENSION_BY_ID，禁止默认 trend"
    )

    # V3 (volume) 由存在变缺失：dimension 必须是 volume 而非 trend
    sp_c, tp_c = _base_payload(dsa={
        "current_segment_volume_sum": 1_000_000.0,
        "prev_segment_volume_sum": 800_000.0,
    })
    sp_d, tp_d = _base_payload(dsa={
        "current_segment_volume_sum": None,
        "prev_segment_volume_sum": None,
    })
    snap_c = {"trade_date": "2026-07-12", "structural_payload": sp_c, "temporal_payload": tp_c}
    snap_d = {"trade_date": "2026-07-13", "structural_payload": sp_d, "temporal_payload": tp_d}
    changes = compute_recent_changes([snap_c, snap_d])
    v3_changes = [c for c in changes if c["publicKey"] == "volume_ratio"]
    assert len(v3_changes) == 1
    assert v3_changes[0]["dimension"] == "volume", "V3 dimension 必须是 volume"


# ---------------------------------------------------------------------------
# 17. schema 装配验证（用户响应无 factId/sourcePath）
# ---------------------------------------------------------------------------


def test_response_schema_assembly():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    resp = AtomicFactsContextResponse(
        contractVersion=svc.CONTRACT_VERSION,
        meta=AtomicFactsMeta(
            payloadVersion=svc.AFC_PAYLOAD_VERSION,
            researchFreezeVersion=svc.RESEARCH_FREEZE_VERSION,
            presentationVersion=svc.PRESENTATION_VERSION,
        ),
        asOf="2026-07-14",
        core=res["core"],
        auxiliary=res["auxiliary"],
        availability=res["availability"],
        recentChanges=[],
        dataQuality=StockContextDataQuality(
            hasSucceededRun=True, hasSnapshot=True, reasonCode=None,
            degradedReasons=[], runTradeDate="2026-07-14", runPublishedAt=None,
            instrumentStatus="active",
        ),
    )
    assert resp.contractVersion == "Atomic Fact Contract V1"
    assert resp.meta.researchFreezeVersion == "V4.13"
    assert resp.meta.payloadVersion == "1"
    assert resp.availability.coreDenominator == 14
    # 用户响应字段不得含内部 ID/路径
    dumped = resp.model_dump()
    for _dim, items in dumped["core"].items():
        for it in items:
            assert "factId" not in it
            assert "sourcePath" not in it
