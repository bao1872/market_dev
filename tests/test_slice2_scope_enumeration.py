"""Slice 2A — Review scope enumeration decoupled from board snapshots.

纯单元测试，不连接数据库 (PURE_UNIT_TEST)。

验证：
- A: ``discover_pit_available_boards`` 查询包含 ``MarketBoard.isActive`` 过滤
     （行为保护：仅枚举生效板块）。
- B: ``_resolve_all_discovery_scopes`` 不再调用
     ``_list_board_scopes_by_hierarchy``，改调 ``discover_pit_available_boards``
     （industry_l1/l2/l3 + concept 共 4 次），实现与 BoardAnalysisSnapshot 解耦。
- C: ``resolve_scope_members`` 对 industry/concept 在
     ``population_status == ready`` 但成员为空时，抛出
     ``OptionalScopeUnavailableError(reason="empty_pit_membership")``，
     由 orchestrator 终态化为 SKIPPED（合法不可用，非执行失败）。
- D: ``discover_pit_available_boards`` 返回的 ``ScopeDefinition`` 不回填
     board snapshot lineage 字段（source_board_snapshot_id / taxonomy_version /
     taxonomy_compatibility_key / membership_version 均为 None）。
- E: ``_resolve_all_discovery_scopes`` 输出的 industry/concept scope 不含
     source_board_snapshot_id 依赖（解耦证据）。
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
_SCOPE_SVC = (
    _REPO / "backend" / "app" / "services" / "review_scope_service.py"
)
_ORCH_SVC = (
    _REPO
    / "backend"
    / "app"
    / "services"
    / "review_orchestrator_service.py"
)


def _load_source(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A. discover_pit_available_boards 包含 isActive 过滤
# ---------------------------------------------------------------------------

def test_discover_query_has_isactive_filter():
    src = _load_source(_SCOPE_SVC)
    # 定位到 discover_pit_available_boards 函数体
    start = src.index("async def discover_pit_available_boards")
    # 下一个顶层 async def 之前
    end = src.index("async def fetch_member_flat_list", start)
    body = src[start:end]
    assert "MarketBoard.isActive.is_(True)" in body, (
        "discover_pit_available_boards 必须过滤 MarketBoard.isActive==True"
    )


# ---------------------------------------------------------------------------
# D. discover 返回的 ScopeDefinition lineage 字段为 None（解耦）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_scope_definition_lineage_is_none():
    from app.services.review_scope_service import (
        ScopeDefinition,
        discover_pit_available_boards,
    )
    from sqlalchemy import Result

    fake_row = ("00000000-0000-0000-0000-000000000001", "测试行业")
    result = MagicMock(spec=Result)
    result.__iter__.return_value = iter([fake_row])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    out = await discover_pit_available_boards(
        session, "industry", "L1", date(2026, 8, 20),
    )
    assert len(out) == 1
    sd: ScopeDefinition = out[0]
    assert sd.scope_type == "industry_l1"
    assert sd.scope_key == "00000000-0000-0000-0000-000000000001"
    assert sd.scope_name == "测试行业"
    # 解耦：不回填 board snapshot lineage
    assert sd.source_board_snapshot_id is None
    assert sd.taxonomy_version is None
    assert sd.taxonomy_compatibility_key is None
    assert sd.membership_version is None


# ---------------------------------------------------------------------------
# C. resolve_scope_members 空成员 → OptionalScopeUnavailableError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_scope_members_empty_pit_membership_skipped():
    from app.services.review_scope_service import (
        OptionalScopeUnavailableError,
        resolve_scope_members,
    )

    board_uuid = "00000000-0000-0000-0000-000000000099"
    fake_board = MagicMock()
    fake_board.type = "industry"
    fake_board.hierarchyLevel = "L1"
    fake_board.name = "空行业"

    fake_membership = MagicMock()
    fake_membership.population_status = "ready"
    fake_membership.instrument_ids = []

    with patch(
        "app.services.review_scope_service.resolve_board_membership_at",
        new=AsyncMock(return_value=fake_membership),
    ), patch(
        "app.services.review_scope_service.select",
    ) as sel, patch(
        "app.services.review_scope_service.uuid",
    ) as fake_uuid:
        fake_uuid.UUID.return_value = board_uuid
        sel.return_value = MagicMock()
        session = AsyncMock()
        # session.execute 第二次返回 board（第一次 uuid 已 mock 不进 execute）
        board_result = MagicMock()
        board_result.scalar_one_or_none.return_value = fake_board
        session.execute = AsyncMock(return_value=board_result)

        with pytest.raises(OptionalScopeUnavailableError) as excinfo:
            await resolve_scope_members(
                session, "industry_l1", board_uuid, trade_date=date(2026, 8, 20),
            )
        assert excinfo.value.reason == "empty_pit_membership"
        assert excinfo.value.scope_type == "industry_l1"


# ---------------------------------------------------------------------------
# B + E. _resolve_all_discovery_scopes 解耦 board snapshots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_all_discovery_scopes_decoupled_from_snapshots():
    import app.services.review_orchestrator_service as orch

    # 构造最小 MarketReviewRun stub
    run = MagicMock()
    run.trade_date = date(2026, 8, 20)
    run.source_board_run_id = (
        "00000000-0000-0000-0000-0000000000aa"
    )

    fake_scope = MagicMock()
    fake_scope.scope_type = "industry_l1"
    fake_scope.scope_key = "00000000-0000-0000-0000-0000000000bb"
    fake_scope.scope_name = "X"
    fake_scope.source_board_snapshot_id = None
    fake_scope.taxonomy_version = None
    fake_scope.taxonomy_compatibility_key = None
    fake_scope.membership_version = None

    session = AsyncMock()

    def _disc(session, board_type, hierarchy_level, trade_date):
        st = board_type + (("_" + hierarchy_level.lower()) if hierarchy_level else "")
        s = MagicMock()
        s.scope_type = st
        s.scope_key = f"k-{st}"
        s.scope_name = f"n-{st}"
        s.source_board_snapshot_id = None
        s.taxonomy_version = None
        s.taxonomy_compatibility_key = None
        s.membership_version = None
        return [s]

    with patch.object(
        orch, "discover_pit_available_boards",
        new=AsyncMock(side_effect=_disc),
    ) as disc_mock, patch.object(
        orch, "_list_major_index_scopes",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        orch, "_list_style_scopes",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        orch, "_list_board_scopes_by_hierarchy",
        new=AsyncMock(return_value=[]),
    ) as legacy_mock:
        scopes = await orch._resolve_all_discovery_scopes(session, run)

    # 4 次 PIT discovery（industry_l1/l2/l3 + concept）
    assert disc_mock.await_count == 4, (
        f"discover_pit_available_boards 应被调用 4 次, 实际 {disc_mock.await_count}"
    )
    # 不再调用旧 board snapshot 读取
    assert legacy_mock.await_count == 0, (
        "_resolve_all_discovery_scopes 不应再调用 _list_board_scopes_by_hierarchy"
    )
    # 输出包含 market 固定 scope + 4 个 PIT discovery scope
    scope_types = [s.scope_type for s in scopes]
    assert scope_types.count("market") == 1
    assert scope_types.count("industry_l1") == 1
    assert scope_types.count("industry_l2") == 1
    assert scope_types.count("industry_l3") == 1
    assert scope_types.count("concept") == 1


@pytest.mark.asyncio
async def test_resolve_all_discovery_scopes_no_snapshot_lineage():
    """E: 输出的 industry/concept scope 不依赖 source_board_snapshot_id。"""
    import app.services.review_orchestrator_service as orch

    run = MagicMock()
    run.trade_date = date(2026, 8, 20)
    run.source_board_run_id = "00000000-0000-0000-0000-0000000000aa"

    def _make(scope_type: str) -> MagicMock:
        s = MagicMock()
        s.scope_type = scope_type
        s.scope_key = f"k-{scope_type}"
        s.scope_name = f"n-{scope_type}"
        s.source_board_snapshot_id = None
        s.taxonomy_version = None
        s.taxonomy_compatibility_key = None
        s.membership_version = None
        return s

    session = AsyncMock()
    with patch.object(
        orch, "discover_pit_available_boards",
        new=AsyncMock(
            side_effect=lambda *a, **k: [_make(a[1] + (("_" + a[2].lower()) if a[2] else ""))]
        ),
    ), patch.object(orch, "_list_major_index_scopes", new=AsyncMock(return_value=[])), \
         patch.object(orch, "_list_style_scopes", new=AsyncMock(return_value=[])), \
         patch.object(orch, "_list_board_scopes_by_hierarchy", new=AsyncMock(return_value=[])):
        scopes = await orch._resolve_all_discovery_scopes(session, run)

    for s in scopes:
        if s.scope_type in ("industry_l1", "industry_l2", "industry_l3", "concept"):
            assert s.source_board_snapshot_id is None
