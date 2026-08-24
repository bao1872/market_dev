"""[Slice 4A7] Board 读 API 数据源切到 Unified Review canonical facts。

Pure-unit tests (PURE_UNIT_TEST=1, no DB/network) 覆盖以下场景：

1. list GET 端点从 canonical Review fact（ReviewScopeObservationFact）构建行。
2. detail GET 端点从 canonical Review fact 构建快照。
3. 不再需要 BoardAnalysisSnapshot（读接口不查询该模型）。
4. canonical 缺失时 detail 返回 404。
5. list 分页与 board_type 过滤仍然工作。
6. 源码检查证明读 handler 不再调用 list_board_analyses / get_board_analysis_detail。
"""

from __future__ import annotations

import inspect
import os
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

# Pure-unit guard: these tests must never touch a real DB/network.
os.environ.setdefault("PURE_UNIT_TEST", "1")

from app.api import board_analysis as board_api  # noqa: E402

TRADE_DATE = date(2026, 8, 20)

# --------------------------------------------------------------------------- #
# Sample canonical observation payload（对应 ReviewScopeObservationFact
# observation_payload 的 trend/structure/momentum/participation 形状）。
# --------------------------------------------------------------------------- #
OBS: dict = {
    "trend": {
        "state": {"up_count": 30, "down_count": 20, "neutral_count": 10},
        "trend_strength_distribution": {
            "mean": 0.7, "p25": 0.5, "p50": 0.7, "p75": 0.9,
        },
        "dsa_vwap_dev_pct_distribution": {
            "mean": 0.03, "p25": 0.01, "p50": 0.03, "p75": 0.05,
        },
    },
    "structure": {
        "swing": {"state": {"up_count": 25, "down_count": 15, "neutral_count": 20}},
        "alignment": {"aligned_count": 40, "divergent_count": 20},
        "current_state": {
            "mean_active_orderblock_count": 2.5,
            "latest_events": {
                "bos": {"up": 12, "down": 8},
                "choch": {"up": 5, "down": 3},
                "ob": {"up": 10, "down": 6},
                "eqh": 4,
                "eql": 2,
            },
        },
    },
    "momentum": {
        "state": {"expanding_count": 15, "contracting_count": 10, "flat_count": 5},
        "squeeze_state": {
            "squeeze_count": 8, "squeeze_release_count": 6, "non_squeeze_count": 16,
        },
        "change": {"enhancing_count": 12, "weakening_count": 9, "flat_count": 9},
        "sqzmom": {"mean": 0.4},
    },
    "participation": {
        "volume": {
            "badge": {
                "high_count": 20, "low_count": 10,
                "normal_count": 30, "unknown_count": 0,
            },
            "ratio20_mean": 1.2,
            "ratio200_mean": 1.0,
        },
    },
}


def _run():
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_core_run_id=uuid.uuid4(),
        algorithm_version="review-1.0.0",
        trade_date=TRADE_DATE,
    )


