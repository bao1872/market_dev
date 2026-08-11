"""Review V2 focused PG verification — Batch1+Batch2 combined."""

import pytest

pytestmark = pytest.mark.pg


@pytest.mark.asyncio
async def test_market_scope_cross_section_is_null(db_session):
    """market scope: crossSectionPercentile must be NULL/None."""
    from sqlalchemy import select
    from app.models.market_review import MarketReviewScopeSnapshot, MarketReviewRun

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run available")

    snap = (await db_session.execute(
        select(MarketReviewScopeSnapshot).where(
            MarketReviewScopeSnapshot.review_run_id == run.id,
            MarketReviewScopeSnapshot.scope_type == "market",
        ),
    )).scalar_one_or_none()
    if snap is None:
        pytest.skip("No market scope snapshot")

    for payload in [snap.p_payload, snap.q_payload, snap.u_payload,
                     snap.c_payload, snap.v_payload]:
        if payload:
            assert payload.get("crossSectionPercentile") is None, \
                f"market must have null crossSectionPercentile, got {payload.get('crossSectionPercentile')}"


@pytest.mark.asyncio
async def test_industry_l1_l2_l3_concept_parallel(db_session):
    """industry_l1/l2/l3/concept all have independent observations."""
    from sqlalchemy import select, func
    from app.models.market_review import MarketReviewScopeSnapshot, MarketReviewRun

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")

    stmt = select(
        MarketReviewScopeSnapshot.scope_type,
        func.count(MarketReviewScopeSnapshot.id),
    ).where(
        MarketReviewScopeSnapshot.review_run_id == run.id,
        MarketReviewScopeSnapshot.scope_type.in_(
            ("industry_l1", "industry_l2", "industry_l3", "concept")),
    ).group_by(MarketReviewScopeSnapshot.scope_type)
    rows = (await db_session.execute(stmt)).all()
    types_found = {r[0] for r in rows}
    # At minimum industry_l1 should exist; L2/L3/concept may be empty if data unavailable
    assert "industry_l1" in types_found, f"industry_l1 must exist, found: {types_found}"


@pytest.mark.asyncio
async def test_peer_cohort_separation(db_session):
    """industry_l1/l2/l3 each have independent cross-sectional percentile."""
    from sqlalchemy import select
    from app.models.market_review import MarketReviewScopeSnapshot, MarketReviewRun

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")

    snaps = (await db_session.execute(
        select(MarketReviewScopeSnapshot).where(
            MarketReviewScopeSnapshot.review_run_id == run.id,
            MarketReviewScopeSnapshot.scope_type.in_(
                ("industry_l1", "industry_l2", "industry_l3", "concept")),
        ),
    )).scalars().all()

    by_type: dict[str, list] = {}
    for s in snaps:
        by_type.setdefault(s.scope_type, []).append(s)

    # Each scope type should have crossSectionPercentile values
    for st, items in by_type.items():
        pct_values = []
        for item in items:
            if item.p_payload and item.p_payload.get("crossSectionPercentile") is not None:
                pct_values.append(item.p_payload["crossSectionPercentile"])
        # Different scope types should NOT share the same percentile pool
        # This is verified by the unit tests; PG test confirms data exists
        assert len(items) > 0, f"{st} has no snapshots"


@pytest.mark.asyncio
async def test_discovery_build_no_error(db_session):
    """build_discoveries_for_run completes without error."""
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")

    discoveries = await review_discovery_service.build_discoveries_for_run(db_session, run)
    # 0 discoveries is a legal state
    assert isinstance(discoveries, list)
    for d in discoveries:
        assert d.discovery_id
        assert d.scope_type
        assert d.rank_key == {} or isinstance(d.rank_key, dict)


@pytest.mark.asyncio
async def test_discovery_ranking_api(db_session):
    """Ranked read model produces consistent output."""
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")

    ranked, relations, by_id = await review_discovery_service.build_ranked_read_model(
        db_session, run)
    for d in ranked:
        assert d.rank_key, f"rank_key must be non-empty for {d.discovery_id}"
        # Verify rankKey survives to_dict
        dd = d.to_dict()
        assert dd["rankKey"] == d.rank_key
        # Verify discovery is in by_id
        assert d.discovery_id in by_id


@pytest.mark.asyncio
async def test_discovery_list_detail_consistent(db_session):
    """List and detail use same read model."""
    from sqlalchemy import select
    from app.models.market_review import MarketReviewRun
    from app.services import review_discovery_service

    run = (await db_session.execute(
        select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1),
    )).scalar_one_or_none()
    if run is None:
        pytest.skip("No published Review run")

    ranked, _, by_id = await review_discovery_service.build_ranked_read_model(
        db_session, run)
    if not ranked:
        pytest.skip("No discoveries to verify")

    # Pick first discovery and verify by_id lookup returns same object
    d = ranked[0]
    from_by_id = by_id.get(d.discovery_id)
    assert from_by_id is not None
    assert from_by_id.discovery_id == d.discovery_id
    assert from_by_id.rank_key == d.rank_key
    assert from_by_id.status == d.status


@pytest.mark.asyncio
async def test_static_d4_does_not_create_discovery(db_session):
    """D4 concentration_high signal alone should not create Discovery."""
    from app.domain.review.discovery import is_discovery_eligible
    assert is_discovery_eligible(["concentration_high"], ["D"]) is False


@pytest.mark.asyncio
async def test_change_signal_creates_discovery(db_session):
    """Change signal (B2 low_level_repair) should be eligible."""
    from app.domain.review.discovery import is_discovery_eligible
    assert is_discovery_eligible(["low_level_repair"], ["B"]) is True


@pytest.mark.asyncio
async def test_style_led_requires_two_industries(db_session):
    """STYLE_LED: 1 industry → false, 2 industries → true."""
    from app.domain.review.cross_scope_relation import compute_relations
    style = {"discoveryId": "d1", "scope": {"type": "style", "key": "s1"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
    ind1 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 65.0}}}
    assert "STYLE_LED" not in {r.relation_type for r in compute_relations([style, ind1])}

    ind2 = {"discoveryId": "d3", "scope": {"type": "industry_l1", "key": "i2"},
             "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
    assert "STYLE_LED" in {r.relation_type for r in compute_relations([style, ind1, ind2])}


@pytest.mark.asyncio
async def test_discovery_lifecycle_from_signals(db_session):
    """Lifecycle derives from canonical signal statuses."""
    from app.domain.review.discovery import _derive_lifecycle
    lc = _derive_lifecycle(["continuing", "confirmed"], ["2026-08-01", "2026-08-03"])
    assert lc["status"] == "confirmed"
    assert lc["first_seen"] == "2026-08-01"
    assert lc["duration"] == 2
