"""[BOARD-LOCAL-OWNERSHIP-CORRECTION-01 / RC1] transport → **真实** sync_boards 集成契约（PG）。

这是 RC1 的核心证据：**禁止 mock sync_boards**。
必须整链真实执行：

    本地 provider 语义 BoardSnapshot
      → build_envelope → serialize（stdin payload）
      → deserialize → validate_envelope → reconstruct_snapshot（远端信任边界）
      → resolve_board_instruments（新公共 helper，同一 session）
      → 真实 board_sync_service.sync_boards() → 真实 DB facts

并验证 MarketBoard 层级/分类学/身份/version 事实与 PIT 解析。

同日行为覆盖（不得引入 cooldown / once-per-day）：
- A → A（完全相同）：幂等，安全重放。
- A → B（同日 membership 变化）：必须在既有 PIT 合同下正确；
  若当前 schema 无法安全支持，测试将失败并暴露该限制（不得靠频率限制回避）。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board_taxonomy import BoardDefinitionVersion
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard
from app.services.board_membership_service import resolve_board_membership_at
from app.services.board_snapshot_transfer import (
    build_envelope,
    deserialize_envelope,
    reconstruct_snapshot,
    serialize_envelope,
    validate_envelope,
)
from app.services.board_sync_service import (
    MIN_CONCEPT_COUNT,
    MIN_INDUSTRY_COUNT,
    MIN_RAW_ROWS,
    resolve_board_instruments,
    sync_boards,
)
from app.services.wencai_board_provider import (
    BOARD_IDENTITY_CONTRACT_VERSION,
    BOARD_SOURCE,
    BOARD_TAXONOMY,
    BOARD_TAXONOMY_COMPATIBILITY_KEY,
    BOARD_TAXONOMY_VERSION,
    BoardSnapshot,
)

pytestmark = pytest.mark.postgres

EFFECTIVE_DATE = date(2026, 9, 24)

_SYMBOLS = ("600000", "600001", "600002", "000001", "000002", "000003")


def _board(
    code: str,
    name: str,
    *,
    board_type: str = "industry",
    level: str | None = None,
    parent: str | None = None,
) -> dict[str, str]:
    """provider 风格 board（字段与 wencai_board_provider 输出一致）。"""
    b: dict[str, str] = {
        "external_code": code,
        "name": name,
        "type": board_type,
        "taxonomy": BOARD_TAXONOMY,
        "source": BOARD_SOURCE,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
    }
    if level is not None:
        b["hierarchy_level"] = level
    if parent is not None:
        b["parent_external_code"] = parent
    return b


def _snapshot(*, concept_members: list[str] | None = None) -> BoardSnapshot:
    """L1/L2/L3 行业 + 1 概念；行业三级共享同一批股票（provider 真实行为）。"""
    l1, l2, l3 = "IND_L1", "IND_L2", "IND_L3"
    con = "CON_1"
    concept_members = concept_members if concept_members is not None else ["600000", "600001"]
    return BoardSnapshot(
        boards=[
            _board(l1, "金融", level="L1"),
            _board(l2, "金融-银行", level="L2", parent=l1),
            _board(l3, "金融-银行-国有银行", level="L3", parent=l2),
            _board(con, "人工智能", board_type="concept"),
        ],
        memberships={
            (l1, "industry"): list(_SYMBOLS),
            (l2, "industry"): list(_SYMBOLS),
            (l3, "industry"): list(_SYMBOLS),
            (con, "concept"): list(concept_members),
        },
        raw_rows=len(_SYMBOLS),
        unresolved_symbols=[],
        diagnostics={"duration_ms": 1},
    )


def _mock_stats(snapshot: BoardSnapshot) -> dict:
    """绕过绝对门禁（小样本 DB 测试）；相对门禁同样由 mock 短路。"""
    industry_count = sum(1 for b in snapshot.boards if b["type"] == "industry")
    concept_count = sum(1 for b in snapshot.boards if b["type"] == "concept")
    all_symbols: set[str] = set()
    for symbols in snapshot.memberships.values():
        all_symbols.update(symbols)
    return {
        "raw_rows": max(snapshot.raw_rows, MIN_RAW_ROWS),
        "industry_count": max(industry_count, MIN_INDUSTRY_COUNT),
        "concept_count": max(concept_count, MIN_CONCEPT_COUNT),
        "board_count": snapshot.board_count,
        "relation_count": snapshot.membership_count,
        "unique_stock_count": len(all_symbols),
        "total_symbol_refs": snapshot.membership_count,
        "code_uniqueness_rate": 1.0,
        "unresolved_count": 0,
    }


@pytest_asyncio.fixture
async def seeded_instruments(db_session: AsyncSession) -> dict[str, Instrument]:
    """预创建 A 股标的（真实 resolve_board_instruments 只解析，不创建）。"""
    created: dict[str, Instrument] = {}
    for symbol in _SYMBOLS:
        inst = Instrument(
            symbol=symbol,
            name=f"测试-{symbol}",
            market="SH" if symbol.startswith("6") else "SZ",
            status="active",
        )
        db_session.add(inst)
        created[symbol] = inst
    await db_session.flush()
    return created


async def _apply_via_transport(db_session: AsyncSession, snapshot: BoardSnapshot) -> dict:
    """走完整远程链路（serialize → validate → reconstruct）后调用**真实** sync_boards。"""
    envelope = build_envelope(snapshot, producer_git_sha="test-sha")
    parsed = deserialize_envelope(serialize_envelope(envelope))
    validate_envelope(parsed)
    restored = reconstruct_snapshot(parsed)

    async def _resolver(symbols):
        return await resolve_board_instruments(db_session, list(symbols))

    with patch(
        "app.services.board_sync_service.validate_snapshot",
        return_value=_mock_stats(restored),
    ):
        with patch(
            "app.services.board_sync_service.get_current_detailed_counts",
            return_value={
                "board_count": 0, "membership_count": 0, "industry_count": 0,
                "concept_count": 0, "stock_count": 0,
            },
        ):
            return await sync_boards(
                db_session,
                restored,
                instrument_resolver=_resolver,
                effective_date=EFFECTIVE_DATE,
            )


# =============================================================================
# 1. transport → 真实 sync_boards：全语义 + 层级身份落库
# =============================================================================


@pytest.mark.asyncio
async def test_pg_transport_roundtrip_then_real_sync_boards(
    db_session: AsyncSession, seeded_instruments: dict[str, Instrument]
) -> None:
    result = await _apply_via_transport(db_session, _snapshot())
    await db_session.commit()

    assert result["status"] == "succeeded"
    assert result["board_count"] == 4
    assert result["resolved"] == len(_SYMBOLS), (
        "transport 后所有 symbol 必须仍可解析（否则真实 owner 会因解析率失败）"
    )

    by_code = {
        b.externalCode: b
        for b in (
            await db_session.execute(select(MarketBoard))
        ).scalars()
    }
    assert set(by_code) == {"IND_L1", "IND_L2", "IND_L3", "CON_1"}

    # 分类学 / 身份合同（RC1：transport 若丢字段，这里会是 qstock 默认值或直接报错）
    for board in by_code.values():
        assert board.taxonomy == BOARD_TAXONOMY
        assert board.source == BOARD_SOURCE
        assert board.taxonomyVersion == BOARD_TAXONOMY_VERSION
        assert board.taxonomyCompatibilityKey == BOARD_TAXONOMY_COMPATIBILITY_KEY
        assert board.membershipVersion, "membershipVersion 必须写入"
        assert board.isActive is True

    # 层级身份：L1 无父；L2 父=L1；L3 父=L2
    assert by_code["IND_L1"].hierarchyLevel == "L1"
    assert by_code["IND_L1"].parentBoardId is None
    assert by_code["IND_L2"].hierarchyLevel == "L2"
    assert by_code["IND_L2"].parentBoardId == by_code["IND_L1"].id
    assert by_code["IND_L3"].hierarchyLevel == "L3"
    assert by_code["IND_L3"].parentBoardId == by_code["IND_L2"].id
    assert by_code["CON_1"].type == "concept"

    # BoardDefinitionVersion 记录身份合同版本 + PIT effective_from
    defs = (
        await db_session.execute(
            select(BoardDefinitionVersion).where(
                BoardDefinitionVersion.board_id == by_code["IND_L3"].id
            )
        )
    ).scalars().all()
    assert len(defs) == 1
    assert defs[0].identity_contract_version == BOARD_IDENTITY_CONTRACT_VERSION
    assert defs[0].hierarchy_level == "L3"
    assert defs[0].parent_board_id == by_code["IND_L2"].id
    assert defs[0].effective_from == EFFECTIVE_DATE

    # PIT 解析可用（exact-T）
    pit = await resolve_board_membership_at(
        db_session, by_code["IND_L1"].id, EFFECTIVE_DATE
    )
    assert len(pit.instrument_ids) == len(_SYMBOLS)


# =============================================================================
# 2. 同日重复同步（A → A）：幂等
# =============================================================================


@pytest.mark.asyncio
async def test_pg_same_day_identical_snapshot_is_idempotent(
    db_session: AsyncSession, seeded_instruments: dict[str, Instrument]
) -> None:
    await _apply_via_transport(db_session, _snapshot())
    await db_session.commit()
    board = (
        await db_session.execute(
            select(MarketBoard).where(MarketBoard.externalCode == "IND_L1")
        )
    ).scalar_one()
    version_before = board.membershipVersion
    def_count_before = len(
        (
            await db_session.execute(
                select(BoardDefinitionVersion).where(
                    BoardDefinitionVersion.board_id == board.id
                )
            )
        ).scalars().all()
    )

    # 同日再次同步**完全相同**的快照（无 cooldown、无 once-per-day）
    result = await _apply_via_transport(db_session, _snapshot())
    await db_session.commit()

    assert result["status"] == "succeeded"
    await db_session.refresh(board)
    assert board.membershipVersion == version_before, "相同快照必须保持同一 membershipVersion"
    def_count_after = len(
        (
            await db_session.execute(
                select(BoardDefinitionVersion).where(
                    BoardDefinitionVersion.board_id == board.id
                )
            )
        ).scalars().all()
    )
    assert def_count_after == def_count_before, "相同快照不得追加新的 definition 版本"


# =============================================================================
# 3. 同日变更同步（A → B）：必须在既有 PIT 合同下正确
# =============================================================================


@pytest.mark.asyncio
async def test_pg_same_day_changed_membership_updates_pit_resolution(
    db_session: AsyncSession, seeded_instruments: dict[str, Instrument]
) -> None:
    """同日 membership 变化后再次同步：新事实必须成为当日 PIT 解析结果。

    若当前 schema/PIT 无法安全支持同日变更，本测试会失败并暴露该限制 ——
    这正是 reviewer 要求的「expose a real existing limitation」，不得用频率限制回避。
    """
    await _apply_via_transport(db_session, _snapshot(concept_members=["600000"]))
    await db_session.commit()
    board = (
        await db_session.execute(
            select(MarketBoard).where(MarketBoard.externalCode == "CON_1")
        )
    ).scalar_one()
    version_a = board.membershipVersion

    # 同日第二次：concept membership 改变（新增 600001）
    result = await _apply_via_transport(
        db_session, _snapshot(concept_members=["600000", "600001"])
    )
    await db_session.commit()
    assert result["status"] == "succeeded", "同日变更必须被接受（无 cooldown / once-per-day）"

    await db_session.refresh(board)
    assert board.membershipVersion != version_a, "membership 改变必须产生新的 membershipVersion"

    # exact-T PIT 解析必须给出**新**成员集（不得是混合集/旧集）
    pit = await resolve_board_membership_at(db_session, board.id, EFFECTIVE_DATE)
    resolved = {
        seeded_instruments[s].id for s in ("600000", "600001")
    }
    assert set(pit.instrument_ids) == resolved, (
        f"同日变更后 exact-T PIT 解析应为新成员集；实际={pit.instrument_ids} 期望={resolved}"
    )
    assert pit.membership_version == board.membershipVersion
