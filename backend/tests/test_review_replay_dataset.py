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
    _boards_select,
    _memberships_select,
    _data_quality_summary,
    _dataset_validate,
    _rows_to_parquet,
    _sha256_file,
    _sha256_content,
    _DOMAIN_LOGICAL_PKS,
    _LINEAGE_FILE_STEMS,
    _RAW_FILE_STEMS,
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


# ---------------------------------------------------------------------------
# DATASET-1.1 回归（ref/优化.md 审查 P0/P1/P2 修复）
# ---------------------------------------------------------------------------


def test_export_select_keys_snake_case():
    """P0-1：D2/D3 导出 projection 必须为 snake_case 契约 key（不含 camelCase）。"""
    b = _boards_select()
    b_keys = {c.name for c in b.selected_columns}
    assert {
        "id", "external_code", "name", "type", "taxonomy", "source",
        "taxonomy_version", "taxonomy_compatibility_key", "hierarchy_level",
        "parent_board_id", "is_active", "membership_version", "updated_at",
    } <= b_keys
    camel = b_keys & {
        "externalCode", "taxonomyVersion", "taxonomyCompatibilityKey",
        "hierarchyLevel", "parentBoardId", "isActive", "membershipVersion", "updatedAt",
    }
    assert not camel, camel

    m = _memberships_select()
    m_keys = {c.name for c in m.selected_columns}
    assert {"board_id", "instrument_id", "updated_at"} <= m_keys
    assert not (m_keys & {"boardId", "instrumentId", "updatedAt"})


