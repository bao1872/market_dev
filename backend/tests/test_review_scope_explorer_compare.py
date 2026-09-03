"""Modified-scope pure/unit tests for SLICE 5 / Explorer compare facts.

No DB, no network. Locks the §十七 backend contract list:

 1. compare facts 从 canonical Observation 直取
 2. DSA VWAP 4.2 保持 4.2，不 ×100
 3. duration owner = trend.continuous.dsa_dir_bars（scope scalar，非分布反推）
 4. SMC priority deterministic
 5. SMC unavailable != no event
 6. momentum enhancing/weakening + denominator 保留
 7. volume ratio20 direct（p50）
 8. EW direct
 9. Breadth direct
10. Capital Tilt persisted fact
11. Migration persisted fact
12. null preservation
13. percentile 使用正式 cross-sectional owner
14. family cohort 不串 family
15. unavailable / valid_peer_count 正确
16. 不复制 percentile formula
"""

from __future__ import annotations

from typing import Any

from app.services.review_scope_explorer_service import (
    build_compare_facts,
    build_peer_percentiles,
    build_peer_percentiles_by_family,
    select_smc_display_event,
)


def _obs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trend": {
            "continuous": {
                "regime_strength": 0.71,
                "dsa_dir_bars": 12.0,
                "dsa_vwap_dev_pct": 4.2,
            }
        },
        "structure": {
            "events": {
                "status": "ready",
                "reason": None,
                "cells": {
                    "leveled": {
                        "BOS_Up_Swing": {
                            "event_type": "BOS",
                            "structure_level": "Swing",
                            "direction": "Up",
                            "member_ratio": 0.18,
                        }
                    },
                    "extreme": {},
                },
            }
        },
        "momentum": {
            "change": {
                "enhancing_count": 5,
                "enhancing_ratio": 0.42,
                "weakening_count": 3,
                "weakening_ratio": 0.18,
                "flat_count": 12,
                "denominator": 20,
            }
        },
        "participation": {"volume": {"ratio20": {"p25": 0.9, "p50": 1.35, "p75": 1.6, "valid_count": 40}}},
        "price": {
            "equal_weight_return": 0.012,
            "breadth": {"advance_ratio": 0.62, "decline_ratio": 0.3, "unchanged_ratio": 0.08},
        },
    }
    base.update(over)
    return base


def _comp(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "internal_structure_facts": {
            "capital_tilt": {
                "equal_weight_return": 0.012,
                "amount_weighted_return": 0.018,
                "capital_tilt": 0.006,
            }
        },
        "leadership": {"status": "ready", "migration": 0.58, "jaccard_stability": 0.42},
    }
    base.update(over)
    return base


# --- 1/2/3/6/7/8/9/10/11: canonical 直取 ------------------------------------

def test_e1_dsa_scalars_read_from_canonical_continuous():
    f = build_compare_facts(_obs(), _comp())
    # 1: direct from trend.continuous
    assert f["dsa"]["regimeStrength"] == 0.71
    # 2: vwap dev 已是 percentage points —— 4.2 保持 4.2，绝不 ×100
    assert f["dsa"]["vwapDevPct"] == 4.2
    assert f["dsa"]["vwapDevPct"] != 420.0
    # 3: duration owner = scope 级 scalar dsa_dir_bars
    assert f["dsa"]["durationBars"] == 12.0


def test_e6_momentum_ratios_and_denominator_preserved():
    f = build_compare_facts(_obs(), _comp())
    m = f["momentum"]
    # producer ratio verbatim；denominator 保留（前端不得重定义）
    assert m["enhancingRatio"] == 0.42
    assert m["weakeningRatio"] == 0.18
    assert m["denominator"] == 20
    # 不得内部重算 count/denominator 覆盖 producer ratio
    assert m["enhancingRatio"] != 5 / 20 or 0.42 == 0.25  # 0.42 != 0.25 -> 保持 producer 值


def test_e7_volume_ratio20_direct_central_value():
    f = build_compare_facts(_obs(), _comp())
    # ratio20 取 canonical central value p50（不 ×100）
    assert f["volume"]["ratio20"] == 1.35
    assert f["volume"]["ratio20"] != 135.0


def test_e8_e9_ew_and_breadth_direct():
    f = build_compare_facts(_obs(), _comp())
    assert f["price"]["equalWeightReturn"] == 0.012
    assert f["price"]["advanceRatio"] == 0.62


