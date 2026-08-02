"""竞价分析正式发布门禁与专属 pointer。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import (
    AuctionAnalysisPublication,
    AuctionQuoteCaptureRun,
    AuctionScanRun,
    AuctionScopeResult,
)
from app.services.auction_truth_service import VERIFIED_AUCTION_SOURCE

MIN_FORMAL_COVERAGE = 0.95


class AuctionPublicationGateError(ValueError):
    """竞价真值、扫描或聚合未达到正式发布条件。"""


def evaluate_auction_publication_gate(
    *,
    truth_status: str,
    test_namespace: str,
    scan_status: str | None,
    scan_coverage: float | None,
    capture_source: str | None,
    capture_status: str | None,
    scope_count: int,
) -> list[str]:
    reasons: list[str] = []
    if truth_status != "verified":
        reasons.append("auction_truth_not_verified")
    if test_namespace != "production":
        reasons.append("canary_or_test_namespace")
    if scan_status != "succeeded":
        reasons.append("scan_not_succeeded")
    if scan_coverage is None or scan_coverage < MIN_FORMAL_COVERAGE:
        reasons.append("scan_coverage_below_threshold")
    if capture_source != VERIFIED_AUCTION_SOURCE:
        reasons.append("capture_source_not_verified_consensus")
    if capture_status != "succeeded":
        reasons.append("capture_not_succeeded")
    if scope_count == 0:
        reasons.append("aggregation_missing")
    return reasons


async def publish_auction_analysis(
    db: AsyncSession,
    *,
    scan_run_id: uuid.UUID,
    capture_run_id: uuid.UUID,
    truth_status: str,
    test_namespace: str,
) -> AuctionAnalysisPublication:
    scan_run = await db.get(AuctionScanRun, scan_run_id)
    capture_run = await db.get(AuctionQuoteCaptureRun, capture_run_id)
    reasons: list[str] = []
    if scan_run is None:
        reasons.append("scan_run_missing")
    if capture_run is None:
        reasons.append("capture_run_missing")
    scope_count = 0
    if scan_run is not None:
        scope_count = int((await db.execute(
            select(func.count(AuctionScopeResult.id)).where(
                AuctionScopeResult.scan_run_id == scan_run.id,
            )
        )).scalar_one())
    reasons.extend(evaluate_auction_publication_gate(
        truth_status=truth_status,
        test_namespace=test_namespace,
        scan_status=scan_run.status if scan_run is not None else None,
        scan_coverage=scan_run.coverage_ratio if scan_run is not None else None,
        capture_source=capture_run.source if capture_run is not None else None,
        capture_status=capture_run.status if capture_run is not None else None,
        scope_count=scope_count,
    ))
    if reasons or scan_run is None or capture_run is None:
        raise AuctionPublicationGateError(",".join(reasons))

    evidence: dict[str, Any] = {
        "truth_status": truth_status,
        "capture_source": capture_run.source,
        "capture_coverage": capture_run.coverage,
        "scan_status": scan_run.status,
        "scan_coverage": scan_run.coverage_ratio,
        "scope_result_count": scope_count,
        "minimum_coverage": MIN_FORMAL_COVERAGE,
    }
    statement = pg_insert(AuctionAnalysisPublication).values(
        trade_date=scan_run.trade_date,
        scan_run_id=scan_run.id,
        capture_run_id=capture_run.id,
        algorithm_version=scan_run.algorithm_version,
        test_namespace=test_namespace,
        coverage_ratio=scan_run.coverage_ratio,
        truth_status=truth_status,
        gate_evidence=evidence,
    ).on_conflict_do_update(
        constraint="uq_auction_analysis_publication_date_version",
        set_={
            "scan_run_id": scan_run.id,
            "capture_run_id": capture_run.id,
            "coverage_ratio": scan_run.coverage_ratio,
            "truth_status": truth_status,
            "gate_evidence": evidence,
            "test_namespace": test_namespace,
        },
    ).returning(AuctionAnalysisPublication.id)
    publication_id = (await db.execute(statement)).scalar_one()
    await db.flush()
    publication = await db.get(AuctionAnalysisPublication, publication_id)
    if publication is None:
        raise RuntimeError("auction publication upsert did not return a row")
    return publication