def test_dataset_validate_raw_gate_blocks_parquet(tmp_path):
    """P0-2 负向：raw gate FAIL 时 _dataset_validate 返回 2，且不创建 parquet/views。"""
    m = _make_manifest()
    dataset = tmp_path / "ds"
    (dataset / "raw").mkdir(parents=True)
    (dataset / "lineage").mkdir()
    # manifest 声明了 instruments.jsonl.gz，但 raw 目录缺失该文件 → checksum gate FAIL
    (dataset / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    rc = _dataset_validate(str(dataset))
    assert rc == 2
    assert not (dataset / "parquet").exists()
    assert not (dataset / "views").exists()


def _write_full_dataset(dataset):
    """写最小合法 dataset（8 raw 域 + 2 lineage，各 ≥1 行、无重复/无孤儿/日期≤asof），
    返回写好的 manifest（raw_files 已含正确 checksum）。"""
    raw = dataset / "raw"
    lineage = dataset / "lineage"
    raw.mkdir(parents=True)
    lineage.mkdir()

    axis = _weekdays(date(2024, 1, 1), date(2025, 7, 31))
    asof = axis[-1]
    asof_s = asof.isoformat()
    dr = compute_date_ranges(asof, axis, history=120, warmup=160, bar_lookback_calendar_days=400)

    files = {
        "instruments": [
            {"id": "i1", "symbol": "600000", "name": "浦发银行", "market": "SH",
             "pinyin_initials": "PFYH", "status": "active", "listing_date": "1999-11-10",
             "total_share": "1000000000", "float_share": "900000000", "share_as_of": asof_s},
        ],
        "boards": [
            {"id": "b1", "external_code": "C001", "name": "概念A", "type": "concept",
             "taxonomy": "custom", "source": "qstock", "taxonomy_version": "v1",
             "taxonomy_compatibility_key": "k1", "hierarchy_level": 1,
             "parent_board_id": None, "is_active": True, "membership_version": "v1",
             "updated_at": asof_s},
        ],
        "board_memberships_current_snapshot": [
            {"board_id": "b1", "instrument_id": "i1", "updated_at": asof_s},
        ],
        "trading_calendar": [
            {"trade_date": asof_s, "is_trading_day": True, "market": "SH",
             "source": "manual", "status": "closed", "verified_at": asof_s},
        ],
        "first_pyramid_daily_state": [
            {"instrument_id": "i1", "trade_date": asof_s, "algorithm_version": "v2",
             "input_hash": "h1", "source_history_run_id": "r1",
             "history_contract_version": "review-history-v2",
             "state_payload": {"pct": 0.5}},
        ],
        "first_pyramid_events": [
            {"instrument_id": "i1", "algorithm_version": "v2", "event_type": "pyramid",
             "event_id": "e1", "event_time": f"{asof_s}T09:30:00+08:00",
             "history_contract_version": "review-history-v2",
             "event_payload": {"n": 1}},
        ],
        "bars_daily": [
            {"instrument_id": "i1", "trade_date": asof_s, "open": "10.5000",
             "high": "10.8000", "low": "10.2000", "close": "10.6000",
             "volume": "1234.56", "amount": "5678.90", "adj_factor": "1.00000000"},
        ],
        "stock_feature_snapshots_asof": [
            {"instrument_id": "i1", "trade_date": asof_s, "primary_timeframe": "1d",
             "secondary_timeframe": "none", "adj": "none", "schema_version": "v1",
             "source_run_id": "s1", "source_primary_bar_time": asof_s,
             "source_secondary_bar_time": asof_s, "structural_payload": {},
             "temporal_payload": {}, "summary_payload": {}, "degraded_reasons": []},
        ],
        "stock_feature_snapshot_runs": [
            {"id": "s1", "trade_date": asof_s, "schema_version": "v1",
             "primary_timeframe": "1d", "secondary_timeframe": "none", "adj": "none",
             "run_type": "manual", "status": "succeeded", "expected_count": 1,
             "snapshot_count": 1, "failed_count": 0, "skipped_count": 0,
             "failure_rate": "0.0", "started_at": asof_s, "finished_at": asof_s,
             "published_at": asof_s, "metadata_": {}},
        ],
        "first_pyramid_history_runs": [
            {"id": "r1", "scheduler_job_run_id": None, "algorithm_version": "v2",
             "parameter_hash": "ph1", "output_bars": 1, "scope": "concept:C001",
             "expected_count": 1, "succeeded_count": 1, "failed_count": 0,
             "skipped_count": 0, "status": "succeeded", "started_at": asof_s,
             "completed_at": asof_s, "metadata_json": {}},
        ],
    }
    raw_stems = set(_RAW_FILE_STEMS.values())
    raw_files: dict = {}
    row_counts: dict = {}
    for fname, rows in files.items():
        subdir = raw if fname in raw_stems else lineage
        fpath = subdir / f"{fname}.jsonl.gz"
        _write_jsonl_gz(str(fpath), rows)
        raw_files[f"{fname}.jsonl.gz"] = {
            "rows": len(rows),
            "compressed_sha256": _sha256_file(str(fpath)),
            "content_sha256": _sha256_content(str(fpath)),
        }
        row_counts[f"{fname}.jsonl.gz"] = len(rows)
    assert set(raw_files) == {
        f"{s}.jsonl.gz" for s in raw_stems | set(_LINEAGE_FILE_STEMS.values())
    }
    m = _make_manifest(raw_files=raw_files, row_counts=row_counts, date_ranges=dr)
    (dataset / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return m


def test_dataset_validate_full_pass(tmp_path):
    """P0-2 正向 + P1-1：合法 dataset 全 PASS → 返回 0，生成 parquet/views，
    manifest.derived_files 已写回且每项含 rows/sha256/derived_from。"""
    pytest.importorskip("pyarrow")
    dataset = tmp_path / "ds"
    m = _write_full_dataset(dataset)
    rc = _dataset_validate(str(dataset))
    assert rc == 0
    assert (dataset / "parquet").exists()
    assert (dataset / "views").exists()
    reloaded = load_manifest(str(dataset / "manifest.json"))
    derived = reloaded.get("derived_files") or {}
    assert derived, "derived_files 必须已写回"
    for fname, finfo in derived.items():
        assert fname.endswith(".parquet")
        for key in ("rows", "sha256", "derived_from"):
            assert key in finfo, f"{fname}.{key}"
        assert finfo["rows"] == m["raw_files"][finfo["derived_from"].split("/")[-1]]["rows"]
    # 写回后 manifest 仍满足契约
    assert validate_manifest_contract(reloaded) == []


def test_views_determinism_same_metric_external_code_shuffled():
    """P1-4：metric 相同、external_code 相同、id 不同的 board，输入顺序反转 → 输出一致。"""
    boards = [
        {"id": "b1", "external_code": "C001", "name": "概念A", "type": "concept", "is_active": True},
        {"id": "b2", "external_code": "C001", "name": "概念B", "type": "concept", "is_active": True},
    ]
    memberships = [
        {"board_id": "b1", "instrument_id": "i1"},
        {"board_id": "b1", "instrument_id": "i2"},
        {"board_id": "b2", "instrument_id": "i1"},
        {"board_id": "b2", "instrument_id": "i2"},
    ]
    v1 = build_views(boards, memberships)
    v2 = build_views(list(reversed(boards)), memberships)
    assert v1 == v2


def test_views_analysis_axis_date_range_and_trade_dates():
    """P1-4：传入 analysis_axis 时 date_range 为机器可消费 [start,end]，
    representative_sample.trade_dates 为确定性 5 dates；未传时回退说明字符串。"""
    axis = [d.isoformat() for d in _weekdays(date(2025, 6, 1), date(2025, 7, 31))]
    boards = _sample_boards()
    memberships = _sample_memberships()
    views = build_views(boards, memberships, analysis_axis=axis)
    for vid, view in views.items():
        assert view["date_range"] == [axis[0], axis[-1]], vid
    rep = views["representative_sample"]
    n = len(axis)
    expected = [axis[i] for i in sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})]
    assert len(rep["trade_dates"]) == 5
    assert rep["trade_dates"] == expected
    # 无 analysis_axis 兼容路径：回退为说明字符串
    legacy = build_views(boards, memberships)
    assert isinstance(legacy["dev_500"]["date_range"], str)
    assert "manifest.date_ranges" in legacy["dev_500"]["date_range"]
    assert "trade_dates" not in legacy["representative_sample"]