def test_e10_e11_capital_tilt_and_migration_persisted():
    f = build_compare_facts(_obs(), _comp())
    # persisted capital tilt（不是 AW - EW = 0.006；此处恰好同值，另用反例锁）
    assert f["composition"]["capitalTilt"] == 0.006
    # migration 来自 persisted leadership（不是 1 - jaccard = 0.58；另用反例锁）
    assert f["composition"]["migration"] == 0.58

    # 反例：persisted tilt 与 AW-EW 不同 -> 必须取 persisted
    f2 = build_compare_facts(
        _obs(),
        _comp(internal_structure_facts={
            "capital_tilt": {
                "equal_weight_return": 0.012,
                "amount_weighted_return": 0.030,
                "capital_tilt": 0.004,
            }
        }),
    )
    assert f2["composition"]["capitalTilt"] == 0.004
    assert f2["composition"]["capitalTilt"] != 0.018  # AW - EW

    # 反例：migration 与 1-jaccard 不同 -> 必须取 persisted
    f3 = build_compare_facts(
        _obs(),
        _comp(leadership={"status": "ready", "migration": 0.9, "jaccard_stability": 0.42}),
    )
    assert f3["composition"]["migration"] == 0.9
    assert f3["composition"]["migration"] != 0.58  # 1 - jaccard


def test_e12_null_preserved_never_zero():
    f = build_compare_facts(
        {
            "trend": {"continuous": {"regime_strength": None, "dsa_dir_bars": None, "dsa_vwap_dev_pct": None}},
            "structure": {"events": {"status": "unavailable", "reason": "CURRENT_EVENTS_UNAVAILABLE", "cells": {}}},
            "momentum": {"change": {}},
            "participation": {"volume": {"ratio20": {}}},
            "price": {},
        },
        None,
    )
    assert f["dsa"]["regimeStrength"] is None
    assert f["dsa"]["durationBars"] is None
    assert f["dsa"]["vwapDevPct"] is None
    assert f["momentum"]["enhancingRatio"] is None
    assert f["momentum"]["denominator"] is None
    assert f["volume"]["ratio20"] is None
    assert f["price"]["equalWeightReturn"] is None
    assert f["price"]["advanceRatio"] is None
    assert f["composition"]["capitalTilt"] is None
    assert f["composition"]["migration"] is None
    # 缺失 payload 也不得崩
    assert build_compare_facts(None, None)["dsa"]["regimeStrength"] is None


# --- 4/5: SMC display priority ----------------------------------------------

def _events(*cells: tuple[str, str, str, float]) -> dict[str, Any]:
    return {
        "status": "ready",
        "reason": None,
        "cells": {
            "leveled": {
                f"{etype}_{direction}_{level}": {
                    "event_type": etype,
                    "structure_level": level,
                    "direction": direction,
                    "member_ratio": ratio,
                }
                for etype, level, direction, ratio in cells
            },
            "extreme": {},
        },
    }


def test_e4_smc_priority_is_fixed_and_deterministic():
    # 故意乱序输入：Internal BOS / Swing BOS / Internal CHoCH / Swing CHoCH
    ev = _events(
        ("BOS", "Internal", "Up", 0.9),    # rank 3
        ("BOS", "Swing", "Up", 0.99),      # rank 1
        ("CHoCH", "Internal", "Down", 0.8),  # rank 2
        ("CHoCH", "Swing", "Down", 0.01),  # rank 0 -> 胜出（即使 ratio 最低）
    )
    got = select_smc_display_event(ev)
    assert got["eventType"] == "CHoCH"
    assert got["structureLevel"] == "Swing"
    assert got["direction"] == "Down"
    assert got["memberRatio"] == 0.01
    assert got["availability"] == "ready"


def test_e4_smc_tie_breaks_on_member_ratio_desc():
    ev = _events(
        ("BOS", "Swing", "Up", 0.10),
        ("BOS", "Swing", "Down", 0.35),  # 同 priority，ratio 高者胜
    )
    got = select_smc_display_event(ev)
    assert got["memberRatio"] == 0.35
    assert got["direction"] == "Down"


