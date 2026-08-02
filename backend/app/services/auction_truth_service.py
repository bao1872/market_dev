"""竞价最终报价的独立来源聚合与一致性门禁。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from app.services.auction_quote_provider import AuctionQuoteResult

VERIFIED_AUCTION_SOURCE = "verified_consensus"
BLOCKED_EXTERNAL_REASON = "blocked_external_auction_truth_source"


@dataclass(frozen=True)
class AuctionTruthPolicy:
    min_independent_sources: int = 2
    price_tick: Decimal = Decimal("0.01")
    volume_relative_tolerance: Decimal = Decimal("0.02")
    amount_relative_tolerance: Decimal = Decimal("0.02")


@dataclass(frozen=True)
class AuctionTruthDecision:
    symbol: str
    market: str
    status: str
    quote: AuctionQuoteResult | None
    independent_source_count: int
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]


def _relative_spread(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    high = max(values)
    low = min(values)
    denominator = max(abs(high), abs(low), Decimal("1"))
    return (high - low) / denominator


def decide_auction_truth(
    quotes: Iterable[AuctionQuoteResult],
    *,
    policy: AuctionTruthPolicy | None = None,
) -> AuctionTruthDecision:
    """按 provider_family 去重后验证价格、量和额的一致性。"""
    resolved_policy = policy or AuctionTruthPolicy()
    candidates = [q for q in quotes if q.is_valid and q.is_final_auction]
    if not candidates:
        raise ValueError("quotes must contain at least one valid final auction quote")

    symbol = candidates[0].symbol
    market = candidates[0].market
    if any((q.symbol, q.market) != (symbol, market) for q in candidates):
        raise ValueError("all quotes must identify the same instrument")

    by_family: dict[str, AuctionQuoteResult] = {}
    for quote in sorted(candidates, key=lambda q: (q.provider_family, q.source_id, q.source_server or "")):
        by_family.setdefault(quote.provider_family, quote)
    independent = list(by_family.values())
    evidence = {
        "sources": [
            {
                "source_id": q.source_id,
                "provider_family": q.provider_family,
                "source_server": q.source_server,
                "source_timestamp": q.servertime,
                "capture_time": q.captured_at.isoformat(),
                "final_price": q.price,
                "prev_close": q.last_close,
                "volume": q.volume,
                "amount": q.amount,
                "raw_payload": q.raw_payload,
            }
            for q in candidates
        ],
        "independent_families": sorted(by_family),
    }
    if len(independent) < resolved_policy.min_independent_sources:
        return AuctionTruthDecision(
            symbol=symbol,
            market=market,
            status="blocked_external",
            quote=None,
            independent_source_count=len(independent),
            reason_codes=(BLOCKED_EXTERNAL_REASON,),
            evidence=evidence,
        )

    prices = [Decimal(str(q.price)) for q in independent if q.price is not None]
    volumes = [Decimal(str(q.volume)) for q in independent if q.volume is not None]
    amounts = [Decimal(str(q.amount)) for q in independent if q.amount is not None]
    reasons: list[str] = []
    if len(prices) != len(independent) or max(prices) - min(prices) > resolved_policy.price_tick:
        reasons.append("auction_truth_price_conflict")
    if len(volumes) != len(independent) or _relative_spread(volumes) > resolved_policy.volume_relative_tolerance:
        reasons.append("auction_truth_volume_conflict")
    if len(amounts) != len(independent) or _relative_spread(amounts) > resolved_policy.amount_relative_tolerance:
        reasons.append("auction_truth_amount_conflict")
    if reasons:
        return AuctionTruthDecision(
            symbol=symbol,
            market=market,
            status="conflict",
            quote=None,
            independent_source_count=len(independent),
            reason_codes=tuple(reasons),
            evidence=evidence,
        )

    representative = independent[0]
    verified = AuctionQuoteResult(
        symbol=symbol,
        market=market,
        price=representative.price,
        last_close=representative.last_close,
        open=representative.open,
        high=representative.high,
        low=representative.low,
        volume=representative.volume,
        amount=representative.amount,
        servertime=representative.servertime,
        quality_status="ok",
        reason_codes=[],
        raw_payload={"truth_evidence": evidence},
        source_server=",".join(sorted(q.source_id for q in independent)),
        captured_at=max(q.captured_at for q in independent),
        is_final_auction=True,
        source_id=VERIFIED_AUCTION_SOURCE,
        provider_family=VERIFIED_AUCTION_SOURCE,
    )
    return AuctionTruthDecision(
        symbol=symbol,
        market=market,
        status="verified",
        quote=verified,
        independent_source_count=len(independent),
        reason_codes=(),
        evidence=evidence,
    )


def aggregate_auction_truth(
    source_quotes: Iterable[Iterable[AuctionQuoteResult]],
    *,
    expected_symbols: Iterable[tuple[str, str]],
    provider_families: Iterable[str] | None = None,
    policy: AuctionTruthPolicy | None = None,
) -> dict[str, Any]:
    """合并各来源结果，保留 partial/conflict/blocked 的逐股证据。"""
    grouped: dict[tuple[str, str], list[AuctionQuoteResult]] = {}
    for quotes in source_quotes:
        for quote in quotes:
            grouped.setdefault((quote.symbol, quote.market), []).append(quote)

    configured_families = set(provider_families or ())
    if not configured_families:
        configured_families = {
            quote.provider_family
            for quotes in grouped.values()
            for quote in quotes
        }
    decisions: list[AuctionTruthDecision] = []
    for symbol, market in expected_symbols:
        candidates = grouped.get((symbol, market), [])
        valid = [q for q in candidates if q.is_valid and q.is_final_auction]
        if not valid:
            decisions.append(AuctionTruthDecision(
                symbol=symbol,
                market=market,
                status="partial",
                quote=None,
                independent_source_count=0,
                reason_codes=("auction_truth_quote_missing",),
                evidence={"sources": []},
            ))
            continue
        decision = decide_auction_truth(valid, policy=policy)
        resolved_policy = policy or AuctionTruthPolicy()
        if (
            decision.status == "blocked_external"
            and len(configured_families) >= resolved_policy.min_independent_sources
        ):
            decision = replace(
                decision,
                status="partial",
                reason_codes=("auction_truth_independent_quote_missing",),
            )
        decisions.append(decision)

    verified_quotes = [decision.quote for decision in decisions if decision.quote is not None]
    statuses = {decision.status for decision in decisions}
    if statuses == {"verified"}:
        status = "verified"
    elif "conflict" in statuses:
        status = "conflict"
    elif len(configured_families) < (policy or AuctionTruthPolicy()).min_independent_sources:
        status = "blocked_external"
    else:
        status = "partial"
    return {
        "status": status,
        "verified_quotes": verified_quotes,
        "decisions": decisions,
        "expected_count": len(decisions),
        "verified_count": len(verified_quotes),
        "coverage": len(verified_quotes) / len(decisions) if decisions else 0.0,
    }


class VerifiedAuctionQuoteProvider:
    """把已验证共识结果交给现有 capture 持久化合同。"""

    def __init__(self, quotes: Iterable[AuctionQuoteResult]) -> None:
        self._quotes = {(q.symbol, q.market): q for q in quotes}

    @property
    def source_id(self) -> str:
        return VERIFIED_AUCTION_SOURCE

    @property
    def provider_family(self) -> str:
        return VERIFIED_AUCTION_SOURCE

    def fetch_auction_quotes(self, symbols: list[tuple[str, str]]) -> list[AuctionQuoteResult]:
        return [self._quotes[key] for key in symbols if key in self._quotes]

    def close(self) -> None:
        return None


class StaticAuctionQuoteProvider(VerifiedAuctionQuoteProvider):
    """将一次来源抓取结果交给 capture service 持久化，避免二次请求。"""

    def __init__(
        self,
        quotes: Iterable[AuctionQuoteResult],
        *,
        source_id: str,
        provider_family: str,
    ) -> None:
        super().__init__(quotes)
        self._source_id = source_id
        self._provider_family = provider_family

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def provider_family(self) -> str:
        return self._provider_family


async def fetch_quote_sources(
    providers: Iterable[Any],
    symbols: list[tuple[str, str]],
) -> list[tuple[str, str, list[AuctionQuoteResult]]]:
    """并发抓取来源，并将 provider 声明的供应链身份写入 DTO。"""
    provider_list = list(providers)

    async def fetch(provider: Any) -> tuple[str, str, list[AuctionQuoteResult]]:
        source_id = str(getattr(provider, "source_id", type(provider).__name__))
        family = str(getattr(provider, "provider_family", source_id))
        try:
            raw_quotes = await asyncio.to_thread(provider.fetch_auction_quotes, symbols)
        finally:
            provider.close()
        quotes = [
            AuctionQuoteResult(
                symbol=q.symbol,
                market=q.market,
                price=q.price,
                last_close=q.last_close,
                open=q.open,
                high=q.high,
                low=q.low,
                volume=q.volume,
                amount=q.amount,
                servertime=q.servertime,
                quality_status=q.quality_status,
                reason_codes=list(q.reason_codes),
                raw_payload=q.raw_payload,
                source_server=q.source_server,
                captured_at=q.captured_at,
                is_final_auction=q.is_final_auction,
                source_id=source_id,
                provider_family=family,
            )
            for q in raw_quotes
        ]
        return source_id, family, quotes

    return list(await asyncio.gather(*(fetch(provider) for provider in provider_list)))