def test_manifest_derived_files_contract():
    """P1-1 契约：derived_files 条目齐全 → 无违规；条目缺字段 → 检出 violation。"""
    m = _make_manifest(
        derived_files={
            "bars_daily.parquet": {
                "rows": 2, "sha256": "a" * 64, "derived_from": "raw/bars_daily.jsonl.gz",
            },
            "instruments.parquet": {
                "rows": 1, "sha256": "b" * 64, "derived_from": "raw/instruments.jsonl.gz",
            },
        }
    )
    assert validate_manifest_contract(m) == []
    m["derived_files"]["bars_daily.parquet"].pop("sha256")
    violations = validate_manifest_contract(m)
    assert any("derived_files.bars_daily.parquet.sha256 缺失" in v for v in violations)


def test_rows_to_parquet_batch_and_decimal_stem(tmp_path):
    """P1-2 + 潜伏 decimal bug：_rows_to_parquet 按 file_stem 解析 decimal 列，
    bars（stem=bars_daily）的 open/close/adj_factor 为 decimal128，且 rows 正确。"""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [
        {"instrument_id": "i1", "trade_date": "2025-01-02",
         "open": "12.3400", "close": "12.3500", "adj_factor": "1.00000000"},
        {"instrument_id": "i2", "trade_date": "2025-01-02",
         "open": "3.1000", "close": None, "adj_factor": "0.98000000"},
    ]
    _write_jsonl_gz(str(raw_dir / "bars_daily.jsonl.gz"), rows)
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()
    info = _rows_to_parquet(str(raw_dir), str(parquet_dir), "bars", "bars_daily", batch_size=1)
    assert info["rows"] == 2
    fields = {f.name: f.type for f in pq.ParquetFile(info["path"]).schema_arrow}
    assert pa.types.is_decimal(fields["open"])
    assert pa.types.is_decimal(fields["close"])
    assert pa.types.is_decimal(fields["adj_factor"])
    t = pq.read_table(info["path"])
    assert t.num_rows == 2


# ---------------------------------------------------------------------------
# DATASET-2.1 回归（ref/优化.md P1-A/P1-B coverage 修复）
# ---------------------------------------------------------------------------