def test_e4_smc_excludes_non_bos_choch_events():
    ev = {
        "status": "ready",
        "reason": None,
        "cells": {
            "leveled": {
                "SQZ_RELEASE_Up_Swing": {"event_type": "SQZ_RELEASE", "structure_level": "Swing", "direction": "Up", "member_ratio": 0.9},
                "OB_Up_Swing": {"event_type": "OB", "structure_level": "Swing", "direction": "Up", "member_ratio": 0.8},
                "EQH_Up_Swing": {"event_type": "EQH", "structure_level": "Swing", "direction": "Up", "member_ratio": 0.7},
            },
            "extreme": {},
        },
    }
    got = select_smc_display_event(ev)
    # OB / EQH / SQZ_RELEASE 一律不进列表
    assert got["eventType"] is None
    # ready + 无 BOS/CHoCH -> "无"，不是 unavailable
    assert got["availability"] == "ready"


def test_e5_smc_unavailable_differs_from_no_event():
    unavail = select_smc_display_event(
        {"status": "unavailable", "reason": "CURRENT_EVENTS_UNAVAILABLE", "cells": {}}
    )
    assert unavail["availability"] == "unavailable"
    assert unavail["reason"] == "CURRENT_EVENTS_UNAVAILABLE"
    assert unavail["eventType"] is None

    no_event = select_smc_display_event(_events())
    assert no_event["availability"] == "ready"
    assert no_event["eventType"] is None
    # 两者语义必须可区分
    assert no_event["availability"] != unavail["availability"]

    # 非 dict / None -> unavailable（fail-soft，不抛错）
    assert select_smc_display_event(None)["availability"] == "unavailable"
    assert select_smc_display_event({})["availability"] == "unavailable"


# --- 13/14/15/16: peer percentile -------------------------------------------

def _rows(n: int) -> list[tuple[str, Any, Any]]:
    """n 个 scope，regime_strength 与 equal_weight_return 单调递增。"""
    return [
        (f"s{i}", {"regime_strength": 0.1 * (i + 1)}, {"equal_weight_return": 0.001 * (i + 1)})
        for i in range(n)
    ]


def test_e13_e15_percentile_uses_canonical_owner_and_gate():
    # 8 个 peer（> _MINIMUM_VALID_PEER_COUNT=5）-> ready，分位由 canonical owner 计算
    pct = build_peer_percentiles(_rows(8))
    assert set(pct.keys()) == {f"s{i}" for i in range(8)}
    # 最小值分位低、最大值分位高（由 canonical _empirical_percentile_rank 决定）
    values = [pct[f"s{i}"]["regimeStrengthPeerPercentile"] for i in range(8)]
    assert all(v is not None for v in values), "peer 样本足够时 percentile 不得为 None"
    assert values[0] < values[-1]
    # 单调：值越大分位越高
    assert values == sorted(values)
    # 与 EW 字段互不串
    ew_values = [pct[f"s{i}"]["equalWeightReturnPeerPercentile"] for i in range(8)]
    assert ew_values == sorted(ew_values)


def test_e15_insufficient_peer_sample_is_null_not_zero():
    # 仅 3 个 scope（< 5 最小有效 peer）-> unavailable，percentile 为 null（绝不是 0）
    pct = build_peer_percentiles(_rows(3))
    for i in range(3):
        assert pct[f"s{i}"]["regimeStrengthPeerPercentile"] is None
        assert pct[f"s{i}"]["equalWeightReturnPeerPercentile"] is None


def test_e14_cohort_is_scoped_by_caller_not_cross_family():
    # cohort 完全由传入 rows 决定：不同 family 必须由调用方分开传入（不在此处混算）
    family_a = _rows(6)
    family_b = [(f"b{i}", {"regime_strength": 0.9}, {"equal_weight_return": 0.09}) for i in range(6)]
    pa = build_peer_percentiles(family_a)
    pb = build_peer_percentiles(family_b)
    # A 家族最大值在自己的 cohort 内是最高分位
    assert pa["s5"]["regimeStrengthPeerPercentile"] is not None
    # B 家族成员值相同 -> 分位相同（cohort 独立，不串 A 的值）
    b_values = {pb[f"b{i}"]["regimeStrengthPeerPercentile"] for i in range(6)}
    assert len(b_values) == 1
    # B 的分位不受 A 影响（A 的最高值 0.6 < B 的 0.9，若串 family B 会变）
    assert next(iter(b_values)) is not None


