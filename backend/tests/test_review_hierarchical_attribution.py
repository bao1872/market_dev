from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.review import attribution_engine
from app.domain.review.attribution_engine import (
    aggregate_child_scope_attributions,
    aggregate_instrument_attributions,
    classify_instrument_board_role,
    classify_instrument_relation_to_scope,
)
from app.services import review_attribution_service as service
from app.services.board_membership_service import PITMembership


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.added: list[Any] = []
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _Rows:
        self.executed.append(stmt)
        return _Rows(self.rows)

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _metrics(value: float = 50.0) -> dict[str, dict[str, Any]]:
    return {
        code: {"value": value, "rawValue": value / 100, "components": []}
        for code in ("P", "Q", "U", "C", "V")
    }


@pytest.mark.asyncio
async def test_child_scope_listing_uses_all_rows_and_parent_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    boards = [
        SimpleNamespace(id=uuid.uuid4(), name="概念甲"),
        SimpleNamespace(id=uuid.uuid4(), name="概念乙"),
    ]
    snapshots = [
        SimpleNamespace(id=uuid.uuid4()),
        SimpleNamespace(id=uuid.uuid4()),
    ]
    session = _FakeSession(list(zip(boards, snapshots, strict=True)))

    memberships = {
        boards[0].id: PITMembership(
            instrument_ids=(parent_ids[0], uuid.uuid4()),
            taxonomy_version="tax-2",
            compatibility_key="qstock-concept-v1",
            membership_version="m-7",
        ),
        boards[1].id: PITMembership(
            instrument_ids=(uuid.uuid4(),),
            taxonomy_version="tax-2",
            compatibility_key="qstock-concept-v1",
            membership_version="m-8",
        ),
    }

    async def _membership(_session: Any, board_id: uuid.UUID, _date: date) -> PITMembership:
        return memberships[board_id]

    monkeypatch.setattr(service, "resolve_board_membership_at", _membership)
    result = await service._list_child_scope_keys(
        session,
        "style",
        "large_cap_style",
        "concept",
        trade_date=date(2026, 7, 31),
        source_board_run_id=uuid.uuid4(),
        parent_instrument_ids=parent_ids,
    )

    assert len(session.executed) == 1
    assert len(result) == 1
    assert result[0].scope_key == str(boards[0].id)
    assert result[0].member_ids == (parent_ids[0],)
    assert result[0].source_board_snapshot_id == snapshots[0].id
    assert result[0].taxonomy_compatibility_key == "qstock-concept-v1"


def test_child_attribution_preserves_sign_and_structured_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = uuid.uuid4()
    child_scopes = [
        {
            "scope_type": "industry_l2",
            "scope_key": "child-positive",
            "scope_name": "正贡献",
            "relation_type": "descendant_industry",
            "flat_list": [
                {
                    "fp_trend_direction": "up",
                    "fp_swing_direction": "up",
                    "fp_internal_direction": "up",
                    "fp_momentum_direction": "expanding",
                    "fp_momentum_change": "enhancing",
                    "fp_structure_alignment": "aligned",
                    "fp_segment_change_pct": 8,
                    "fp_volume_ratio20": 2,
                }
            ],
            "eligible_count": 1,
            "ready_count": 1,
            "coverage_ratio": 1.0,
            "source_board_snapshot_id": snapshot_id,
            "taxonomy_version": "tax-2",
            "taxonomy_compatibility_key": "qstock-industry-v1",
            "membership_version": "m-2",
            "parent_scope_type": "industry_l1",
            "parent_scope_key": "parent",
            "data_quality": {"status": "ready"},
        },
        {
            "scope_type": "industry_l2",
            "scope_key": "child-negative",
            "scope_name": "负贡献",
            "relation_type": "descendant_industry",
            "flat_list": [
                {
                    "fp_trend_direction": "down",
                    "fp_swing_direction": "down",
                    "fp_internal_direction": "down",
                    "fp_momentum_direction": "contracting",
                    "fp_momentum_change": "weakening",
                    "fp_structure_alignment": "divergent",
                    "fp_segment_change_pct": -8,
                    "fp_volume_ratio20": 0.5,
                }
            ],
            "eligible_count": 1,
            "ready_count": 1,
            "coverage_ratio": 1.0,
            "source_board_snapshot_id": uuid.uuid4(),
            "taxonomy_version": "tax-2",
            "taxonomy_compatibility_key": "qstock-industry-v1",
            "membership_version": "m-3",
            "parent_scope_type": "industry_l1",
            "parent_scope_key": "parent",
            "data_quality": {"status": "ready"},
        },
    ]

    def _child_metrics(flat_list: list[dict[str, Any]], *, ready_count: int) -> dict[str, dict[str, Any]]:
        value = 80.0 if flat_list[0]["fp_trend_direction"] == "up" else 20.0
        return _metrics(value)

    monkeypatch.setattr(attribution_engine, "compute_all_metrics", _child_metrics)
    result = aggregate_child_scope_attributions(_metrics(), 10, child_scopes)

    assert len(result) == 2
    assert {item["child_scope_key"] for item in result} == {
        "child-positive",
        "child-negative",
    }
    assert any(item["contribution_value"] > 0 for item in result)
    assert any(item["contribution_value"] < 0 for item in result)
    assert [abs(item["contribution_value"]) for item in result] == sorted(
        (abs(item["contribution_value"]) for item in result), reverse=True
    )
    evidence = result[0]["evidence_payload"]
    assert set(evidence["contributions"]) == {"P", "Q", "U", "C", "V"}
    assert evidence["parent_scope_type"] == "industry_l1"
    assert evidence["source_board_snapshot_id"]
    assert evidence["taxonomy_compatibility_key"] == "qstock-industry-v1"
    assert evidence["board_sync"]["state"] in {"synchronized", "divergent"}


