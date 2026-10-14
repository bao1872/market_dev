"""New strategy right is independent; existing read routes must not fall back to market right."""
from app.models.user_capability import ALL_CAPABILITIES, CAPABILITY_STRATEGY_RESEARCH
from app.services.effective_access_service import compute_default_route, CapabilityState


def _c(key: str):
    return CapabilityState(key=key, active=True, granted_at=None, expires_at=None,
                           watchlist_limit=None, source="admin_grant", reason="active")


def test_fifth_capability_independent_entry():
    assert len(ALL_CAPABILITIES)==5
    assert CAPABILITY_STRATEGY_RESEARCH in ALL_CAPABILITIES
    assert compute_default_route(False, {CAPABILITY_STRATEGY_RESEARCH:_c(CAPABILITY_STRATEGY_RESEARCH)}) == '/strategy'
    assert compute_default_route(False, {'research_replay':_c('research_replay')}) == '/auction'
    assert compute_default_route(False, {'market_data':_c('market_data')}) == '/market'
    assert compute_default_route(False, {'market_review':_c('market_review')}) == '/review'
