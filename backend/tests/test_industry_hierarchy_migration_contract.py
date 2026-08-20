"""Industry hierarchy migration contract — real PostgreSQL integration test.

Runs only on the remote verification DB (PANJI_REMOTE_VERIFY_DB_TEST=1,
APP_ENV=verification, DATABASE_URL=bz_stock_verify_<sha>). All writes are
rolled back by the conftest savepoint fixture, so it never pollutes the DB.

Purpose: prevent future changes from breaking the legacy L1 full-path board
→ new L3 migration contract that depends on PIT (BoardDefinitionVersion)
evolution.

Confirmed facts:
  1. 同花顺 industry raw field is a 3-level path L1-L2-L3.
  2. Historical market_boards stored the full path as a single L1 board.
  3. Current parser generates L1 / L2 / L3 boards.
  4. legacy external_code == new L3 external_code (SHA256 of full path).

The migration reuses the same external_code, so a live sync re-evolves the
same MarketBoard's BoardDefinitionVersion (closing the old L1 PIT and opening
a new L3 PIT) instead of inserting a duplicate board.

This test exercises the REAL `_append_pit_history` persistence path against
real ORM + PostgreSQL.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board_taxonomy import BoardDefinitionVersion, BoardMembershipHistory
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard
from app.services import board_sync_service as svc
from app.services.board_sync_service import BoardSnapshot
from app.services.wencai_board_provider import (
    BOARD_IDENTITY_CONTRACT_VERSION,
    BOARD_SOURCE,
    BOARD_TAXONOMY,
    BOARD_TAXONOMY_COMPATIBILITY_KEY,
    BOARD_TAXONOMY_VERSION,
)

pytestmark = pytest.mark.postgres


# --------------------------------------------------------------------------- #
# Helpers mirroring the real parser's external_code rule.
# --------------------------------------------------------------------------- #
def _code(board_type: str, name: str) -> str:
    prefix = "wc:c:" if board_type == "concept" else "wc:i:"
    return prefix + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


L1_NAME = "金融"
L2_NAME = "金融-银行"
L3_NAME = "金融-银行-国有银行"  # the legacy full path
LEGACY_CODE = _code("industry", L3_NAME)  # == new L3 external_code
L2_CODE = _code("industry", L2_NAME)
EFFECTIVE_DATE = date(2026, 8, 14)


def _make_board(
    external_code: str,
    name: str,
    hierarchy_level: str,
    parent_external_code: str | None,
) -> dict[str, str]:
    board: dict[str, str] = {
        "external_code": external_code,
        "name": name,
        "type": "industry",
        "hierarchy_level": hierarchy_level,
        "taxonomy": BOARD_TAXONOMY,
        "source": BOARD_SOURCE,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
    }
    if parent_external_code is not None:
        board["parent_external_code"] = parent_external_code
    return board


# --------------------------------------------------------------------------- #
# Test
# --------------------------------------------------------------------------- #
async def test_legacy_l1_full_path_migrates_to_l3(db_session: AsyncSession) -> None:
    """legacy L1 full-path board safely migrates to L3 via PIT evolution."""
    # ---- Phase 1: seed legacy fixture (rolled back by fixture on exit) ------
    instrument_id = uuid.uuid4()
    db_session.add(Instrument(
        id=instrument_id,
        symbol="MIGRTEST000001",
        name="迁移测试标的",
        market="SH",
        status="active",
    ))
    await db_session.flush()

    legacy_board_id = uuid.uuid4()
    db_session.add(MarketBoard(
        id=legacy_board_id,
        externalCode=LEGACY_CODE,
        name=L3_NAME,  # legacy stored the full path as the L1 board name
        type="industry",
        taxonomy="qstock",
        source="qstock",
        taxonomyVersion="legacy-v1",
        taxonomyCompatibilityKey="qstock-board-v1",
        hierarchyLevel="L1",
        parentBoardId=None,
        membershipVersion="legacy-projection-20260801",
        isActive=True,
    ))
    await db_session.flush()

    # The currently-active PIT version of the legacy L1 full-path board.
    legacy_def_id = uuid.uuid4()
    legacy_definition = BoardDefinitionVersion(
        id=legacy_def_id,
        board_id=legacy_board_id,
        taxonomy="qstock",
        source="qstock",
        taxonomy_version="legacy-v1",
        taxonomy_compatibility_key="qstock-board-v1",
        identity_contract_version="unversioned",
        board_type="industry",
        hierarchy_level="L1",
        parent_board_id=None,
        membership_version="members:legacy",
        effective_from=date(2026, 8, 1),
        effective_to=None,  # open / active
        definition_hash="legacy-open-hash-placeholder",
    )
    db_session.add(legacy_definition)
    db_session.add(BoardMembershipHistory(
        board_definition_version_id=legacy_def_id,
        instrument_id=instrument_id,
        membership_version="members:legacy",
        effective_from=date(2026, 8, 1),
        effective_to=None,
    ))
    await db_session.flush()

    # L2 MarketBoard row must exist so board_key_to_id resolves the L3 parent.
    # The legacy board IS the new L3 board (same external_code => reuse the row),
    # so we only add an L2 MarketBoard here.
    l2_board_id = uuid.uuid4()
    db_session.add(MarketBoard(
        id=l2_board_id,
        externalCode=L2_CODE,
        name=L2_NAME,
        type="industry",
        taxonomy=BOARD_TAXONOMY,
        source=BOARD_SOURCE,
        taxonomyVersion=BOARD_TAXONOMY_VERSION,
        taxonomyCompatibilityKey=BOARD_TAXONOMY_COMPATIBILITY_KEY,
        hierarchyLevel="L2",
        parentBoardId=None,
        membershipVersion="legacy-projection-20260801",
        isActive=True,
    ))
    await db_session.flush()

    # legacy external_code == new L3 external_code => same MarketBoard row
    board_key_to_id = {
        (L2_CODE, "industry"): l2_board_id,
        (LEGACY_CODE, "industry"): legacy_board_id,
    }
    desired_memberships = {(legacy_board_id, instrument_id)}

    # ---- Phase 2: call real persistence / migration path --------------------
    snapshot = BoardSnapshot()
    snapshot.boards = [
        _make_board(L2_CODE, L2_NAME, "L2", None),
        _make_board(LEGACY_CODE, L3_NAME, "L3", L2_CODE),  # L3 reuses legacy code
    ]
    snapshot.memberships = {
        (L2_CODE, "industry"): ["MIGRTEST000001"],
        (LEGACY_CODE, "industry"): ["MIGRTEST000001"],
    }
    snapshot.raw_rows = 6000

    await svc._append_pit_history(
        db_session,
        snapshot=snapshot,
        board_key_to_id=board_key_to_id,
        desired_memberships=desired_memberships,
        deactivated_board_ids=[],
        effective_date=EFFECTIVE_DATE,
    )
    await db_session.flush()

    # ---- Phase 3: assertions ------------------------------------------------
    # 1. definition_hash changed (legacy "unversioned"/L1 != new wencai-identity-v1/L3)
    versions = (
        await db_session.execute(
            select(BoardDefinitionVersion)
            .where(BoardDefinitionVersion.board_id == legacy_board_id)
            .order_by(BoardDefinitionVersion.effective_from)
        )
    ).scalars().all()
    assert len(versions) == 2, f"expected 2 PIT versions, got {len(versions)}"
    old_version, new_version = versions[0], versions[1]
    assert old_version.definition_hash != new_version.definition_hash

    # 2. old PIT closed
    assert old_version.effective_to == EFFECTIVE_DATE
    # 3. new PIT created (open)
    assert new_version.effective_to is None
    assert new_version.effective_from == EFFECTIVE_DATE

    # 4. hierarchy_level becomes L3
    assert new_version.hierarchy_level == "L3"

    # 5. parent_board_id exists (resolved from parent_external_code = L2 code)
    assert new_version.parent_board_id == l2_board_id

    # 6. discover_pit_available_boards(level=L3) returns the migrated board
    # [REVIEW-EXECUTION-PATH-CONSOLIDATION] 该函数已迁移到 review_scope_service
    # （shadow runner 已删除），返回 canonical ScopeDefinition。
    from app.services.review_scope_service import discover_pit_available_boards

    specs = await discover_pit_available_boards(
        db_session, "industry", "L3", EFFECTIVE_DATE
    )
    returned_ids = {sp.scope_key for sp in specs}
    assert str(legacy_board_id) in returned_ids