def test_role_and_relation_do_not_consume_segment_change_as_daily_return() -> None:
    base = {
        "fp_trend_direction": "up",
        "fp_momentum_change": "enhancing",
        "fp_volume_ratio20": 2.0,
    }
    positive_segment = {**base, "fp_segment_change_pct": 99.0}
    negative_segment = {**base, "fp_segment_change_pct": -99.0}

    assert classify_instrument_board_role(positive_segment, 5, 10) == "elasticity"
    assert classify_instrument_board_role(negative_segment, 5, 10) == "elasticity"
    assert classify_instrument_relation_to_scope(positive_segment, 70, 70) == (
        "synchronized_strengthening"
    )
    assert classify_instrument_relation_to_scope(negative_segment, 70, 70) == (
        "synchronized_strengthening"
    )


def test_instrument_attribution_preserves_master_identity_and_role_evidence() -> None:
    instrument_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    result = aggregate_instrument_attributions(
        _metrics(),
        10,
        [
            {
                "instrument_id": instrument_id,
                "symbol": "600000",
                "name": "浦发银行",
                "source_snapshot_id": snapshot_id,
                "flat": {
                    "fp_trend_direction": "up",
                    "fp_swing_direction": "up",
                    "fp_internal_direction": "up",
                    "fp_momentum_direction": "expanding",
                    "fp_momentum_change": "enhancing",
                    "fp_structure_alignment": "aligned",
                    "fp_segment_change_pct": 3,
                    "fp_volume_ratio20": 1.8,
                },
            }
        ],
    )

    assert result[0]["instrument_id"] == instrument_id
    assert result[0]["symbol"] == "600000"
    assert result[0]["name"] == "浦发银行"
    assert result[0]["source_snapshot_id"] == snapshot_id
    assert set(result[0]["contribution_payload"]["components"]) == {
        "P", "Q", "U", "C", "V"
    }
    assert result[0]["role_evidence"]["rank"] == 1
    assert result[0]["role_evidence"]["trend"] == "up"


def test_review_attribution_dtos_expose_hierarchy_and_instrument_evidence() -> None:
    # [REVIEW-BACKEND-FINAL-CLOSURE] legacy signal/discovery/tracking API DTO helpers
    # (_attribution_to_dto / _instrument_to_dto) 已随 Phase 5 退休删除；其底层
    # attribution_engine 领域逻辑由本文件其余用例覆盖，本用例不再验证已删除的 DTO。
    pytest.skip("legacy attribution DTO helpers retired in Phase 5")
