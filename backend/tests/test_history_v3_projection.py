"""History-v3 projection PURE tests (no DB).

[CHANGE-20260826-001 History-v3] History(T) 是 canonical Core artifact 的 PURE 投影，
禁止重新运行 DSA / SMC / Bollinger / SQZMOM / VolumeContext kernel。

本文件为纯单元测试（PURE_UNIT_TEST=1 可运行），不连接数据库。
materialize（DB）测试见 test_history_v3_materialize.py。
"""
import app.strategy_assets.algorithms.features.smc_pine_core as smc
import app.strategy_assets.algorithms.features.sqzmom_lb as sqz
import app.strategy.selectors.dsa_selector as dsa
from app.services.history_v3_projection import (
    REVIEW_HISTORY_V3_CONTRACT_VERSION,
    build_history_v3_projection,
    to_history_result_shape,
)


def _make_core_flat():
    return {
        "fp_regime_value": "强势",
        "fp_regime_strength": 0.82,
        "fp_dsa_dir_bars": 7,
        "fp_dsa_vwap_dev_pct": -1.3,
        "fp_segment_id": 5,
        "fp_segment_direction": "up",
        "fp_segment_bars": 12,
        "fp_segment_change_pct": 3.4,
        "fp_segment_slope": 0.21,
        "fp_segment_volume_ratio": 1.5,
        "fp_segment_amount_ratio": 1.2,
        "fp_segment_avg_volume": 800.0,
        "fp_prev_segment_volume": 600.0,
        "fp_swing_direction": "bullish",
        "fp_internal_direction": "bearish",
        "fp_sqzmom_value": 0.5,
        "fp_sqzmom_prev": 0.3,
        "fp_momentum_direction": "up",
        "fp_momentum_change": 0.2,
        "fp_volume_ratio20": 1.1,
        "fp_volume_ratio200": 0.9,
        "fp_volume_percentile20": 35,
        "fp_volume_percentile200": 50,
        "fp_volume_zscore20": -0.4,
        "fp_volume_zscore200": 0.1,
        "fp_squeeze_avg_volume": 100.0,
        "fp_release_volume_ratio": 0.5,
        "fp_momentum_volume_relation": "缩量挤压",
        "fp_squeeze_state": "已释放",
        "fp_latest_sqz_off_freshness": 0,
        "fp_structure_event_type": "BOS",
        "fp_structure_event_direction": "up",
        "fp_structure_event_date": "2026-08-25",
        "fp_structure_event_price": 10.5,
        "fp_structure_event_freshness": 1,
    }


def test_v3_contract_version_constant():
    assert REVIEW_HISTORY_V3_CONTRACT_VERSION == "review-history-v3"


def test_v3_is_pure_projection_no_kernel_calls():
    """PHASE 10: 投影不调用任何 Core kernel（spy）。"""
    calls = {"compute_sqzmom_lb": 0, "compute_dsa_bundle": 0, "compute_smc_pine": 0}
    orig_sqz = sqz.compute_sqzmom_lb
    orig_dsa = getattr(dsa, "compute_dsa_bundle", None)
    orig_smc = getattr(smc, "compute_smc_pine", None)

    def spy_sqz(*a, **k):
        calls["compute_sqzmom_lb"] += 1
        return orig_sqz(*a, **k)

    def spy_dsa(*a, **k):
        calls["compute_dsa_bundle"] += 1
        return orig_dsa(*a, **k) if orig_dsa else None

    def spy_smc(*a, **k):
        calls["compute_smc_pine"] += 1
        return orig_smc(*a, **k) if orig_smc else None

    sqz.compute_sqzmom_lb = spy_sqz
    if orig_dsa:
        dsa.compute_dsa_bundle = spy_dsa
    if orig_smc:
        smc.compute_smc_pine = spy_smc
    try:
        build_history_v3_projection(
            core_flat=_make_core_flat(),
            instrument_id="00000000-0000-0000-0000-000000000001",
            trade_date="2026-08-25",
            core_run_id="11111111-1111-1111-1111-111111111111",
        )
    finally:
        sqz.compute_sqzmom_lb = orig_sqz
        if orig_dsa:
            dsa.compute_dsa_bundle = orig_dsa
        if orig_smc:
            smc.compute_smc_pine = orig_smc

    assert calls["compute_sqzmom_lb"] == 0, "projection must not run SQZMOM kernel"
    assert calls["compute_dsa_bundle"] == 0, "projection must not run DSA kernel"
    assert calls["compute_smc_pine"] == 0, "projection must not run SMC kernel"


def test_v3_field_rtm_and_adapters():
    """PHASE 3: 全字段 RTM + momentum delta 适配器。"""
    proj = build_history_v3_projection(
        core_flat=_make_core_flat(),
        instrument_id="00000000-0000-0000-0000-000000000002",
        trade_date="2026-08-25",
    )
    sp = proj["state_payload"]
    assert sp["regime_value"] == "强势"
    assert sp["dsa_dir_bars"] == 7
    assert sp["segment_direction"] == "up"
    assert sp["swing_bias"] == "bullish"
    assert sp["internal_bias"] == "bearish"
    # adapter: numeric momentum delta → enhancing/weakening/flat
    assert sp["momentum_change"] == "enhancing"  # +0.2
    assert sp["sqzmom_delta"] == "enhancing"      # +0.3
    # squeeze volume facts (Core SSOT)
    assert sp["squeeze_period_volume_mean"] == 100.0
    assert sp["release_volume_ratio"] == 0.5
    assert sp["momentum_volume_relation"] == "缩量挤压"


def test_v3_event_projection_no_kernel():
    """PHASE 4: 事件投影（BOS + SQZ_RELEASE）来自 Core，不重判。"""
    proj = build_history_v3_projection(
        core_flat=_make_core_flat(),
        instrument_id="00000000-0000-0000-0000-000000000003",
        trade_date="2026-08-25",
    )
    etypes = {e["event_type"] for e in proj["event_payloads"]}
    assert "BOS" in etypes
    assert "SQZ_RELEASE" in etypes
    sqz_ev = next(e for e in proj["event_payloads"] if e["event_type"] == "SQZ_RELEASE")
    assert sqz_ev["source"] == "core_squeeze_state"


def test_v3_deterministic_lineage():
    """PHASE 2: 纯函数 → 相同输入相同 lineage hash。"""
    a = build_history_v3_projection(core_flat=_make_core_flat(), instrument_id="i", trade_date="2026-08-25")
    b = build_history_v3_projection(core_flat=_make_core_flat(), instrument_id="i", trade_date="2026-08-25")
    assert a["lineage"]["hash"] == b["lineage"]["hash"]


def test_v3_to_history_result_shape_carries_v3_version():
    proj = build_history_v3_projection(core_flat=_make_core_flat(), instrument_id="i", trade_date="2026-08-25")
    hr = to_history_result_shape(proj)
    assert hr["meta"]["contract_version"] == "review-history-v3"
    assert len(hr["daily_state"]) == 1
    assert hr["daily_state"][0]["state_payload"]["regime_value"] == "强势"
