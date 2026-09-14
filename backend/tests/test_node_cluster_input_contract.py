"""R1 生产合同测试：Node 输入数据合同、历史覆盖证明与缓存正确性。

覆盖冻结实现合同的 9 个场景：
- 老股票 + DB 1200/4000 15m + 理论历史足够 → INPUT_CONTRACT_VIOLATION
- 新股 + authoritative listing_date + 最大可能 15m < 4000 → degraded
- listing_date=NULL + 15m 不足 → 不得 degraded，fail closed → INPUT_CONTRACT_VIOLATION
- daily 100/250 + 老股票 → INPUT_CONTRACT_VIOLATION
- daily 100/250 + 真正新股 → degraded
- 250 daily + 4000 completed qfq 15m → available
- 3999 completed + 1 forming → forming 不计入 4000
- 15m 内容变化但 last timestamp 不变 → Node cache miss
- adj factor identity 改变 → cache miss

不重写算法：只验证输入合同、历史覆盖证明与缓存 identity。
"""

from __future__ import annotations

import asyncio
import types
import uuid
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.services.monitor_batch_service import (
    _node_cluster_profile_cache_key,
    _smc_runtime_target_cache_key,
)
from app.services.node_cluster_engine import build_engine_cache_key
from app.services.node_cluster_input_provider import NodeClusterInputProvider

# ---------------------------------------------------------------------------
# 1. availability 决策矩阵（纯逻辑，proof 驱动）
# ---------------------------------------------------------------------------

def _p(proven: bool, reason: str = "x") -> tuple[bool, str]:
    return (proven, reason)


@pytest.mark.parametrize(
    "daily_count,m15_count,daily_proof,m15_proof,exp_avail,exp_reason",
    [
        # 正常合同
        (250, 4000, _p(False), _p(False), "available", None),
        # 老股票 DB 缺口（理论历史足够）→ 系统缺口，不得 degraded
        (250, 1200, _p(False, "HISTORY_UNDERFILLED_DB_GAP"), _p(False, "HISTORY_UNDERFILLED_DB_GAP"),
         "unavailable", "INPUT_CONTRACT_VIOLATION"),
        # 新股 + 已证 genuine exhausted → 允许降级
        (250, 144, _p(False), _p(True, "GENUINE_HISTORY_EXHAUSTED"),
         "degraded", "INSUFFICIENT_15M_HISTORY"),
        # 15m==0 + 已证 genuine exhausted → 降级
        (250, 0, _p(False), _p(True, "GENUINE_HISTORY_EXHAUSTED"),
         "degraded", "INSUFFICIENT_15M_HISTORY"),
        # listing_date=NULL + 15m 不足 → 不得 degraded
        (250, 1200, _p(False, "MISSING_HISTORY_BOUNDARY_PROOF"), _p(False, "MISSING_HISTORY_BOUNDARY_PROOF"),
         "unavailable", "INPUT_CONTRACT_VIOLATION"),
        # daily 100/250 + 老股票（无法证明耗尽）→ violation
        (100, 4000, _p(False), _p(False), "unavailable", "INPUT_CONTRACT_VIOLATION"),
        # daily 100/250 + 真正新股（已证耗尽）→ degraded
        (100, 4000, _p(True, "GENUINE_HISTORY_EXHAUSTED"), _p(False),
         "degraded", "INSUFFICIENT_DAILY_HISTORY"),
        # daily < 10 绝对下限
        (9, 4000, _p(False), _p(False), "unavailable", "INSUFFICIENT_DAILY_BARS"),
        # daily < 10 即使已证耗尽仍 unavailable
        (5, 0, _p(True, "GENUINE_HISTORY_EXHAUSTED"), _p(True, "GENUINE_HISTORY_EXHAUSTED"),
         "unavailable", "INSUFFICIENT_DAILY_BARS"),
        # 3999 completed + 1 forming：forming 在过滤后不计入 → 仍不足且无法证明耗尽
        (250, 3999, _p(False), _p(False), "unavailable", "INPUT_CONTRACT_VIOLATION"),
    ],
)
def test_availability_decision_matrix(
    daily_count, m15_count, daily_proof, m15_proof, exp_avail, exp_reason,
):
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count, m15_count, daily_proof, m15_proof,
    )
    assert avail == exp_avail, f"avail={avail} expected={exp_avail}"
    assert reason == exp_reason, f"reason={reason} expected={exp_reason}"