def _write_quality_summary_input(
    dataset,
    *,
    instruments_n: int,
    fact_universe: int,
    analysis_axis: list[str],
    calendar_rows: list[dict],
    state_by_date: dict[str, int],
    bar_by_date: dict[str, int],
) -> dict:
    """写 `_data_quality_summary` 所需的最小合成 raw 文件 + manifest。

    PURE_UNIT：全部本地合成，不连库。返回 manifest（已含
    `source_readiness.first_pyramid_history.coverage.review_fact_universe`）。
    """
    raw = dataset / "raw"
    lineage = dataset / "lineage"
    raw.mkdir(parents=True)
    lineage.mkdir()

    _write_jsonl_gz(
        str(raw / "instruments.jsonl.gz"),
        [
            {
                "id": f"i{n}", "symbol": f"{n:06d}", "name": f"n{n}", "market": "SH",
                "pinyin_initials": "X", "status": "active", "listing_date": "2000-01-01",
                "total_share": "1", "float_share": "1", "share_as_of": analysis_axis[0],
            }
            for n in range(instruments_n)
        ],
    )
    _write_jsonl_gz(
        str(raw / "boards.jsonl.gz"),
        [
            {
                "id": "b1", "external_code": "C001", "name": "概念A", "type": "concept",
                "taxonomy": "custom", "source": "qstock", "taxonomy_version": "v1",
                "taxonomy_compatibility_key": "k1", "hierarchy_level": 1,
                "parent_board_id": None, "is_active": True, "membership_version": "v1",
                "updated_at": analysis_axis[0],
            }
        ],
    )
    _write_jsonl_gz(
        str(raw / "board_memberships_current_snapshot.jsonl.gz"),
        [{"board_id": "b1", "instrument_id": "i0", "updated_at": analysis_axis[0]}],
    )
    _write_jsonl_gz(str(raw / "trading_calendar.jsonl.gz"), calendar_rows)
    state_rows = []
    for d, n in state_by_date.items():
        for k in range(n):
            state_rows.append(
                {
                    "instrument_id": f"i{k}", "trade_date": d, "algorithm_version": "v2",
                    "input_hash": "h", "source_history_run_id": "r1",
                    "history_contract_version": "review-history-v2", "state_payload": {},
                }
            )
    _write_jsonl_gz(str(raw / "first_pyramid_daily_state.jsonl.gz"), state_rows)
    bar_rows = []
    for d, n in bar_by_date.items():
        for k in range(n):
            bar_rows.append(
                {
                    "instrument_id": f"i{k}", "trade_date": d, "open": "1.0", "high": "1.0",
                    "low": "1.0", "close": "1.0", "volume": "1", "amount": "1",
                    "adj_factor": "1.00000000",
                }
            )
    _write_jsonl_gz(str(raw / "bars_daily.jsonl.gz"), bar_rows)
    _write_jsonl_gz(
        str(raw / "first_pyramid_events.jsonl.gz"),
        [{"event_type": "pyramid", "event_time": f"{analysis_axis[0]}T09:30:00+08:00"}],
    )
    _write_jsonl_gz(
        str(raw / "stock_feature_snapshots_asof.jsonl.gz"),
        [{"instrument_id": "i0", "source_run_id": None}],
    )
    _write_jsonl_gz(str(lineage / "stock_feature_snapshot_runs.jsonl.gz"), [])
    _write_jsonl_gz(str(lineage / "first_pyramid_history_runs.jsonl.gz"), [{"id": "r1"}])

    m = _make_manifest()
    m["date_ranges"] = {"analysis_axis": analysis_axis}
    fph = m["source_readiness"]["first_pyramid_history"]
    fph["coverage"]["review_fact_universe"] = fact_universe
    return m


