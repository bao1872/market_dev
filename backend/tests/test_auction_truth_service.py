from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.auction import AuctionFinalQuoteOut
from app.services.auction_publication_service import evaluate_auction_publication_gate
from app.services.auction_quote_provider import AuctionQuoteResult
from app.services.auction_truth_service import (
    BLOCKED_EXTERNAL_REASON,
    aggregate_auction_truth,
    decide_auction_truth,
)


def _quote(
    source_id: str,
    family: str,
    *,
    server: str | None = None,
    price: float = 10.0,
    volume: float = 1000.0,
    amount: float = 10000.0,
) -> AuctionQuoteResult:
    return AuctionQuoteResult(
        symbol="600000",
        market="SH",
        price=price,
        last_close=9.9,
        open=price,
        high=price,
        low=price,
        volume=volume,
        amount=amount,
        servertime="09:25:05",
        quality_status="ok",
        raw_payload={"source": source_id},
        source_server=server,
        captured_at=datetime(2026, 8, 3, 1, 25, 6, tzinfo=UTC),
        is_final_auction=True,
        source_id=source_id,
        provider_family=family,
    )


def test_single_source_is_explicitly_blocked() -> None:
    decision = decide_auction_truth([_quote("mootdx", "tongdaxin")])
    assert decision.status == "blocked_external"
    assert decision.reason_codes == (BLOCKED_EXTERNAL_REASON,)
    assert decision.quote is None


def test_two_servers_on_same_supply_chain_are_not_independent() -> None:
    decision = decide_auction_truth([
        _quote("mootdx-a", "tongdaxin", server="a:7709"),
        _quote("mootdx-b", "tongdaxin", server="b:7709"),
    ])
    assert decision.status == "blocked_external"
    assert decision.independent_source_count == 1


def test_two_independent_sources_produce_canonical_final_quote() -> None:
    decision = decide_auction_truth([
        _quote("source-a", "family-a", price=10.00),
        _quote("source-b", "family-b", price=10.01, volume=1010, amount=10100),
    ])
    assert decision.status == "verified"
    assert decision.quote is not None
    assert decision.quote.source_id == "verified_consensus"
    assert decision.quote.is_final_auction is True
    assert decision.quote.captured_at.tzinfo is not None
    assert decision.quote.raw_payload is not None
    assert "truth_evidence" in decision.quote.raw_payload
    dto = AuctionFinalQuoteOut.model_validate({
        "symbol": decision.quote.symbol,
        "market": decision.quote.market,
        "final_price": decision.quote.price,
        "prev_close": decision.quote.last_close,
        "volume": decision.quote.volume,
        "amount": decision.quote.amount,
        "source_timestamp": decision.quote.captured_at,
        "source_server": decision.quote.source_server,
        "raw_payload": decision.quote.raw_payload,
        "capture_time": decision.quote.captured_at,
        "is_final_auction": decision.quote.is_final_auction,
    })
    assert dto.symbol == "600000"
    assert dto.is_final_auction is True


def test_price_conflict_blocks_consensus() -> None:
    decision = decide_auction_truth([
        _quote("source-a", "family-a", price=10.00),
        _quote("source-b", "family-b", price=10.02),
    ])
    assert decision.status == "conflict"
    assert "auction_truth_price_conflict" in decision.reason_codes


def test_volume_and_amount_conflicts_are_separate() -> None:
    decision = decide_auction_truth([
        _quote("source-a", "family-a"),
        _quote("source-b", "family-b", volume=1100, amount=11000),
    ])
    assert set(decision.reason_codes) == {
        "auction_truth_volume_conflict",
        "auction_truth_amount_conflict",
    }


def test_partial_capture_never_becomes_verified() -> None:
    result = aggregate_auction_truth(
        [[_quote("source-a", "family-a")]],
        expected_symbols=[("600000", "SH"), ("000001", "SZ")],
    )
    assert result["status"] == "blocked_external"
    assert result["coverage"] == 0.0
    assert {decision.status for decision in result["decisions"]} == {
        "blocked_external",
        "partial",
    }


def test_two_configured_independent_sources_with_missing_symbol_is_partial() -> None:
    result = aggregate_auction_truth(
        [
            [_quote("source-a", "family-a")],
            [],
        ],
        expected_symbols=[("600000", "SH")],
        provider_families=["family-a", "family-b"],
    )
    assert result["status"] == "partial"
    assert result["decisions"][0].reason_codes == (
        "auction_truth_independent_quote_missing",
    )


def test_formal_publication_gate_requires_truth_full_chain_and_production() -> None:
    assert evaluate_auction_publication_gate(
        truth_status="verified",
        test_namespace="production",
        scan_status="succeeded",
        scan_coverage=0.95,
        capture_source="verified_consensus",
        capture_status="succeeded",
        scope_count=3,
    ) == []
    reasons = evaluate_auction_publication_gate(
        truth_status="conflict",
        test_namespace="auction_canary",
        scan_status="partial",
        scan_coverage=0.94,
        capture_source="mootdx",
        capture_status="partial",
        scope_count=0,
    )
    assert set(reasons) == {
        "auction_truth_not_verified",
        "canary_or_test_namespace",
        "scan_not_succeeded",
        "scan_coverage_below_threshold",
        "capture_source_not_verified_consensus",
        "capture_not_succeeded",
        "aggregation_missing",
    }