def test_classify_boundaries():
    # ok
    assert NodeClusterInputProvider._classify(250, 250, False) == "ok"
    assert NodeClusterInputProvider._classify(4000, 4000, False) == "ok"
    # degraded only when proven
    assert NodeClusterInputProvider._classify(144, 4000, True) == "degraded"
    assert NodeClusterInputProvider._classify(100, 250, True) == "degraded"
    # violation when not proven
    assert NodeClusterInputProvider._classify(1200, 4000, False) == "violation"
    assert NodeClusterInputProvider._classify(100, 250, False) == "violation"


# ---------------------------------------------------------------------------
# 2. 15m completed contract：forming bar 不得计入
# ---------------------------------------------------------------------------

def _build_15m_df(timestamps: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [pd.Timestamp(t).tz_localize("Asia/Shanghai") for t in timestamps]
    )
    return pd.DataFrame({"close": [1.0] * len(idx)}, index=idx)


def test_filter_unfinished_15m_bars_drops_forming():
    # now = 2026-06-24 14:50（交易时段内）；15:00 这根为 forming
    now = pd.Timestamp("2026-06-24 14:50:00").tz_localize("Asia/Shanghai")
    bars = _build_15m_df([
        "2026-06-24 09:45:00", "2026-06-24 10:00:00", "2026-06-24 11:30:00",
        "2026-06-24 13:15:00", "2026-06-24 14:30:00", "2026-06-24 14:45:00",
        "2026-06-24 15:00:00",  # forming
    ])
    filtered = NodeClusterInputProvider._filter_unfinished_15m_bars(bars, now)
    assert len(filtered) == 6
    assert filtered.index.max() == pd.Timestamp("2026-06-24 14:45:00").tz_localize("Asia/Shanghai")
    assert pd.Timestamp("2026-06-24 15:00:00").tz_localize("Asia/Shanghai") not in filtered.index


def test_filter_unfinished_15m_bars_empty():
    empty = pd.DataFrame()
    assert NodeClusterInputProvider._filter_unfinished_15m_bars(empty, None).empty


def test_filter_unfinished_15m_bars_fail_closed_when_now_none():
    bars = _build_15m_df([
        "2026-06-24 14:30:00", "2026-06-24 14:45:00", "2026-06-24 15:00:00",
    ])
    # now=None → fail closed：丢弃最后一根（宁可少算）
    filtered = NodeClusterInputProvider._filter_unfinished_15m_bars(bars, None)
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# 3. history exhaustion proof：单向安全上界
# ---------------------------------------------------------------------------

def _patch_exhaustion(monkeypatch, listing_date, trading_days):
    async def _fake_listing_date(*a, **k):
        return listing_date

    async def _fake_count_trading_days(*a, **k):
        return trading_days

    monkeypatch.setattr(
        "app.repositories.bar_repository._get_listing_date",
        _fake_listing_date,
    )
    monkeypatch.setattr(
        "app.services.board_facts_service._count_trading_days_between",
        _fake_count_trading_days,
    )


def test_exhaustion_proof_old_stock_not_proven(monkeypatch):
    # 老股票：上市 10 年，约 4000 交易日 → 理论最大远超 4000
    _patch_exhaustion(monkeypatch, date(2010, 1, 1), 4000)
    proofs = asyncio.run(
        NodeClusterInputProvider._compute_exhaustion_proofs(
            MagicMock(), uuid.UUID(int=1), date(2026, 6, 1),
        )
    )
    assert proofs["1d"] == (False, "HISTORY_UNDERFILLED_DB_GAP")
    assert proofs["15m"] == (False, "HISTORY_UNDERFILLED_DB_GAP")


