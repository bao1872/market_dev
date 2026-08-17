"""REVIEW-REPLAY-DATASET-V1（DATASET-1）纯函数单测。

PURE_UNIT：不连库，不 import 任何数据库工厂/引擎或 app.db 相关模块
（conftest 会对这些 token 做源码扫描并在纯单元模式跳过，因此本文件
只从 probe 模块导入纯函数，且注释中也不得出现会被扫描命中的 token）。

覆盖 REVIEW-REPLAY-DATASET-V1-IMPLEMENTATION.md §3.4 测试清单 1-9。
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from scripts.review_scope_dynamics_probe import (
    build_manifest,
    build_views,
    compute_date_ranges,
    find_duplicate_pks,
    lineage_closure_l2,
    load_manifest,
    logical_pk,
    validate_manifest_contract,
    _serialize_cell,
    _write_jsonl_gz,
    _iter_jsonl_gz,
    _write_parquet,
    _DOMAIN_LOGICAL_PKS,
)


# ---------------------------------------------------------------------------
# helpers（合成数据，确定性）
# ---------------------------------------------------------------------------


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _make_manifest(**overrides) -> dict:
    axis = _weekdays(date(2024, 1, 1), date(2025, 7, 31))
    asof = axis[-1]
    dr = compute_date_ranges(asof, axis, history=120, warmup=160, bar_lookback_calendar_days=400)
    m = build_manifest(
        dataset_dir_name="review-source-abc123def456-v1",
        capture_git_sha="a" * 40,
        base_dev_sha="b" * 40,
        asof=asof,
        transaction_timestamp=datetime(2025, 7, 31, 15, 30, 0, tzinfo=UTC),
        snapshot_started_at_utc=datetime(2025, 7, 31, 15, 29, 0, tzinfo=UTC),
        date_ranges=dr,
        row_counts={"instruments.jsonl.gz": 1},
        raw_files={
            "instruments.jsonl.gz": {
                "rows": 1,
                "compressed_sha256": "c" * 64,
                "content_sha256": "d" * 64,
            }
        },
        capture_status="succeeded",
        contract_versions_observed={"review-history-v2": 100},
        coverage={"daily_state_instruments": 5000},
    )
    m.update(overrides)
    return m


# ---------------------------------------------------------------------------
# 1. 序列化契约
# ---------------------------------------------------------------------------


def test_serialize_decimal_not_float():
    v = _serialize_cell(Decimal("12.3400"))
    assert isinstance(v, str)
    assert v == "12.3400"
    assert not isinstance(v, float)


def test_serialize_uuid_date_datetime():
    assert _serialize_cell(date(2025, 1, 2)) == "2025-01-02"
    dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert _serialize_cell(dt) == "2025-01-02T03:04:05+00:00"
    # naive datetime → 补 UTC
    naive = datetime(2025, 1, 2, 3, 4, 5)
    assert _serialize_cell(naive) == "2025-01-02T03:04:05+00:00"


# ---------------------------------------------------------------------------
# 2. deterministic gzip（mtime=0）
# ---------------------------------------------------------------------------


def test_jsonl_deterministic_gzip_mtime0(tmp_path):
    rows = [
        {"b": 2, "a": "x", "nested": {"k2": [3, 1], "k1": "v"}},
        {"z": "1.50", "d": "2025-01-02"},
    ]
    # gzip 头会内嵌 basename：用相同 basename 但不同目录验证字节级确定性
    p1 = tmp_path / "d1" / "same.jsonl.gz"
    p2 = tmp_path / "d2" / "same.jsonl.gz"
    _write_jsonl_gz(str(p1), rows)
    _write_jsonl_gz(str(p2), rows)
    assert p1.read_bytes() == p2.read_bytes()
    got = list(_iter_jsonl_gz(str(p1)))
    assert got == rows


# ---------------------------------------------------------------------------
# 3. compute_date_ranges
# ---------------------------------------------------------------------------


def test_compute_date_ranges():
    axis = _weekdays(date(2024, 1, 1), date(2025, 7, 31))
    asof = axis[-1]
    dr = compute_date_ranges(asof, axis, history=120, warmup=160, bar_lookback_calendar_days=400)

    assert dr["asof"] == asof.isoformat()
    assert len(dr["analysis_axis"]) == 120
    assert len(dr["warmup_axis"]) == 160
    # source_fact_start == warmup_axis[0]
    assert dr["source_fact_start"] == dr["warmup_axis"][0]
    source_fact = date.fromisoformat(dr["source_fact_start"])
    # states_start = source_fact_start 前一个交易日（warmup 足够长时）
    idx = axis.index(source_fact)
    assert idx > 0
    assert dr["states_start"] == axis[idx - 1].isoformat()
    # bars_start = source_fact_start - 400 日历日
    assert dr["bars_start"] == (source_fact - timedelta(days=400)).isoformat()
    # events 以 source_fact_start 为左端（date-prefix 边界）
    assert dr["events_range"] == [dr["source_fact_start"], dr["asof"]]
    assert dr["calendar_range"] == [dr["bars_start"], dr["asof"]]


def test_compute_date_ranges_raises_when_asof_missing():
    with pytest.raises(ValueError):
        compute_date_ranges(date(2025, 1, 2), [date(2025, 1, 3)], history=120)


# ---------------------------------------------------------------------------
# 4. manifest roundtrip
# ---------------------------------------------------------------------------


def test_manifest_roundtrip(tmp_path):
    m = _make_manifest()
    path = tmp_path / "manifest.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    loaded = load_manifest(str(path))
    assert loaded == m


# ---------------------------------------------------------------------------
# 5. manifest 契约
# ---------------------------------------------------------------------------


def test_manifest_contract():
    m = _make_manifest()
    assert validate_manifest_contract(m) == []
    # P0：禁止 membership_semantics
    assert "membership_semantics" not in m
    # membership readiness：唯一 SSOT，含 current/historical_pit
    sms = m["scope_membership_sources"]
    assert sms["market"]["historical_pit"] == "not_available"
    assert sms["major_index"]["current"] == "deferred"
    assert sms["style"]["current"] == "deferred"
    # source_readiness.first_pyramid_history 由 capture 结果生成（非硬编码 available）
    fph = m["source_readiness"]["first_pyramid_history"]
    assert fph["capture_status"] == "succeeded"
    assert fph["contract_versions_observed"] == {"review-history-v2": 100}
    # source_readiness 不再重复判定 membership（仅 scope_membership 指针）
    sm = m["source_readiness"]["scope_membership"]
    assert sm["status"] == "family_dependent"
    assert sm["detail_ref"] == "scope_membership_sources"
    # market_data.vwap_raw_source == unavailable
    assert m["market_data"]["vwap_raw_source"] == "unavailable"
    # review_contract.prd_contract_copy 存在
    assert m["review_contract"]["prd_contract_copy"]
    # membership_snapshot_at == transaction_timestamp
    assert m["membership_snapshot_at"] == m["transaction_timestamp"]


def test_manifest_contract_violations_detected():
    m = _make_manifest(membership_semantics="should-be-forbidden")
    m["scope_membership_sources"]["market"]["historical_pit"] = "available"
    m["source_readiness"]["first_pyramid_history"].pop("capture_status", None)
    m["market_data"]["vwap_raw_source"] = "available"
    m["review_contract"]["prd_contract_copy"] = ""
    m["membership_snapshot_at"] = "other"
    violations = validate_manifest_contract(m)
    assert any("membership_semantics" in v for v in violations)
    assert any("historical_pit 必须 == not_available" in v for v in violations)
    assert any("capture_status" in v for v in violations)
    assert any("vwap_raw_source 必须 == unavailable" in v for v in violations)
    assert any("prd_contract_copy 必须存在" in v for v in violations)
    assert any("membership_snapshot_at 必须 == transaction_timestamp" in v for v in violations)


# ---------------------------------------------------------------------------
# 6. logical views 确定性 + union
# ---------------------------------------------------------------------------


def _sample_boards():
    return [
        {"id": "b1", "external_code": "C001", "name": "概念A", "type": "concept", "is_active": True},
        {"id": "b2", "external_code": "C002", "name": "概念B", "type": "concept", "is_active": True},
        {"id": "b3", "external_code": "I001", "name": "行业甲", "type": "industry", "is_active": True},
        {"id": "b4", "external_code": "I002", "name": "行业乙", "type": "industry", "is_active": False},
    ]


def _sample_memberships():
    return [
        {"board_id": "b1", "instrument_id": "i1"},
        {"board_id": "b1", "instrument_id": "i2"},
        {"board_id": "b2", "instrument_id": "i2"},
        {"board_id": "b2", "instrument_id": "i3"},
        {"board_id": "b3", "instrument_id": "i1"},
        {"board_id": "b4", "instrument_id": "i9"},  # inactive board 的成员不进入 active view
    ]


def test_views_tie_breaker_determinism():
    boards = _sample_boards()
    memberships = _sample_memberships()
    v1 = build_views(boards, memberships)
    v2 = build_views(boards, memberships)
    assert v1 == v2
    assert sorted(v1.keys()) == [
        "all_concepts", "all_industries", "capacity_4096", "dev_500", "representative_sample",
    ]
    # 每个 view：derived_instrument_ids == union(memberships[scope_keys]) 且含 membership_usage 枚举
    for vid, view in v1.items():
        scope_set = set(view["scope_keys"])
        union = {str(m["instrument_id"]) for m in memberships if str(m["board_id"]) in scope_set}
        assert set(view["derived_instrument_ids"]) == union, vid
        usage = view["membership_usage"]
        assert set(usage) == {"current", "historical"}
        assert usage["current"] in ("available", "deferred")
        assert usage["historical"] in ("available", "not_available", "deferred")
    # inactive board 不进入 active view union
    assert "i9" not in set(v1["all_concepts"]["derived_instrument_ids"])
    assert "i9" not in set(v1["all_industries"]["derived_instrument_ids"])


def test_logical_pk_duplicate():
    for domain, pk in _DOMAIN_LOGICAL_PKS.items():
        assert logical_pk(domain) == pk
    rows = [
        {"instrument_id": "i1", "trade_date": "2025-01-02"},
        {"instrument_id": "i1", "trade_date": "2025-01-02"},
        {"instrument_id": "i2", "trade_date": "2025-01-02"},
    ]
    dups = find_duplicate_pks(rows, ("instrument_id", "trade_date"))
    assert dups == [("i1", "2025-01-02")]
    with pytest.raises(ValueError):
        logical_pk("not_a_domain")


# ---------------------------------------------------------------------------
# 8. lineage closure L2
# ---------------------------------------------------------------------------


def test_lineage_closure_l2():
    d5_rows = [
        {"instrument_id": "i1", "source_history_run_id": "r1"},
        {"instrument_id": "i2", "source_history_run_id": "r2"},
        {"instrument_id": "i3", "source_history_run_id": None},
    ]
    history_runs = [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]
    closure = lineage_closure_l2(d5_rows, history_runs)
    assert closure == {"r1", "r2"}
    # history_runs 仅用于孤儿检测：闭包 - available = {"r1","r2"} - {"r1","r2","r3"} = ∅
    assert not (closure - {r["id"] for r in history_runs})


# ---------------------------------------------------------------------------
# 9. parquet decimal roundtrip（无 pyarrow 时跳过）
# ---------------------------------------------------------------------------


def test_parquet_decimal_roundtrip(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    rows = [
        {
            "instrument_id": "i1",
            "trade_date": "2025-01-02",
            "open": "12.3400",
            "close": "12.3500",
            "adj_factor": "1.00000000",
        },
        {
            "instrument_id": "i2",
            "trade_date": "2025-01-02",
            "open": "3.1000",
            "close": None,
            "adj_factor": "0.98000000",
        },
    ]
    out = _write_parquet(rows, str(tmp_path / "bars_daily.parquet"), "bars_daily")
    assert out["rows"] == 2
    t = pq.read_table(out["path"])
    df = t.to_pandas()
    # decimal128 → pandas 保留 Decimal 精度（不丢失尾零 / 不转 float）
    assert df.loc[0, "open"] == Decimal("12.3400")
    assert df.loc[1, "open"] == Decimal("3.1000")
    assert df.loc[1, "close"] is None
    assert df.loc[0, "adj_factor"] == Decimal("1.00000000")