def _fact(scope_type, scope_key, scope_name, pit, provided, readiness, obs=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_name,
        pit_member_count=pit,
        provided_member_count=provided,
        readiness=readiness,
        observation_payload=obs or OBS,
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


def _fact_session(facts):
    """Fake AsyncSession：execute(...).scalars() 返回给定 facts。"""
    sess = AsyncMock()
    res = MagicMock()
    res.scalars.return_value = _FakeScalars(facts)
    sess.execute = AsyncMock(return_value=res)
    return sess


def _resolve(review, td=None):
    return review, td or TRADE_DATE


# --------------------------------------------------------------------------- #
# 1. list GET 读取 canonical Review fact
# --------------------------------------------------------------------------- #
async def test_list_reads_canonical_fact(monkeypatch):
    run = _run()
    fact = _fact("concept", "C1", "C1板块", pit=100, provided=95, readiness="ready")
    monkeypatch.setattr(
        board_api, "_resolve_published_review",
        AsyncMock(return_value=_resolve(run)),
    )
    monkeypatch.setattr(board_api, "compute_is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(
        board_api, "_list_board_facts", AsyncMock(return_value=[fact]),
    )

    resp = await board_api.list_board_analysis(
        type="concept",
        trade_date=TRADE_DATE,
        sort="coverage_desc",
        page=1,
        page_size=20,
        db=_fact_session([]),
        ctx=MagicMock(),
    )

    assert resp.total == 1
    item = resp.items[0]
    # scope_key -> board_id；scope_type concept -> board_type concept
    assert item.board_id == "C1"
    assert item.board_type == "concept"
    assert item.board_name == "C1板块"
    assert item.source_core_run_id == str(run.source_core_run_id)
    # eligible/ready/coverage
    assert item.eligible_count == 100
    assert item.ready_count == 95
    assert item.missing_count == 5
    assert item.coverage_ratio == 0.95
    # status=readiness 如实；is_published=True（来源即已发布 Review run）
    assert item.status == "ready"
    assert item.is_published is True
    # legacy 字段不做伪 lineage
    assert item.board_analysis_run_id is None
    assert item.parameter_hash is None
    assert item.taxonomy_version is None


# --------------------------------------------------------------------------- #
# 2. detail GET 读取 canonical Review fact 并映射 payload
# --------------------------------------------------------------------------- #
async def test_detail_reads_canonical_fact(monkeypatch):
    run = _run()
    fact = _fact("industry_l1", "IND1", "行业一", pit=200, provided=180,
                 readiness="ready")
    monkeypatch.setattr(
        board_api, "_resolve_published_review",
        AsyncMock(return_value=_resolve(run)),
    )
    monkeypatch.setattr(board_api, "compute_is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(board_api, "_get_board_fact", AsyncMock(return_value=fact))

    resp = await board_api.get_board_analysis(
        board_id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        db=_fact_session([]),
        ctx=MagicMock(),
    )

    snap = resp.snapshot
    assert snap.board_id == "IND1"
    assert snap.board_type == "industry"  # industry_l1/l2/l3 -> industry
    assert snap.ready_count == 180
    assert snap.coverage_ratio == 0.9

    payload = snap.payload
    # canonical 字段映射（不新增公式）
    assert payload["trend_strength"]["avg"] == 0.7
    assert payload["vwap_dev_pct"]["avg"] == 0.03
    assert payload["structure"]["avg_active_ob_count"] == 2.5
    assert payload["momentum"]["avg_sqzmom"] == 0.4
    assert payload["momentum"]["enhancing"] == 12
    assert payload["momentum"]["fading"] == 9
    assert payload["momentum"]["flat"] == 9
    assert payload["volume"]["high"] == 20
    assert payload["volume"]["low"] == 10
    assert payload["volume"]["normal"] == 30
    assert payload["volume"]["unknown"] == 0
    assert payload["volume"]["avg_volume_ratio20"] == 1.2
    assert payload["volume"]["avg_volume_ratio200"] == 1.0
    assert payload["structure_events"]["bos_up"] == 12
    assert payload["structure_events"]["choch_down"] == 3


# --------------------------------------------------------------------------- #
# 3. no BoardAnalysisSnapshot required（读接口不接该模型）
# --------------------------------------------------------------------------- #
def test_get_handlers_do_not_query_board_snapshot_model():
    src = inspect.getsource(board_api)
    # 读接口不 import / 不 refer BoardAnalysisSnapshot 持久化模型
    assert "from app.models" not in src or "BoardAnalysisSnapshot" not in src.split(
        "from app.models"
    )[-1]
    # _board_fact_statement 查询的是 canonical ReviewScopeObservationFact
    stmt_src = inspect.getsource(board_api._board_fact_statement)
    assert "ReviewScopeObservationFact" in stmt_src


def test_payload_built_without_board_snapshot_fields():
    payload = board_api._build_board_payload(OBS)
    for key in ("trend_dist", "trend_strength", "vwap_dev_pct",
                "structure", "structure_events", "momentum", "volume"):
        assert key in payload
    # 覆盖为空时也不抛错（缺分布 -> 0/None）
    empty = board_api._build_board_payload({})
    assert empty["trend_strength"]["avg"] is None
    assert empty["momentum"]["enhancing"] == 0


# --------------------------------------------------------------------------- #
# 4. canonical missing -> 404 detail
# --------------------------------------------------------------------------- #
async def test_detail_404_when_canonical_missing(monkeypatch):
    run = _run()
    monkeypatch.setattr(
        board_api, "_resolve_published_review",
        AsyncMock(return_value=_resolve(run)),
    )
    monkeypatch.setattr(board_api, "_get_board_fact", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await board_api.get_board_analysis(
            board_id=uuid.uuid4(),
            trade_date=TRADE_DATE,
            db=_fact_session([]),
            ctx=MagicMock(),
        )
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
# 5. list 分页 / board_type 过滤仍然工作
# --------------------------------------------------------------------------- #
async def test_list_pagination_slices(monkeypatch):
    run = _run()
    facts = [_fact("concept", f"C{i}", f"板块{i}", pit=10 * i + 10,
                   provided=10 * i + 5, readiness="ready") for i in range(25)]
    monkeypatch.setattr(
        board_api, "_resolve_published_review",
        AsyncMock(return_value=_resolve(run)),
    )
    monkeypatch.setattr(board_api, "compute_is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(
        board_api, "_list_board_facts", AsyncMock(return_value=facts),
    )

    resp = await board_api.list_board_analysis(
        type="concept", sort="name_asc", page=2, page_size=10,
        db=_fact_session([]), ctx=MagicMock(),
    )
    assert resp.total == 25
    assert len(resp.items) == 10
    assert resp.page == 2
    assert resp.has_more is True


def test_board_type_filter_maps_to_scope_types():
    assert board_api._scope_types_for_board_type("concept") == ("concept",)
    assert board_api._scope_types_for_board_type("industry") == (
        "industry_l1", "industry_l2", "industry_l3",
    )
    assert set(board_api._scope_types_for_board_type(None)).issuperset(
        ("concept", "industry_l1", "industry_l2", "industry_l3"),
    )
    # scope_type -> board_type 映射
    assert board_api._board_type_from_scope_type("concept") == "concept"
    assert board_api._board_type_from_scope_type("industry_l2") == "industry"


# --------------------------------------------------------------------------- #
# 6. source inspection：读 handler 不再调用 legacy list/detail 服务
# --------------------------------------------------------------------------- #
def test_handlers_no_longer_call_legacy_board_service():
    src = inspect.getsource(board_api)
    assert "list_board_analyses" not in src
    assert "get_board_analysis_detail" not in src


async def test_list_empty_when_no_published_review(monkeypatch):
    monkeypatch.setattr(
        board_api, "_resolve_published_review", AsyncMock(return_value=None),
    )
    resp = await board_api.list_board_analysis(
        type=None, trade_date=None, sort="coverage_desc", page=1, page_size=20,
        db=_fact_session([]), ctx=MagicMock(),
    )
    assert resp.total == 0
    assert resp.items == []