def test_exhaustion_proof_new_stock_proven(monkeypatch):
    # 上市 30 交易日 → 15m 最大 30*16=480 < 4000，daily 最大 30 < 250
    _patch_exhaustion(monkeypatch, date(2026, 1, 1), 30)
    proofs = asyncio.run(
        NodeClusterInputProvider._compute_exhaustion_proofs(
            MagicMock(), uuid.UUID(int=1), date(2026, 2, 1),
        )
    )
    assert proofs["1d"] == (True, "GENUINE_HISTORY_EXHAUSTED")
    assert proofs["15m"] == (True, "GENUINE_HISTORY_EXHAUSTED")


def test_exhaustion_proof_listing_date_null(monkeypatch):
    # listing_date=NULL → 绝不声称 exhausted，fail closed
    _patch_exhaustion(monkeypatch, None, 4000)
    proofs = asyncio.run(
        NodeClusterInputProvider._compute_exhaustion_proofs(
            MagicMock(), uuid.UUID(int=1), date(2026, 6, 1),
        )
    )
    assert proofs["1d"] == (False, "MISSING_HISTORY_BOUNDARY_PROOF")
    assert proofs["15m"] == (False, "MISSING_HISTORY_BOUNDARY_PROOF")


def test_exhaustion_proof_boundary_30_trading_days(monkeypatch):
    # 验证 “上市 30 交易日 → 15m 最大 480 < 4000” 的精确上界
    _patch_exhaustion(monkeypatch, date(2026, 1, 1), 30)
    proofs = asyncio.run(
        NodeClusterInputProvider._compute_exhaustion_proofs(
            MagicMock(), uuid.UUID(int=1), date(2026, 2, 1),
        )
    )
    assert proofs["15m"][0] is True  # 480 < 4000


# ---------------------------------------------------------------------------
# 4. 缓存 identity：15m 内容变化 / adj 变化必须 cache miss
# ---------------------------------------------------------------------------

def _fake_node_input(**overrides) -> object:
    base = {
        "daily_source_hash": "d-hash",
        "m15_source_hash": "m15-hash",
        "daily_adj_factor_hash": "adj-hash",
        "adjustment_as_of": "2026-06-01",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_monitor_profile_cache_key_binds_15m_hash():
    a = _node_cluster_profile_cache_key(1, "d-last", "m15-last", _fake_node_input())
    b = _node_cluster_profile_cache_key(
        1, "d-last", "m15-last", _fake_node_input(m15_source_hash="m15-hash-CHANGED"),
    )
    assert a != b  # 15m 内容变化 → cache miss（即使 last timestamp 相同）


def test_monitor_profile_cache_key_binds_adj_as_of():
    a = _node_cluster_profile_cache_key(1, "d-last", "m15-last", _fake_node_input())
    b = _node_cluster_profile_cache_key(
        1, "d-last", "m15-last", _fake_node_input(adjustment_as_of="2026-06-02"),
    )
    assert a != b  # 复权点变化 → cache miss


def test_monitor_smc_cache_key_binds_15m_hash():
    a = _smc_runtime_target_cache_key(1, "d-last", _fake_node_input())
    b = _smc_runtime_target_cache_key(
        1, "d-last", _fake_node_input(m15_source_hash="m15-hash-CHANGED"),
    )
    assert a != b


def test_engine_cache_key_binds_15m_and_as_of():
    profile_a = types.SimpleNamespace(
        daily_source_hash="d", bars_15m_source_hash="m15",
        adjustment_as_of="2026-06-01", algorithm_version="v1", contract_fingerprint="fp",
    )
    profile_b = types.SimpleNamespace(
        daily_source_hash="d", bars_15m_source_hash="m15-CHANGED",
        adjustment_as_of="2026-06-01", algorithm_version="v1", contract_fingerprint="fp",
    )
    profile_c = types.SimpleNamespace(
        daily_source_hash="d", bars_15m_source_hash="m15",
        adjustment_as_of="2026-06-02", algorithm_version="v1", contract_fingerprint="fp",
    )
    ka = build_engine_cache_key(1, profile_a)
    assert build_engine_cache_key(1, profile_b) != ka  # 15m 内容变化 → miss
    assert build_engine_cache_key(1, profile_c) != ka  # as_of 变化 → miss


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
