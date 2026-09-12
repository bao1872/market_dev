"""Node Monitor Target Set / Versioning（Stage C2）单元测试。

覆盖 C2 spec §7-§48 的全部 fail-closed / deterministic / version-boundary 行为。
本测试只验证 Target contract 自身的纯函数语义，不接 Monitor、不连 DB、不调 MDAS。

关键不变量：
- C1 `NodeClusterInput` source hash 是 RAW source identity；
  engine `profile.daily_source_hash / bars_15m_source_hash` 是 qfq 内容 hash；
  两者**不得**做相等断言（§9 / §38）。
- Context mode 是硬前提（§7 / §41）。
- Target Set 完全 deterministic（§31 / §46 无 wall-clock）。
- version-scoped target_id：同一 Set+价格 → 同 ID；source/factor/as_of/algorithm
  任一变化 → 新 version + 新 target_id（§32-§36）。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.services.node_cluster_engine import NodeClusterProfileResult
from app.services.node_cluster_input_provider import NodeClusterInput
from app.services.node_monitor_target_service import (
    _NODE_MONITOR_TARGET_SCHEMA_VERSION,
    NodeMonitorTargetService,
    NodeMonitorTargetUnavailableError,
    _stable_sha256,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_node_input(
    *,
    daily_source_hash: str = "raw-daily-A",
    m15_source_hash: str = "raw-m15-A",
    daily_adj_factor_hash: str = "FACTOR1",
    m15_adj_factor_hash: str = "FACTOR1",
    adjustment_context_hash: str = "CTX1",
    adjustment_as_of: date = date(2026, 9, 12),
    availability: str = "available",
    degraded_reason: str | None = None,
    daily_count: int = 250,
    m15_count: int = 4000,
    m15_end: str | None = None,
) -> NodeClusterInput:
    """构造合成 NodeClusterInput（Context mode）。"""
    daily_idx = pd.date_range("2026-01-01", periods=daily_count, freq="D")
    if m15_end is not None:
        m15_idx = pd.date_range(end=m15_end, periods=m15_count, freq="15min")
    else:
        m15_idx = pd.date_range("2026-09-01 09:30", periods=m15_count, freq="15min")
    daily_bars = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        index=daily_idx,
    )
    bars_15m = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        index=m15_idx,
    )
    return NodeClusterInput(
        daily_bars=daily_bars,
        bars_15m=bars_15m,
        daily_source_hash=daily_source_hash,
        daily_adj_factor_hash=daily_adj_factor_hash,
        m15_source_hash=m15_source_hash,
        m15_adj_factor_hash=m15_adj_factor_hash,
        daily_count=daily_count,
        m15_count=m15_count,
        daily_requested=250,
        m15_requested=4000,
        daily_history_exhausted=False,
        m15_history_exhausted=False,
        availability=availability,
        degraded_reason=degraded_reason,
        adjustment_as_of=adjustment_as_of,
        adjustment_context_hash=adjustment_context_hash,
    )


def _make_profile(
    peak_prices: list[float],
    *,
    algorithm_version: str = "nc-v1",
    output_schema_version: int = 1,
    contract_fingerprint: str = "nc-cf-v1",
    adj_factor_hash: str = "FACTOR1",
    adjustment_as_of: str = "2026-09-12",
    profile_hash: str = "profhash-A",
    daily_bars_count: int = 250,
    bars_15m_count: int = 4000,
) -> NodeClusterProfileResult:
    """构造合成 NodeClusterProfileResult。

    peak_prices: 原始 Peak 价格（可带 >4 位小数，如 §29 的 10.12344）。
    peak_rows price_mid 与 profile_rows 使用 round(p, 4)；all_peak_prices
    保持原始传入值，以验证 §14 canonical/legacy invariant 在取整后仍一致。
    """
    peak_rows: list[dict] = []
    profile_rows: list[dict] = []
    for i, p in enumerate(peak_prices):
        mid = round(float(p), 4)
        peak_rows.append({
            "price_mid": mid,
            "bullish_volume": 100.0 + i,
            "bearish_volume": 50.0 + i,
            "total_volume": 150.0 + i,
            "is_peak": True,
        })
        profile_rows.append({
            "price_low": round(mid - 0.05, 4),
            "price_high": round(mid + 0.05, 4),
            "price_mid": mid,
            "bullish_volume": 100.0 + i,
            "bearish_volume": 50.0 + i,
            "total_volume": 150.0 + i,
            "is_peak": True,
            "is_poc": False,
            "is_value_area": False,
        })
    return NodeClusterProfileResult(
        algorithm_version=algorithm_version,
        output_schema_version=output_schema_version,
        contract_fingerprint=contract_fingerprint,
        profile_rows=profile_rows,
        peak_rows=peak_rows,
        all_peak_prices=[float(p) for p in peak_prices],
        poc_price=None,
        vah_price=None,
        val_price=None,
        price_step=None,
        lowest_price=None,
        highest_price=None,
        # 注意：这是 qfq 内容 hash（与 C1 RAW source hash 不是同一坐标，禁止比较）
        daily_source_hash="qfq-daily",
        bars_15m_source_hash="qfq-m15",
        adj_factor_hash=adj_factor_hash,
        adjustment_as_of=adjustment_as_of,
        daily_bars_count=daily_bars_count,
        bars_15m_count=bars_15m_count,
        profile_hash=profile_hash,
    )


def _target_by_price(ts, price: float):
    for t in ts.targets:
        if abs(t.price - price) < 1e-9:
            return t
    raise AssertionError(f"target price {price} not found in target set")


# =============================================================================
# §29 旧语义完全一致（最核心兼容测试）
# =============================================================================


def test_legacy_all_peak_prices_semantics_preserved():
    """C2 target.all_peak_prices 与旧 all_peak_prices（round 4）完全一致。"""
    node_input = _make_node_input()
    profile = _make_profile([10.12344, 20.56784, 30.00001])

    ts = NodeMonitorTargetService.build_target_set(node_input, profile)

    assert ts.all_peak_prices == (10.1234, 20.5678, 30.0)
    assert sorted({round(float(p), 4) for p in profile.all_peak_prices}) == [
        10.1234, 20.5678, 30.0,
    ]
    # 顺序：价格升序
    prices = [t.price for t in ts.targets]
    assert prices == sorted(prices)
    assert prices == [10.1234, 20.5678, 30.0]


# =============================================================================
# §30 canonical mismatch 必须 fail
# =============================================================================


def test_canonical_target_set_mismatch_fails():
    """旧 crossing semantics 与 Canonical Node DTO 分叉时拒绝运行。"""
    node_input = _make_node_input()
    # legacy all_peak_prices = [10, 20]，但 canonical regions = [10, 30]
    profile = _make_profile([10.0, 30.0])
    profile = profile.__class__(
        **{**profile.__dict__, "all_peak_prices": [10.0, 20.0]},
    )

    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "canonical_target_set_mismatch"


# =============================================================================
# §31 deterministic
# =============================================================================


def test_deterministic_build():
    """完全相同输入连续 build 两次，version / target_id / to_dict 完全一致。"""
    node_input = _make_node_input()
    profile = _make_profile([10.0, 20.0, 30.0])

    a = NodeMonitorTargetService.build_target_set(node_input, profile)
    b = NodeMonitorTargetService.build_target_set(node_input, profile)

    assert a.target_set_version == b.target_set_version
    assert [t.target_id for t in a.targets] == [t.target_id for t in b.targets]
    assert a.to_dict() == b.to_dict()
    assert len(a.target_set_version) == 64


# =============================================================================
# §32 新 completed 15m 必须换 version
# =============================================================================


def test_m15_source_hash_change_changes_version_and_target_id():
    """仅 m15_source_hash 变化（Profile target prices 不变）→ version + target_id 都变。"""
    profile = _make_profile([10.0, 20.0, 30.0])
    old = NodeMonitorTargetService.build_target_set(
        _make_node_input(m15_source_hash="raw-m15-A"), profile,
    )
    new = NodeMonitorTargetService.build_target_set(
        _make_node_input(m15_source_hash="raw-m15-B"), profile,
    )

    assert old.target_set_version != new.target_set_version
    old_t = _target_by_price(old, 20.0)
    new_t = _target_by_price(new, 20.0)
    assert old_t.target_id != new_t.target_id


# =============================================================================
# §33 adjustment context 变化必须换 version
# =============================================================================


def test_adjustment_context_hash_change_changes_version():
    """仅 adjustment_context_hash 变化（raw/peaks/profile hash 人为保持不变）→ version + target_id 变。"""
    profile = _make_profile([20.0])
    old = NodeMonitorTargetService.build_target_set(
        _make_node_input(adjustment_context_hash="CTX1"), profile,
    )
    new = NodeMonitorTargetService.build_target_set(
        _make_node_input(adjustment_context_hash="CTX2"), profile,
    )

    assert old.target_set_version != new.target_set_version
    assert _target_by_price(old, 20.0).target_id != _target_by_price(new, 20.0).target_id


# =============================================================================
# §34 daily source 变化必须换 version
# =============================================================================


def test_daily_source_hash_change_changes_version():
    """仅 daily_source_hash 变化 → version 变。"""
    profile = _make_profile([20.0])
    old = NodeMonitorTargetService.build_target_set(
        _make_node_input(daily_source_hash="raw-daily-A"), profile,
    )
    new = NodeMonitorTargetService.build_target_set(
        _make_node_input(daily_source_hash="raw-daily-B"), profile,
    )
    assert old.target_set_version != new.target_set_version


# =============================================================================
# §35 algorithm / contract / profile / node_regions 变化必须换 version
# =============================================================================


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__class__(**{**p.__dict__, "algorithm_version": "nc-v2"}),
        lambda p: p.__class__(**{**p.__dict__, "output_schema_version": 2}),
        lambda p: p.__class__(**{**p.__dict__, "contract_fingerprint": "nc-cf-v2"}),
        lambda p: p.__class__(**{**p.__dict__, "profile_hash": "profhash-B"}),
    ],
)
def test_profile_metadata_change_changes_version(mutate):
    """algorithm_version / output_schema_version / contract_fingerprint / profile_hash 任一改变 → version 变。"""
    base = _make_node_input()
    profile = _make_profile([20.0])
    old = NodeMonitorTargetService.build_target_set(base, profile)
    new_profile = mutate(profile)
    new = NodeMonitorTargetService.build_target_set(base, new_profile)
    assert old.target_set_version != new.target_set_version


# =============================================================================
# §36 canonical Node DTO（node_regions）变化必须换 version
# =============================================================================


def test_canonical_node_dto_change_changes_version():
    """相同 target mid（20.0），但 region low/high/volume 改变使 node_regions_hash 变化 → version 变。"""
    node_input = _make_node_input()
    base = _make_profile([20.0])

    old = NodeMonitorTargetService.build_target_set(node_input, base)

    # 修改 peak_rows / profile_rows 的 volume（保持 mid=20.0，保持 all_peak_prices 一致）
    new_peak_rows = [dict(r, bullish_volume=999.0, total_volume=999.0) for r in base.peak_rows]
    new_profile_rows = [dict(r, bullish_volume=999.0, total_volume=999.0) for r in base.profile_rows]
    changed = base.__class__(
        **{**base.__dict__, "peak_rows": new_peak_rows, "profile_rows": new_profile_rows},
    )
    new = NodeMonitorTargetService.build_target_set(node_input, changed)

    # crossing price 没变，但 canonical Profile semantics 已变
    assert [t.price for t in old.targets] == [t.price for t in new.targets]
    assert old.target_set_version != new.target_set_version
    assert old.node_regions_hash != new.node_regions_hash


# =============================================================================
# §37 factor / as_of consistency fail-closed
# =============================================================================


def test_node_input_factor_hash_mismatch():
    node_input = _make_node_input(
        daily_adj_factor_hash="FACTOR1", m15_adj_factor_hash="FACTOR2",
    )
    profile = _make_profile([20.0], adj_factor_hash="FACTOR1")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "node_input_factor_hash_mismatch"


def test_profile_factor_hash_mismatch():
    node_input = _make_node_input(
        daily_adj_factor_hash="FACTOR1", m15_adj_factor_hash="FACTOR1",
    )
    profile = _make_profile([20.0], adj_factor_hash="FACTOR-X")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_factor_hash_mismatch"


def test_profile_adjustment_as_of_mismatch():
    node_input = _make_node_input(
        daily_adj_factor_hash="FACTOR1", m15_adj_factor_hash="FACTOR1",
        adjustment_as_of=date(2026, 9, 12),
    )
    profile = _make_profile(
        [20.0], adj_factor_hash="FACTOR1", adjustment_as_of="2026-09-13",
    )
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_adjustment_as_of_mismatch"


def test_adjustment_as_of_missing():
    node_input = _make_node_input(
        daily_adj_factor_hash="FACTOR1", m15_adj_factor_hash="FACTOR1",
        adjustment_as_of=None,
    )
    profile = _make_profile(
        [20.0], adj_factor_hash="FACTOR1", adjustment_as_of="2026-09-12",
    )
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "adjustment_as_of_missing"


# =============================================================================
# §38 RAW source hash != profile qfq hash 仍成功
# =============================================================================


def test_raw_source_hash_differs_from_qfq_profile_hash_ok():
    """C1 RAW source hash 与 engine qfq 内容 hash 本就不同坐标，构建必须成功（无错误相等断言）。"""
    node_input = _make_node_input(
        daily_source_hash="raw-daily", m15_source_hash="raw-m15",
        daily_adj_factor_hash="FACTOR1", m15_adj_factor_hash="FACTOR1",
        adjustment_context_hash="CTX1",
    )
    # profile 的 daily_source_hash/bars_15m_source_hash 是 qfq 内容 hash（不同字符串）
    profile = _make_profile(
        [20.0], adj_factor_hash="FACTOR1", adjustment_as_of="2026-09-12",
        daily_bars_count=250, bars_15m_count=4000,
    )
    assert profile.daily_source_hash == "qfq-daily"
    assert profile.bars_15m_source_hash == "qfq-m15"

    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    assert ts.daily_source_hash == "raw-daily"
    assert ts.m15_source_hash == "raw-m15"
    assert len(ts.targets) == 1


# =============================================================================
# §39 degraded input 允许
# =============================================================================


def test_degraded_input_allowed():
    node_input = _make_node_input(
        availability="degraded",
        degraded_reason="INSUFFICIENT_15M_HISTORY",
        m15_count=144,
        m15_end="2026-09-12 14:45",
    )
    profile = _make_profile(
        [20.0], adj_factor_hash="FACTOR1", adjustment_as_of="2026-09-12",
        bars_15m_count=144,
    )
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    assert ts.input_availability == "degraded"
    assert ts.input_degraded_reason == "INSUFFICIENT_15M_HISTORY"
    assert len(ts.targets) == 1


# =============================================================================
# §40 unavailable input 拒绝
# =============================================================================


def test_unavailable_input_rejected():
    node_input = _make_node_input(availability="unavailable", degraded_reason="MISSING_15M_BARS")
    profile = _make_profile([20.0])
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "node_input_unavailable"


# =============================================================================
# §41 context 缺失拒绝（Legacy C1 input）
# =============================================================================


def test_legacy_context_missing_rejected():
    node_input = _make_node_input(adjustment_context_hash=None)
    profile = _make_profile([20.0])
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "adjustment_context_hash_missing"


# =============================================================================
# §42 invalid target price 拒绝（参数化 NaN / Inf / -Inf）
# =============================================================================


@pytest.mark.parametrize(
    "bad_price",
    [float("nan"), float("inf"), float("-inf")],
)
def test_invalid_target_price_from_all_peak_prices(bad_price):
    node_input = _make_node_input()
    profile = _make_profile([10.0, bad_price])
    # 保证 canonical invariant 不在 price 校验前短路：但 all_peak_prices 含非有限值
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "invalid_target_price"


@pytest.mark.parametrize(
    "bad_price",
    [float("nan"), float("inf"), float("-inf")],
)
def test_invalid_target_price_from_region_mid(bad_price):
    node_input = _make_node_input()
    # peak_rows mid 非有限 → regions mid 非有限
    profile = _make_profile([10.0])
    bad_peak = [{
        "price_mid": bad_price,
        "bullish_volume": 1.0,
        "bearish_volume": 1.0,
        "total_volume": 2.0,
        "is_peak": True,
    }]
    changed = profile.__class__(**{**profile.__dict__, "peak_rows": bad_peak})
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, changed)
    assert exc.value.reason == "invalid_target_price"


# =============================================================================
# §43 positional canonical ID 不能成为 target ID
# =============================================================================


def test_source_region_ids_not_used_as_target_id():
    node_input = _make_node_input()
    profile = _make_profile([20.0])
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    t = ts.targets[0]
    assert "peak_000" in t.source_region_ids
    assert t.target_id != "peak_000"
    assert len(t.target_id) == 64
    int(t.target_id, 16)  # 合法 hex


# =============================================================================
# §44 target_set_version / target_id 长度 + hex
# =============================================================================


def test_version_and_target_id_length_and_hex():
    node_input = _make_node_input()
    profile = _make_profile([10.0, 20.0, 30.0])
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    assert len(ts.target_set_version) == 64
    int(ts.target_set_version, 16)
    for t in ts.targets:
        assert len(t.target_id) == 64
        int(t.target_id, 16)


# =============================================================================
# §24 空 Peak 是合法 Target Set
# =============================================================================


def test_empty_peak_is_valid_target_set():
    node_input = _make_node_input()
    # 无 peak，但 profile_rows 非空（profile_hash 为真实 hash，非 "empty"）
    profile = _make_profile([])
    # _make_profile([]) 产生空 peak_rows + 空 profile_rows；手动补非空 profile_rows
    profile_rows = [{
        "price_low": 9.95, "price_high": 10.05, "price_mid": 10.0,
        "bullish_volume": 1.0, "bearish_volume": 1.0, "total_volume": 2.0,
        "is_peak": False, "is_poc": False, "is_value_area": False,
    }]
    profile = profile.__class__(
        **{**profile.__dict__, "profile_rows": profile_rows, "profile_hash": "realhash"},
    )
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    assert ts.targets == ()
    assert ts.all_peak_prices == ()
    assert ts.node_regions_hash == "empty"
    assert len(ts.target_set_version) == 64


# =============================================================================
# §45 updated_through
# =============================================================================


def test_updated_through_uses_last_completed_15m():
    node_input = _make_node_input(m15_count=4000, m15_end="2026-09-12 14:45")
    profile = _make_profile([20.0], bars_15m_count=4000)
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    assert ts.updated_through == pd.Timestamp("2026-09-12 14:45")
    assert ts.to_dict()["updated_through"] == "2026-09-12T14:45:00"


# =============================================================================
# §26 to_dict JSON-safe
# =============================================================================


def test_to_dict_json_safe():
    node_input = _make_node_input()
    profile = _make_profile([10.0, 20.0, 30.0])
    ts = NodeMonitorTargetService.build_target_set(node_input, profile)
    blob = json.dumps(ts.to_dict(), allow_nan=False)
    parsed = json.loads(blob)
    assert parsed["schema_version"] == _NODE_MONITOR_TARGET_SCHEMA_VERSION
    assert parsed["target_set_version"] == ts.target_set_version
    assert len(parsed["targets"]) == 3
    assert parsed["all_peak_prices"] == [10.1234, 20.5678, 30.0] or parsed["all_peak_prices"] == [10.0, 20.0, 30.0]


# =============================================================================
# §7-§9 source identity 缺失拒绝
# =============================================================================


def test_daily_source_hash_missing():
    node_input = _make_node_input(daily_source_hash="")
    profile = _make_profile([20.0])
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "daily_source_hash_missing"


def test_m15_source_hash_missing():
    node_input = _make_node_input(m15_source_hash="")
    profile = _make_profile([20.0])
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "m15_source_hash_missing"


def test_factor_hash_missing():
    node_input = _make_node_input(daily_adj_factor_hash="", m15_adj_factor_hash="")
    profile = _make_profile([20.0], adj_factor_hash="FACTOR1")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "factor_hash_missing"


# =============================================================================
# §12 profile bar-count mismatch
# =============================================================================


def test_profile_daily_count_mismatch():
    node_input = _make_node_input(daily_count=250)
    profile = _make_profile([20.0], daily_bars_count=249)
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_daily_count_mismatch"


def test_profile_m15_count_mismatch():
    node_input = _make_node_input(m15_count=4000)
    profile = _make_profile([20.0], bars_15m_count=3999)
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_m15_count_mismatch"


# =============================================================================
# §25 profile metadata 缺失拒绝
# =============================================================================


def test_profile_algorithm_version_missing():
    node_input = _make_node_input()
    profile = _make_profile([20.0], algorithm_version="")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_algorithm_version_missing"


def test_profile_contract_fingerprint_missing():
    node_input = _make_node_input()
    profile = _make_profile([20.0], contract_fingerprint="")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_contract_fingerprint_missing"


def test_profile_hash_missing():
    node_input = _make_node_input()
    profile = _make_profile([20.0], profile_hash="")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_hash_missing"


def test_profile_hash_invalid_when_rows_present():
    node_input = _make_node_input(availability="available")
    # 非空 profile_rows 却 hash 为 "empty"
    profile = _make_profile([20.0], profile_hash="empty")
    with pytest.raises(NodeMonitorTargetUnavailableError) as exc:
        NodeMonitorTargetService.build_target_set(node_input, profile)
    assert exc.value.reason == "profile_hash_invalid"


# =============================================================================
# §19 _stable_sha256 helper
# =============================================================================


def test_stable_sha256_helper():
    p1 = {"a": 1, "b": [1, 2]}
    p2 = {"b": [1, 2], "a": 1}
    assert _stable_sha256(p1) == _stable_sha256(p2)
    assert len(_stable_sha256(p1)) == 64
    # 不同 payload → 不同 hash
    assert _stable_sha256({"a": 1}) != _stable_sha256({"a": 2})


# =============================================================================
# §46 / §47 / §48 架构守护（无 wall-clock / 无 I/O / 仅经 engine 消费 canonical）
# =============================================================================


def _service_source() -> str:
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "node_monitor_target_service.py"
    return p.read_text(encoding="utf-8")


def test_no_wall_clock_in_service():
    src = _service_source()
    forbidden = [
        "datetime.now", "datetime.utcnow", "now_shanghai",
        "time.time", "Timestamp.now", "datetime.utcnow",
        "created_at", "calculated_at", "request_time",
    ]
    for tok in forbidden:
        assert tok not in src, f"service 不得包含 wall-clock 标记: {tok}"


def test_no_io_or_low_level_imports_in_service():
    src = _service_source()
    forbidden = [
        "MarketDataAggregationService", "bar_repository", "get_pytdx",
        "Pytdx", "Eastmoney", "Redis", "AsyncSession", "select(",
        "get_xdxr", "BusinessDateAdjustmentService",
    ]
    for tok in forbidden:
        assert tok not in src, f"service 不得包含 I/O 依赖: {tok}"
    # 只通过 node_cluster_engine 消费 canonical contract
    assert "from app.services.node_cluster_engine import" in src
    assert "from app.services.node_cluster_input_provider import" in src
    # 禁止直接导入底层 VP
    assert "unified_volume_profile import" not in src
