"""Pure-unit strategy search ID contract, plus mandatory real HTTP verification."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.api import strategy_candidate_research as api


class _Results:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeReadOnlySession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Results(self.rows)


def _run(monkeypatch, keyword, rows=None, *, available=True):
    monkeypatch.setattr(
        api, "_dataset",
        lambda: {"stocks": {"SH:600000": {}} if available else {}},
    )
    if rows is None:
        rows = [SimpleNamespace(market="SH", symbol="600000", name="浦发银行")]
    db = _FakeReadOnlySession(rows)
    result = asyncio.run(api.strategy_stock_search(keyword=keyword, _=None, db=db))
    return result, db


@pytest.mark.parametrize("keyword", ["600000", "SH:600000", "sh:600000", "浦发", "pfyh"])
def test_canonical_search_five_keywords(monkeypatch, keyword):
    result, db = _run(monkeypatch, keyword)
    assert result["scope"] == "STRATEGY_RESEARCH"
    assert result["items"] == [
        {"symbol": "SH:600000", "name": "浦发银行", "in_snapshot": True},
    ]
    assert result["available_count"] == 1
    assert db.statement is not None
    sql_values = list(db.statement.compile().params.values())
    if ":" in keyword:
        assert "SH" in sql_values
        assert "600000" in sql_values


@pytest.mark.parametrize("market,bare", [
    ("SH", "600000"), ("SZ", "000001"), ("BJ", "830799"),
])
def test_market_plus_raw_code_becomes_snapshot_identity(market, bare):
    assert api._strategy_full_symbol(market, bare) == f"{market}:{bare}"


def test_prefix_query_uses_market_and_bare_code(monkeypatch):
    _, db = _run(monkeypatch, "SH:600")
    values = list(db.statement.compile().params.values())
    assert "SH" in values
    assert any(str(value).startswith("600") for value in values)


@pytest.mark.parametrize("keyword", ["SH:", "SH:6000000", "XX:600000", "SH:6000A0", ":600000"])
def test_invalid_prefixed_search_stops_without_sql(monkeypatch, keyword):
    result, db = _run(monkeypatch, keyword)
    assert result["items"] == []
    assert result["available_count"] == 0
    assert db.statement is None


def test_symbol_not_present_in_snapshot_is_searchable_but_explicit(monkeypatch):
    result, _ = _run(monkeypatch, "浦发", available=False)
    assert result["items"][0]["symbol"] == "SH:600000"
    assert result["items"][0]["in_snapshot"] is False
    assert result["available_count"] == 0


@pytest.mark.parametrize("market,bare", [("US", "600000"), ("SH", "60000"), ("SH", "60000A")])
def test_invalid_master_identity_is_fail_closed(market, bare):
    with pytest.raises(ValueError, match="INVALID_STRATEGY_INSTRUMENT_ID"):
        api._strategy_full_symbol(market, bare)


def test_corrupt_returned_master_identity_rejected(monkeypatch):
    bad = SimpleNamespace(market="US", symbol="600000", name="invalid")
    monkeypatch.setattr(api, "_dataset", lambda: {"stocks": {}})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.strategy_stock_search(keyword="600000", _=None, db=_FakeReadOnlySession([bad])))
    assert exc.value.status_code == 503
