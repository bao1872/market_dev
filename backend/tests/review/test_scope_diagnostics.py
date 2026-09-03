"""Targeted unit tests for Review scope diagnostics (R3 History / Cross-sectional).

Pure, no DB, no PG. Covers the contract-critical logic:
- 20D rolling math (lagged baseline, null != 0, std==0 -> None);
- published-run lineage selection (_select_published_facts);
- field rolling alignment (_compute_field_rolling).

DB-backed end-to-end history run-selection (published run resolution across a
date window) is a targeted-PG claim and is NOT RUN here (see task report).
"""
from __future__ import annotations

import types
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.first_pyramid_semantics import Direction
from app.domain.review.analysis.observation_stats import (
    empirical_percentile,
    safe_mean,
    safe_std,
    zscore,
)
from app.domain.review.scope_observation import (
    _build_changed_members,
    _duration_buckets,
)
from app.services.review_scope_diagnostics_service import (
    _compute_field_rolling,
    _select_published_facts,
    build_canonical_by_date,
)


@dataclass
class _StubFact:
    trade_date: date
    review_run_id: Any
    observation_payload: Any


# ---------------------------------------------------------------------------
# observation_stats
# ---------------------------------------------------------------------------


def test_safe_mean_excludes_none_and_never_coerces_zero():
    assert safe_mean([1.0, 2.0, None, 3.0]) == 2.0
    assert safe_mean([None, None]) is None
    # null is not treated as 0
    assert safe_mean([None, 10.0]) == 10.0


def test_safe_std_requires_two_finite():
    assert safe_std([5.0]) is None
    assert safe_std([1.0, 3.0]) == 1.0  # population std of [1,3]


def test_zscore_std_zero_is_none_not_zero():
    # baseline std == 0 must yield None, never a fake z = 0
    assert zscore(5.0, 5.0, 0.0) is None
    assert zscore(5.0, 5.0, None) is None
    assert zscore(None, 5.0, 1.0) is None
    assert zscore(7.0, 5.0, 1.0) == 2.0


def test_empirical_percentile_self_inclusive():
    # value == median of [1,2,3,4,5] -> rank (2 + 0.5*1)/5*100 = 50
    assert empirical_percentile(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 50.0
    assert empirical_percentile(None, [1.0, 2.0]) is None


# ---------------------------------------------------------------------------
# lineage selection
# ---------------------------------------------------------------------------


def test_select_published_facts_drops_non_published_run():
    d = date(2026, 3, 1)
    pub = "run-pub"
    other = "run-other"
    rows = [
        _StubFact(d, pub, {"price": {"equal_weight_return": 0.01}}),
        _StubFact(d, other, {"price": {"equal_weight_return": 0.99}}),  # later same-day run
    ]
    out = _select_published_facts(rows, {d: pub})
    assert list(out.keys()) == [d]
    assert out[d]["price"]["equal_weight_return"] == 0.01  # published only


def test_select_published_facts_no_pointer_yields_empty():
    d = date(2026, 3, 1)
    rows = [_StubFact(d, "run-x", {"price": {}})]
    assert _select_published_facts(rows, {d: None}) == {}


# ---------------------------------------------------------------------------
# P1-2 — history date axis = formal published Review dates, slots preserved
# ---------------------------------------------------------------------------


def test_build_canonical_by_date_preserves_missing_slots_as_null():
    """正式发布 D1,D2,D3；fact 仅 D1,D3。日期槽必须全部保留，D2 为 null。

    违反此契约的表现：D2 被删掉 → 轴变成 [D1,D3]，图上 D1→D3 被连成连续。
    """
    d1, d2, d3 = date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)
    formal_dates = [d1, d2, d3]
    run_id_by_date = {d1: "r1", d2: "r2", d3: "r3"}
    # D2 的 run 没有写出 fact（scope 当日未 ready）
    fact_by_run = {"r1": {"v": 1.0}, "r3": {"v": 3.0}}

    canonical = build_canonical_by_date(formal_dates, run_id_by_date, fact_by_run)

    # 日期轴 = 正式发布日期，不得由 fact 存在性决定
    assert list(canonical.keys()) == [d1, d2, d3]
    # 缺失 fact 的日期保留槽，series 值 = null（绝不 drop / forward-fill）
    assert canonical[d1] == {"v": 1.0}
    assert canonical[d2] is None
    assert canonical[d3] == {"v": 3.0}


