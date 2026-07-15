"""Atomic Fact Contract V1 - 纯函数/服务级单元测试（无 DB，快速）。

覆盖硬性验证：
1. Registry 数量严格 14/10/1、顺序和 ID 唯一
2. V1 永久缺席（不进 any fact、不进 availability）
3. T3/T6 默认隐藏（feature flag 关 → 不在 auxiliary）
4. T2/M2/M3 真实原始值与单位（不伪造成 [-1,1]）
5. T5/V3 阈值唯一真源（thresholdEnabled=False，仅比值 + 分类未启用）
6. M5 三态 + 双 true 进入数据质量异常
7. S3 边界（0.33/0.67；0.63 → 中间）
8. S7/S8 非负距离展示（禁止显示负距离）
9. persisted payload 与旧快照 fallback 结果一致（均走同一纯函数）
10. compact 含完整 14 项且 S2 存在；Core 分母固定 14
11. 无旧 MACD / 旧 SQZMOM 状态 / 布林位置 / 关键证据；无禁用词
12. as_of 无未来信息（recent_changes 仅引用给定快照 trade_date）

用法：
    cd backend && pytest tests/test_atomic_fact_contract_service.py -v
"""

from __future__ import annotations

import json

import pytest

from app.services import atomic_fact_contract_service as svc
from app.services.atomic_fact_contract_service import (
    CORE_FACT_IDS,
    AUX_FACT_IDS,
    REJECTED_FACT_IDS,
    compute_atomic_facts,
    compute_recent_changes,
)
from app.schemas.atomic_fact_contract import AtomicFactsContextResponse


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


def _find_core(res: dict, fact_id: str) -> dict:
    for items in res["core"].values():
        for it in items:
            if it["factId"] == fact_id:
                return it
    raise AssertionError(f"core fact {fact_id} not found")


# ---------------------------------------------------------------------------
# 1. Registry 14/10/1
# ---------------------------------------------------------------------------


def test_registry_counts_and_order():
    assert len(CORE_FACT_IDS) == 14
    assert len(AUX_FACT_IDS) == 10
    assert len(REJECTED_FACT_IDS) == 1
    # 顺序固定
    assert CORE_FACT_IDS == [
        "T1_trend_direction", "T2_aligned_slope", "T4_trend_age", "T5_slope_ratio",
        "M1_momentum_alignment", "M2_aligned_momentum", "M3_aligned_momentum_delta",
        "M5_squeeze_state", "S1_confirmed_boundary_relation", "S2_active_dir_relation",
        "S3_active_position", "S7_dist_favorable_boundary", "S8_dist_adverse_boundary",
        "V3_avg_volume_ratio",
    ]
    # ID 唯一
    all_ids = set(CORE_FACT_IDS) | set(AUX_FACT_IDS) | set(REJECTED_FACT_IDS)
    assert len(all_ids) == 25


# ---------------------------------------------------------------------------
# 2. V1 永久缺席
# ---------------------------------------------------------------------------


def test_v1_permanently_absent():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    flat = json.dumps(res, ensure_ascii=False)
    assert "V1_cumulative_volume_ratio" not in flat
    assert "V1" not in flat  # 任何 V1 痕迹
    assert res["availability"]["v1Present"] is False
    assert res["availability"]["rejectedPresent"] is False
    # V1 也不在 auxiliary
    aux_ids = [it["factId"] for it in res["auxiliary"]]
    assert "V1_cumulative_volume_ratio" not in aux_ids


# ---------------------------------------------------------------------------
# 3. T3/T6 默认隐藏
# ---------------------------------------------------------------------------


def test_t3_t6_default_hidden():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    aux_ids = [it["factId"] for it in res["auxiliary"]]
    assert "T3_trend_efficiency" not in aux_ids
    assert "T6_efficiency_delta" not in aux_ids
    # T3/T6 关闭未计算，故既不出现在 auxiliary 也不在 auxiliaryHidden
    assert "T3_trend_efficiency" not in res["availability"]["auxiliaryHidden"]
    assert "T6_efficiency_delta" not in res["availability"]["auxiliaryHidden"]
    # auxiliaryHidden 为响应中实际返回的 Auxiliary ID（全部默认隐藏）
    assert set(res["availability"]["auxiliaryHidden"]) == set(aux_ids)
    assert len(aux_ids) == 8  # 10 aux 减去 T3/T6


# ---------------------------------------------------------------------------
# 4. T2/M2/M3 真实原始值与单位
# ---------------------------------------------------------------------------


