"""Pure-unit contracts for versioned Board hierarchy and batch identity."""
from __future__ import annotations

import inspect
from pathlib import Path

from app.models.board_analysis_snapshot import BoardAnalysisRun, BoardAnalysisSnapshot
from app.models.board_taxonomy import (
    BoardDefinitionVersion,
    BoardMembershipHistory,
    UniverseDefinition,
    UniverseMembership,
)
from app.models.market_board import MarketBoard
from app.services import (
    board_analysis_service,
    board_membership_service,
    board_sync_service,
    factor_publication_service,
    review_orchestrator_service,
    review_scope_service,
)
from app.services.board_membership_service import batch_version

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/079_board_hierarchy_batch_identity.py"


def test_versioned_taxonomy_models_are_registered() -> None:
    assert BoardDefinitionVersion.__tablename__ == "board_definition_versions"
    assert BoardMembershipHistory.__tablename__ == "board_membership_history"
    assert UniverseDefinition.__tablename__ == "universe_definitions"
    assert UniverseMembership.__tablename__ == "universe_memberships"
    assert BoardAnalysisRun.__tablename__ == "board_analysis_runs"


def test_snapshot_identity_is_batch_plus_board() -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in BoardAnalysisSnapshot.__table__.constraints
        if constraint.name
    }
    assert constraints["uq_board_analysis_snapshots_run_board"] == (
        "board_analysis_run_id",
        "board_id",
    )
    assert BoardAnalysisSnapshot.__table__.c.board_analysis_run_id.nullable is False
    assert "uq_board_analysis_snapshots_date_board_ver" not in constraints


def test_batch_identity_includes_taxonomy_compatibility() -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in BoardAnalysisRun.__table__.constraints
        if constraint.name
    }
    assert constraints["uq_board_analysis_runs_identity"] == (
        "trade_date",
        "source_core_run_id",
        "taxonomy_version",
        "taxonomy_compatibility_key",
        "algorithm_version",
        "membership_version",
    )


def test_universes_are_not_market_board_types() -> None:
    migration = MIGRATION.read_text()
    assert migration.count("'major_index'") >= 2
    assert migration.count("'style'") >= 2
    assert "'csi300'" in migration
    assert "'csi500'" in migration
    assert "'large_cap_style'" in migration
    assert "'small_cap_style'" in migration
    assert "blocked_external_population" in migration
    assert "type='major_index'" not in migration
    assert "type='style'" not in migration
    assert "industry | concept" in (MarketBoard.type.property.columns[0].comment or "")


def test_pit_intervals_are_half_open_and_have_no_projection_fallback() -> None:
    board_source = inspect.getsource(board_membership_service.resolve_board_membership_at)
    universe_source = inspect.getsource(
        board_membership_service.resolve_universe_membership_at,
    )
    for source in (board_source, universe_source):
        assert "effective_from <= trade_date" in source
        assert "effective_to > trade_date" in source
        assert "MarketBoardMembership" not in source
        assert "market_board_memberships" not in source


def test_review_scope_resolution_requires_trade_date_and_pit_services() -> None:
    signature = inspect.signature(review_scope_service.resolve_scope_members)
    assert signature.parameters["trade_date"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(review_scope_service.resolve_scope_members)
    assert "resolve_board_membership_at" in source
    assert "resolve_universe_membership_at" in source
    assert "MarketBoardMembership" not in source


def test_review_lists_configured_universes_and_l1_only() -> None:
    major = inspect.getsource(review_orchestrator_service._list_major_index_scopes)
    style = inspect.getsource(review_orchestrator_service._list_style_scopes)
    industry = inspect.getsource(review_orchestrator_service._list_industry_l1_scopes)
    assert 'universe_type="major_index"' in major
    assert 'universe_type="style"' in style
    assert "definition.universe_key" in major
    assert "definition.universe_key" in style
    assert 'MarketBoard.hierarchyLevel == "L1"' in industry
    assert "BoardAnalysisSnapshot.board_analysis_run_id" in industry


def test_new_publications_target_real_board_batch() -> None:
    board_publish = inspect.getsource(board_analysis_service.publish_board_analysis)
    market_publish = inspect.getsource(
        factor_publication_service.publish_market_aggregation,
    )
    assert "data_run_id=snapshot.board_analysis_run_id" in board_publish
    assert "session.get(BoardAnalysisRun, aggregation_run_id)" in market_publish
    assert 'board_run.status != "succeeded"' in market_publish
    assert "board_run.failed_count != 0" in market_publish


def test_legacy_snapshot_pointer_reader_is_preserved() -> None:
    source = inspect.getsource(
        board_analysis_service.get_published_board_snapshot_id,
    )
    assert "session.get(BoardAnalysisSnapshot, pub.data_run_id)" in source
    assert "BoardAnalysisSnapshot.board_analysis_run_id == pub.data_run_id" in source


def test_sync_deactivates_projection_and_preserves_history() -> None:
    switch_source = inspect.getsource(board_sync_service._atomic_switch)
    history_source = inspect.getsource(board_sync_service._append_pit_history)
    assert ".values(isActive=False" in switch_source
    assert "delete(MarketBoard)" not in switch_source
    assert "BoardDefinitionVersion" in history_source
    assert "BoardMembershipHistory" in history_source
    assert ".values(effective_to=effective_date)" in history_source


def test_formal_batch_blocks_unpopulated_universes() -> None:
    source = inspect.getsource(board_analysis_service.compute_all_boards)
    assert "list_universe_definitions_at" in source
    assert "resolve_universe_membership_at" in source
    assert '"blocked_external_population"' in source
    assert "expected_count = len(boards) + len(universe_definitions)" in source


def test_batch_version_is_deterministic_and_order_independent() -> None:
    assert batch_version(["b", "a", "a"], prefix="membership") == batch_version(
        ["a", "b"], prefix="membership",
    )
