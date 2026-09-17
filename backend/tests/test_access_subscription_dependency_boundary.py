"""Dependency and compatibility guards for access/subscription boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import subscription_service
from app.services.subscription_summary_service import (
    SubscriptionCommercialResult,
    SubscriptionSummary,
    resolve_commercial_status,
    resolve_subscription_summary,
)


def test_subscription_service_preserves_summary_compatibility_exports() -> None:
    assert subscription_service.SubscriptionCommercialResult is SubscriptionCommercialResult
    assert subscription_service.SubscriptionSummary is SubscriptionSummary
    assert subscription_service.resolve_commercial_status is resolve_commercial_status
    assert subscription_service.resolve_subscription_summary is resolve_subscription_summary


def test_effective_access_does_not_import_subscription_write_service() -> None:
    app_dir = Path(__file__).parent.parent / "app"
    path = app_dir / "services" / "effective_access_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.services.subscription_service"
    ]
    assert not violations, f"effective access imports subscription write service: {violations}"