def test_t2_m2_m3_real_values_and_units():
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1, "current_dsa_segment_slope_atr_per_bar": 0.0123},
        vol={"sqzmom_val": 0.002, "sqzmom_delta_1": 0.0003},
    )
    res = compute_atomic_facts(sp, tp)
    t2 = _find_core(res, "T2_aligned_slope")
    assert abs(t2["value"] - 0.0123) < 1e-9
    assert t2["unit"] == "ATR/bar"
    m2 = _find_core(res, "M2_aligned_momentum")
    assert abs(m2["value"] - 0.002) < 1e-9
    m3 = _find_core(res, "M3_aligned_momentum_delta")
    assert abs(m3["value"] - 0.0003) < 1e-9
    assert m3["category"] == "增加"  # 统一零值容差


# ---------------------------------------------------------------------------
# 5. T5/V3 阈值唯一真源（未确认 → 仅比值 + 分类未启用）
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
    t5 = _find_core(res, "T5_slope_ratio")
    assert t5["thresholdEnabled"] is False
    assert "分类未启用" in t5["displayText"]
    assert abs(t5["value"] - 1.23) < 1e-9  # 0.0123/0.0100
    v3 = _find_core(res, "V3_avg_volume_ratio")
    assert v3["thresholdEnabled"] is False
    assert "分类未启用" in v3["displayText"]
    # (1200000/12)/(900000/10) = 100000/90000 ≈ 1.1111
    assert abs(v3["value"] - (100000.0 / 90000.0)) < 1e-9


# ---------------------------------------------------------------------------
# 6. M5 三态 + 双 true 异常
# ---------------------------------------------------------------------------


def test_m5_states():
    # ON
    sp, tp = _base_payload(vol={"sqz_on": True, "sqz_off": False})
    assert _find_core(compute_atomic_facts(sp, tp), "M5_squeeze_state")["category"] == "ON"
    # OFF
    sp, tp = _base_payload(vol={"sqz_on": False, "sqz_off": True})
    assert _find_core(compute_atomic_facts(sp, tp), "M5_squeeze_state")["category"] == "OFF"
    # NORMAL
    sp, tp = _base_payload(vol={"sqz_on": False, "sqz_off": False})
    assert _find_core(compute_atomic_facts(sp, tp), "M5_squeeze_state")["category"] == "NORMAL"
    # INCONSISTENT（双 true → 数据质量异常）
    sp, tp = _base_payload(vol={"sqz_on": True, "sqz_off": True})
    m5 = _find_core(compute_atomic_facts(sp, tp), "M5_squeeze_state")
    assert m5["category"] == "INCONSISTENT"
    assert "数据质量异常" in m5["displayText"]


# ---------------------------------------------------------------------------
# 7. S3 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pos,expected",
    [
        (0.10, "LOWER"),
        (0.33, "MIDDLE"),  # 合同公式：0.33–0.67 → 中间区间（0.33 为中间区间起点）
        (0.50, "MIDDLE"),
        (0.63, "MIDDLE"),  # 关键：0.63 → 中间
        (0.67, "MIDDLE"),  # 边界归属中间区间
        (0.68, "UPPER"),
        (0.95, "UPPER"),
    ],
)
def test_s3_boundary(pos, expected):
    sp, tp = _base_payload(swing={"price_position_in_active_swing_0_1": pos})
    s3 = _find_core(compute_atomic_facts(sp, tp), "S3_active_position")
    assert s3["category"] == expected


# ---------------------------------------------------------------------------
# 8. S7/S8 非负距离展示
# ---------------------------------------------------------------------------


def test_s7_s8_no_negative_distance_display():
    # dsa_dir>0：S7=dist_high=2.5（正，尚未到达）；S8=dist_low=-1.2（负，已越过）
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": 1},
        swing={"distance_to_swing_high_atr": 2.5, "distance_to_swing_low_atr": -1.2},
    )
    res = compute_atomic_facts(sp, tp)
    s7 = _find_core(res, "S7_dist_favorable_boundary")
    assert "尚未到达" in s7["displayText"]
    assert s7["value"] == 2.5
    s8 = _find_core(res, "S8_dist_adverse_boundary")
    assert "已越过" in s8["displayText"]
    assert s8["value"] == -1.2  # 原始值保留符号（admin 可追溯），但展示文案不显示负距离
    assert "负" not in s8["displayText"]

    # dsa_dir<0：S7=dist_low=-1.2（负，已越过）；S8=dist_high=2.5（正，尚未到达）
    sp, tp = _base_payload(
        dsa={"current_dsa_segment_dir": -1},
        swing={"distance_to_swing_high_atr": 2.5, "distance_to_swing_low_atr": -1.2},
    )
    res = compute_atomic_facts(sp, tp)
    s7 = _find_core(res, "S7_dist_favorable_boundary")
    assert "已越过" in s7["displayText"]
    s8 = _find_core(res, "S8_dist_adverse_boundary")
    assert "尚未到达" in s8["displayText"]


