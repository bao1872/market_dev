"""Review V2 focused PG verification — Batch1+Batch2 combined.

Tests that don't need DB data run as unit; DB-dependent tests are pg-marked.
"""

import pytest

# =============================================================================
# Unit tests (no DB needed)
# =============================================================================


def test_static_d4_does_not_create_discovery():
    from app.domain.review.discovery import is_discovery_eligible
    assert is_discovery_eligible(["concentration_high"], ["D"]) is False


def test_change_signal_creates_discovery():
    from app.domain.review.discovery import is_discovery_eligible
    assert is_discovery_eligible(["low_level_repair"], ["B"]) is True


def test_style_led_requires_two_industries():
    from app.domain.review.cross_scope_relation import compute_relations
    style = {"discoveryId": "d1", "scope": {"type": "style", "key": "s1"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
    ind1 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 65.0}}}
    assert "STYLE_LED" not in {r.relation_type for r in compute_relations([style, ind1])}

    ind2 = {"discoveryId": "d3", "scope": {"type": "industry_l1", "key": "i2"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
    assert "STYLE_LED" in {r.relation_type for r in compute_relations([style, ind1, ind2])}


def test_discovery_lifecycle_from_signals():
    from app.domain.review.discovery import _derive_lifecycle
    lc = _derive_lifecycle(["continuing", "confirmed"], ["2026-08-01", "2026-08-03"])
    assert lc["status"] == "confirmed"
    assert lc["first_seen"] == "2026-08-01"
    assert lc["duration"] == 2


def test_signal_classification():
    from app.domain.review.discovery import classify_signal_evidence
    assert classify_signal_evidence("concentration_high", "D") == {
        "is_state": True, "is_change": False, "is_anomaly": False}
    assert classify_signal_evidence("low_level_repair", "B")["is_change"] is True


# =============================================================================
# PG tests (need verification DB with published Review data)
# =============================================================================

pytestmark_db = pytest.mark.pg


@pytest.mark.pg
@pytest.mark.asyncio
async def test_market_scope_cross_section_is_null(db_session):
    from sqlalchemy import select
    from app.models.market_review import MarketReviewScopeSnapshot, MarketReviewRun
    run = (await db_session.execute(
        select(MarketReviewRun).where(MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1))).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run available")
    snap = (await db_session.execute(
        select(MarketReviewScopeSnapshot).where(
            MarketReviewScopeSnapshot.review_run_id == run.id,
            MarketReviewScopeSnapshot.scope_type == "market"))).scalar_one_or_none()
    if snap is None:
        pytest.skip("No market scope snapshot")
    for payload in [snap.p_payload, snap.q_payload, snap.u_payload, snap.c_payload, snap.v_payload]:
        if payload:
            assert payload.get("crossSectionPercentile") is None


@pytest.mark.pg
@pytest.mark.asyncio
async def test_parallel_scopes_exist(db_session):
    from sqlalchemy import select, func
    from app.models.market_review import MarketReviewScopeSnapshot, MarketReviewRun
    run = (await db_session.execute(
        select(MarketReviewRun).where(MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1))).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")
    stmt = select(MarketReviewScopeSnapshot.scope_type, func.count(MarketReviewScopeSnapshot.id),
    ).where(MarketReviewScopeSnapshot.review_run_id == run.id,
            MarketReviewScopeSnapshot.scope_type.in_(
                ("industry_l1", "industry_l2", "industry_l3", "concept")),
    ).group_by(MarketReviewScopeSnapshot.scope_type)
    rows = (await db_session.execute(stmt)).all()
    types_found = {r[0] for r in rows}
    assert "industry_l1" in types_found


@pytest.mark.pg
@pytest.mark.asyncio
async def test_discovery_build_no_error(db_session):
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service
    run = (await db_session.execute(
        select(MarketReviewRun).where(MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1))).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")
    discoveries = await review_discovery_service.build_discoveries_for_run(db_session, run)
    assert isinstance(discoveries, list)


@pytest.mark.pg
@pytest.mark.asyncio
async def test_ranked_read_model(db_session):
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service
    run = (await db_session.execute(
        select(MarketReviewRun).where(MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1))).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")
    ranked, relations, by_id = await review_discovery_service.build_ranked_read_model(db_session, run)
    for d in ranked:
        assert d.rank_key, f"rank_key must be non-empty"
        dd = d.to_dict()
        assert dd["rankKey"] == d.rank_key
        assert d.discovery_id in by_id


@pytest.mark.pg
@pytest.mark.asyncio
async def test_list_detail_consistent(db_session):
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service
    run = (await db_session.execute(
        select(MarketReviewRun).where(MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1))).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")
    ranked, _, by_id = await review_discovery_service.build_ranked_read_model(db_session, run)
    if not ranked:
        pytest.skip("No discoveries")
    d = ranked[0]
    from_by_id = by_id.get(d.discovery_id)
    assert from_by_id is not None
    assert from_by_id.rank_key == d.rank_key
    assert from_by_id.status == d.status