def test_data_quality_summary_p1a_denominator_uses_fact_universe(tmp_path):
    """P1-A：coverage 分母 = review_fact_universe（≠ D1 instruments 全量）。"""
    dataset = tmp_path / "ds"
    analysis = ["2025-01-06", "2025-01-07", "2025-01-08"]
    m = _write_quality_summary_input(
        dataset,
        instruments_n=10,          # D1 全量 = 10
        fact_universe=5,           # review_fact_universe = 5（SH/SZ/BJ 全 A 股）
        analysis_axis=analysis,
        calendar_rows=[
            {"trade_date": d, "is_trading_day": True, "market": "A",
             "source": "manual", "status": "closed", "verified_at": d}
            for d in ["2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"]
        ],
        state_by_date={"2025-01-06": 5, "2025-01-07": 5, "2025-01-08": 0},
        bar_by_date={"2025-01-06": 5, "2025-01-07": 0, "2025-01-08": 5},
    )
    s = _data_quality_summary(str(dataset / "raw"), str(dataset / "lineage"), m)
    assert s["instruments"] == 10
    assert s["review_fact_universe"] == 5
    cov = s["coverage_report"]
    # 旧 bug：分母用 instruments=10 → state p50=0.5；修复后 5/5=1.0
    assert cov["state_t_coverage_by_date"]["p50"] == 1.0
    assert cov["state_t_coverage_by_date"]["max"] == 1.0
    # bar：T1=5/5、T2=0、T3=5/5 → sorted [0,1,1]，p50/max=1.0
    assert cov["bar_exact_t_coverage"]["p50"] == 1.0
    # 分母修复后 prd_readiness = available（旧分母下为 partial）
    assert s["prd_readiness"] == "available"


def test_data_quality_summary_p1b_canonical_t1_from_calendar(tmp_path):
    """P1-B：state_t1_cov 用 calendar canonical predecessor 覆盖全部 analysis 日
    （含首分析日的 T-1=warmup 内真实 predecessor），并忽略非 A / 非交易日行。"""
    dataset = tmp_path / "ds"
    analysis = ["2025-01-06", "2025-01-07", "2025-01-08"]
    m = _write_quality_summary_input(
        dataset,
        instruments_n=5,
        fact_universe=5,
        analysis_axis=analysis,
        # 干扰行：market=SH（非 A）、is_trading_day=False → 均不得进入 trading axis
        calendar_rows=[
            {"trade_date": "2025-01-03", "is_trading_day": True, "market": "A",
             "source": "manual", "status": "closed", "verified_at": "2025-01-03"},
            {"trade_date": "2025-01-03", "is_trading_day": True, "market": "SH",
             "source": "manual", "status": "closed", "verified_at": "2025-01-03"},
            {"trade_date": "2025-01-04", "is_trading_day": False, "market": "A",
             "source": "manual", "status": "closed", "verified_at": "2025-01-04"},
            {"trade_date": "2025-01-06", "is_trading_day": True, "market": "A",
             "source": "manual", "status": "closed", "verified_at": "2025-01-06"},
            {"trade_date": "2025-01-07", "is_trading_day": True, "market": "A",
             "source": "manual", "status": "closed", "verified_at": "2025-01-07"},
            {"trade_date": "2025-01-08", "is_trading_day": True, "market": "A",
             "source": "manual", "status": "closed", "verified_at": "2025-01-08"},
        ],
        # 2025-01-06 的 canonical T-1 = 2025-01-03（在 warmup，不在 analysis_axis）
        state_by_date={"2025-01-03": 5, "2025-01-06": 0, "2025-01-07": 3, "2025-01-08": 0},
        bar_by_date={"2025-01-06": 5, "2025-01-07": 5, "2025-01-08": 5},
    )
    s = _data_quality_summary(str(dataset / "raw"), str(dataset / "lineage"), m)
    t1 = s["coverage_report"]["state_t1_coverage_by_date"]
    # state_t1_cov = [T06→T03=5/5=1.0, T07→T06=0, T08→T07=3/5=0.6]
    # 旧 bug：丢弃首分析日 → max=0.6；修复后 max=1.0（首分析日 T-1 被计入）
    assert t1["max"] == 1.0
    assert t1["p50"] == 0.6
    assert s["coverage_report"]["missing_t1_analysis_dates"] == []
    # 非 A / 非交易日干扰行被正确过滤：T-1 恒为 calendar 内真实 predecessor
    assert "2025-01-04" not in s["coverage_report"]["missing_t1_analysis_dates"]