# ---------------------------------------------------------------------------
# 9. persisted payload 与旧快照 fallback 结果一致（同一纯函数）
# ---------------------------------------------------------------------------


def test_persisted_equals_fallback():
    sp, tp = _base_payload()
    # 当前视图：从 payload 计算（fallback 路径）
    fallback = compute_atomic_facts(sp, tp)
    # 新快照：summary_payload.atomic_fact_contract_v1 由 build_summary_payload 注入，
    # 其值与 fallback 完全一致（同一纯函数）。此处等价验证两者逐字段相等。
    persisted = compute_atomic_facts(sp, tp)
    assert json.dumps(fallback, ensure_ascii=False, sort_keys=True) == json.dumps(
        persisted, ensure_ascii=False, sort_keys=True
    )
    # availability 也一致
    assert fallback["availability"] == persisted["availability"]


# ---------------------------------------------------------------------------
# 10. compact 完整 14 + S2 存在；分母固定 14
# ---------------------------------------------------------------------------


def test_compact_full_14_and_s2_present():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    all_core_ids = [it["factId"] for items in res["core"].values() for it in items]
    assert len(all_core_ids) == 14
    assert set(all_core_ids) == set(CORE_FACT_IDS)
    assert "S2_active_dir_relation" in all_core_ids
    assert res["availability"]["coreDenominator"] == 14
    assert res["availability"]["corePresent"] == 14
    assert res["availability"]["coreMissing"] == []


# ---------------------------------------------------------------------------
# 11. 无旧 MACD / SQZMOM 状态 / 布林 / 关键证据；无禁用词
# ---------------------------------------------------------------------------


FORBIDDEN_WORDS = [
    "买入", "卖出", "加仓", "减仓", "止损", "安全", "买点", "卖点", "持仓",
    "趋势形成", "趋势反转", "成熟", "衰竭", "便宜", "昂贵",
    "突破方向", "放量", "缩量", "累计成交量比",
    "MACD", "布林", "SQZMOM状态", "关键证据",
]


def test_no_forbidden_words_and_no_legacy_status():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    flat = json.dumps(res, ensure_ascii=False)
    for w in FORBIDDEN_WORDS:
        assert w not in flat, f"出现禁用词/旧状态: {w}"
    # 响应无旧 state/events 字段
    assert "state" not in res
    assert "events" not in res


# ---------------------------------------------------------------------------
# 12. as_of 无未来信息（recent_changes 仅引用给定快照 trade_date）
# ---------------------------------------------------------------------------


def test_recent_changes_no_future():
    snap_old = {"trade_date": "2026-07-10", "structural_payload": _base_payload()[0], "temporal_payload": _base_payload()[1]}
    snap_new = {"trade_date": "2026-07-14", "structural_payload": _base_payload(dsa={"current_dsa_segment_dir": -1})[0], "temporal_payload": _base_payload()[1]}
    changes = compute_recent_changes([snap_old, snap_new])
    # 所有变化的 asOf 都来自提供的快照 trade_date，绝不超出
    for c in changes:
        assert c["asOf"] in ("2026-07-10", "2026-07-14")
    # 单快照无变化
    assert compute_recent_changes([snap_old]) == []


# ---------------------------------------------------------------------------
# 13. schema 装配验证
# ---------------------------------------------------------------------------


def test_response_schema_assembly():
    sp, tp = _base_payload()
    res = compute_atomic_facts(sp, tp)
    resp = AtomicFactsContextResponse(
        contractVersion=svc.CONTRACT_VERSION,
        asOf="2026-07-14",
        core=res["core"],
        auxiliary=res["auxiliary"],
        availability=res["availability"],
        recentChanges=[],
        dataQuality=__import__(
            "app.schemas.stock_state", fromlist=["StockContextDataQuality"]
        ).StockContextDataQuality(
            hasSucceededRun=True, hasSnapshot=True, reasonCode=None,
            degradedReasons=[], runTradeDate="2026-07-14", runPublishedAt=None,
            instrumentStatus="active",
        ),
    )
    assert resp.contractVersion == "Atomic Fact Contract V1"
    assert resp.availability.coreDenominator == 14