def test_e17_family_percentile_never_crosses_family():
    """[SLICE 5 finalization] /scopes 的 scope_type 可省略 → 一次传入混合 family。

    真实 grouping helper（不是“caller 分两次调用”）必须保证：
    industry_l1 的 scope 只在 industry_l1 cohort 内排名，concept 同理。
    """
    # concept 的值整体远小于 industry_l1 —— 若串 family，industry 分位会被压低
    rows = [
        (f"i{i}", "industry_l1", {"regime_strength": 0.80 + 0.01 * i}, {"equal_weight_return": 0.020 + 0.001 * i})
        for i in range(8)
    ] + [
        (f"c{i}", "concept", {"regime_strength": 0.10 + 0.01 * i}, {"equal_weight_return": 0.001 + 0.0001 * i})
        for i in range(8)
    ]
    pct = build_peer_percentiles_by_family(rows)

    i_vals = [pct[f"i{i}"]["regimeStrengthPeerPercentile"] for i in range(8)]
    c_vals = [pct[f"c{i}"]["regimeStrengthPeerPercentile"] for i in range(8)]
    assert all(v is not None for v in i_vals + c_vals), "两个 family 各自样本都足够"

    # 各 family 内部单调
    assert i_vals == sorted(i_vals)
    assert c_vals == sorted(c_vals)
    # 关键：两族最低值拿到相同的“族内最低分位”（说明各自独立排名，而非混算）
    assert i_vals[0] == c_vals[0]
    assert i_vals[-1] == c_vals[-1]

    # 反证：若把同一批 rows 当单一 cohort，industry_l1 最低值会被 concept 压到很低
    merged_single = build_peer_percentiles(
        [(k, tc, pr) for k, _fam, tc, pr in rows]
    )
    # 串 family 后 industry_l1 最低值（0.80）会压过全部 concept 低值，分位被抬高
    assert merged_single["i0"]["regimeStrengthPeerPercentile"] != i_vals[0], (
        "串/不串 family 必须产生不同结果，否则本测试无意义"
    )
    assert merged_single["i0"]["regimeStrengthPeerPercentile"] > i_vals[0], (
        "串 family 会把 industry_l1 分位抬高（concept 低值垫底）；"
        "family 分组必须阻止这种跨族污染"
    )


def test_e18_family_grouping_matches_separate_calls():
    """grouping helper 与“分族各调一次”结果一致（等价性锁）。"""
    rows = [
        (f"i{i}", "industry_l1", {"regime_strength": 0.5 + 0.01 * i}, {"equal_weight_return": 0.01 + 0.001 * i})
        for i in range(6)
    ] + [
        (f"c{i}", "concept", {"regime_strength": 0.2 + 0.01 * i}, {"equal_weight_return": 0.002 + 0.0001 * i})
        for i in range(6)
    ]
    grouped = build_peer_percentiles_by_family(rows)
    separate: dict[str, dict[str, float | None]] = {}
    separate.update(
        build_peer_percentiles([(k, tc, pr) for k, f, tc, pr in rows if f == "industry_l1"])
    )
    separate.update(
        build_peer_percentiles([(k, tc, pr) for k, f, tc, pr in rows if f == "concept"])
    )
    assert grouped == separate


def test_e19_single_family_request_still_one_group():
    # 请求已带 scope_type 时自然只有一个 group，语义不变
    rows = [(f"s{i}", "industry_l1", {"regime_strength": 0.1 * (i + 1)}, {"equal_weight_return": 0.001 * (i + 1)}) for i in range(6)]
    pct = build_peer_percentiles_by_family(rows)
    assert set(pct.keys()) == {f"s{i}" for i in range(6)}
    vals = [pct[f"s{i}"]["regimeStrengthPeerPercentile"] for i in range(6)]
    assert vals == sorted(vals)


def test_e16_no_duplicated_percentile_formula():
    """不复制 percentile 公式：本模块直接调用 canonical owner。

    锁法：模块源码中不得出现分位排名算术（100.0 * / (n - 1) 等），
    percentile 只能来自 compute_cross_sectional。
    """
    import inspect

    from app.services import review_scope_explorer_service as mod

    src = inspect.getsource(mod)
    assert "compute_cross_sectional" in src, "必须复用 canonical cross-sectional owner"
    # 不得自行实现 percentile rank 公式
    assert "empirical_percentile_rank" not in src, "禁止复制/直接调用内部排名函数绕过 owner"
    for forbidden in ("* 100.0 /", "/ (n - 1)", "/ (len(", "rank / "):
        assert forbidden not in src, f"禁止复制 percentile 公式片段: {forbidden}"
