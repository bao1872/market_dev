"""Regression tests for Review Discovery API endpoints.

GUARDS ROUND 4 FIRST_BLOCKER REVIEW_DISCOVERY_USER_PATH_SERVER_ERROR:

The Discovery list/detail endpoints referenced a helper
``_get_published_run_or_404`` that was never defined in ``app.api.review``.
Every authenticated call raised ``NameError`` -> HTTP 500 ("服务器错误"),
blocking the entire Review Discovery user path even when zero Discovery is
a legitimate (empty-list) result.

These tests run without a database:
- test_discovery_helper_symbol_defined: importing the router and referencing the
  previously-missing symbol must not raise NameError.
- test_discovery_list_returns_empty_when_no_published_run: calling the list
  endpoint handler directly with a fake ctx/session must return a valid empty
  ``ReviewDiscoveryListResponse`` (not raise), matching the zero-Discovery
  contract (HTTP 200 empty list, never 500).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock


def _make_fake_session(no_run: bool):
    """Fake AsyncSession: execute().scalar_one_or_none() is None when no_run."""
    result = SimpleNamespace(scalar_one_or_none=lambda: None if no_run else object())
    return SimpleNamespace(execute=AsyncMock(return_value=result))


def test_discovery_helper_symbol_defined():
    """The previously-missing symbol must exist and be callable."""
    from app.api import review as review_api

    assert hasattr(review_api, "_get_published_run_or_404")
    assert callable(review_api._get_published_run_or_404)


async def test_discovery_list_returns_empty_when_no_published_run():
    """Zero Discovery is legitimate -> valid empty response, never NameError/500."""
    from app.api import review as review_api
    from app.api.review import ReviewDiscoveryListResponse

    ctx = SimpleNamespace(is_admin=False, user_id="test", capabilities=set())
    db = _make_fake_session(no_run=True)

    resp = await review_api.list_discoveries(
        trade_date=__import__("datetime").date(2026, 8, 11),
        scope_type=None,
        scope_family=None,
        lifecycle_status=None,
        sort=None,
        include_partial=False,
        page=1,
        page_size=20,
        db=db,
        ctx=ctx,
    )
    assert isinstance(resp, ReviewDiscoveryListResponse)
    assert resp.total == 0
    assert resp.items == []