def test_build_canonical_by_date_broken_pointer_excluded_upstream():
    """broken pointer（pointer 存在但 run 未正式发布）不得进入历史轴。

    build_canonical_by_date 自身只按已解析的 run_id_by_date 索引；formal owner
    （list_formally_published_review_dates）已在日期轴层排除 broken pointer，因此
    run_id_by_date 中根本不会出现该日期。
    """
    d1, d_bad = date(2026, 3, 1), date(2026, 3, 5)
    # D_BAD 没有进入正式日期轴（broken pointer 被 formal owner 排除）
    formal_dates = [d1]
    run_id_by_date = {d1: "r1"}
    fact_by_run = {"r1": {"v": 1.0}, "r_bad": {"v": 9.9}}
    canonical = build_canonical_by_date(formal_dates, run_id_by_date, fact_by_run)
    assert d_bad not in canonical
    assert canonical[d1] == {"v": 1.0}


def test_duration_buckets_histogram_edges():
    """[P1-4] dsa_dir_bars 直方图必须落在已确认的 7 档 duration bucket 合同上，纯直方图无新算法。"""
    values = [1, 2, 3, 4, 6, 7, 12, 13, 24, 30, None, float("nan")]
    buckets = _duration_buckets(values)
    labels = [b["label"] for b in buckets]
    # 已确认合同：≤2 / 3-4 / 5-9 / 10-19 / 20-29 / 30-59 / ≥60
    assert labels == ["≤2", "3-4", "5-9", "10-19", "20-29", "30-59", "≥60"], labels
    counts = [b["count"] for b in buckets]
    total = sum(counts)
    # None / NaN 不计入有限样本
    assert total == 10, total
    assert counts[0] == 2  # ≤2: 1,2
    assert counts[1] == 2  # 3-4: 3,4
    assert counts[2] == 2  # 5-9: 6,7
    assert counts[3] == 2  # 10-19: 12,13
    assert counts[4] == 1  # 20-29: 24
    assert counts[5] == 1  # 30-59: 30
    assert counts[6] == 0  # ≥60: 无
    # 空输入 -> 全 0 桶，不报错
    empty = _duration_buckets([])
    assert sum(b["count"] for b in empty) == 0


def test_changed_members_deterministic_by_member_id():
    """[DSA] canonical changed_members 必须按 member_id 稳定排序，与 supplier/DB 返回顺序无关。"""
    labels = {
        Direction.UP: "Up",
        Direction.DOWN: "Down",
        Direction.SIDEWAYS: "Neutral",
    }

    def mk(mid, cur, prev):
        return types.SimpleNamespace(member_id=mid, trend=cur, t1_trend=prev)

    t1_set = {"A", "B", "C"}
    # 两组相同业务成员，不同输入顺序
    order1 = [
        mk("A", Direction.UP, Direction.SIDEWAYS),
        mk("B", Direction.DOWN, Direction.UP),
        mk("C", Direction.UP, Direction.DOWN),
    ]
    order2 = [
        mk("C", Direction.UP, Direction.DOWN),
        mk("A", Direction.UP, Direction.SIDEWAYS),
        mk("B", Direction.DOWN, Direction.UP),
    ]
    out1 = _build_changed_members(order1, t1_set, labels)
    out2 = _build_changed_members(order2, t1_set, labels)
    # 输入顺序不同，但 canonical payload 必须完全一致（按 member_id 排序）
    assert out1 == out2
    assert [m["member_id"] for m in out1] == ["A", "B", "C"]
    # 仅状态变化的成员进入；stable 成员排除
    assert all(m["current_state"] != m["previous_state"] for m in out1)


# ---------------------------------------------------------------------------
# field rolling (lagged baseline)
# ---------------------------------------------------------------------------


def test_compute_field_rolling_lagged_and_aligned():
    # series length 5; baseline(i) = values strictly before i
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    r = _compute_field_rolling(series, window=20)
    # first point: no baseline -> mean/std/z/pct all None, baselineCount 0
    assert r["mean20"][0] is None
    assert r["zscore20"][0] is None
    assert r["baselineCount"][0] == 0
    # point i=4: baseline = [1,2,3,4] mean=2.5 std=population(1.25)=~1.118
    # value 5 -> z = (5-2.5)/1.1180... ~ 2.236
    assert r["mean20"][4] == 2.5
    assert abs(r["zscore20"][4] - 2.23606797749979) < 1e-9
    assert r["baselineCount"][4] == 4
    # all arrays aligned to series length
    assert len(r["zscore20"]) == 5


def test_compute_field_rolling_missing_values_excluded_from_baseline():
    series = [1.0, None, 3.0, None, 5.0]
    r = _compute_field_rolling(series, window=20)
    # i=4 baseline = finite values before i = [1.0, 3.0] (None excluded, not 0)
    assert r["baselineCount"][4] == 2
    assert r["mean20"][4] == 2.0
    assert r["zscore20"][4] == (5.0 - 2.0) / 1.0
