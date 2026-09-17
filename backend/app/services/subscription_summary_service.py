"""Read-only commercial subscription status and summary owner.

This boundary deliberately has no dependency on capability authorization. It
turns Subscription and Plan facts into commercial state consumed by display
paths and the explicitly marked legacy-plan compatibility adapter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.services.plan_service import get_plan as get_plan_async


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class SubscriptionCommercialResult:
    """Normalized commercial state with a fail-closed diagnostic reason."""

    status: Literal["none", "pending", "active", "expired", "revoked", "cancelled"]
    reason: str | None = None


def resolve_commercial_status(
    subscription: Any,
    now: datetime | None = None,
) -> SubscriptionCommercialResult:
    """Resolve commercial state without deriving any functional capability."""
    if subscription is None:
        return SubscriptionCommercialResult(status="none", reason="no_subscription")
    now = now or datetime.now(UTC)

    persistent = getattr(subscription, "status", None)
    if persistent in ("revoked", "cancelled"):
        return SubscriptionCommercialResult(status=persistent, reason=persistent)

    raw_starts_at = getattr(subscription, "starts_at", None)
    raw_expires_at = getattr(subscription, "expires_at", None)
    starts_at = _ensure_aware(raw_starts_at) if raw_starts_at is not None else None
    expires_at = _ensure_aware(raw_expires_at) if raw_expires_at is not None else None

    if starts_at is None:
        return SubscriptionCommercialResult(status="expired", reason="missing_starts_at")
    if expires_at is None:
        return SubscriptionCommercialResult(status="expired", reason="missing_expires_at")
    if starts_at > expires_at:
        return SubscriptionCommercialResult(status="expired", reason="invalid_period")
    if starts_at > now:
        return SubscriptionCommercialResult(status="pending", reason="not_started")
    if expires_at <= now:
        return SubscriptionCommercialResult(status="expired", reason="expired")
    return SubscriptionCommercialResult(status="active", reason="active")


@dataclass(frozen=True)
class SubscriptionSummary:
    """Commercial summary; explicit capabilities ignore it during authorization."""

    status: str
    reason: str | None
    active: bool
    plan_code: str | None
    plan_display_name: str | None
    starts_at: datetime | None
    expires_at: datetime | None
    source: str | None
    entitlement_snapshot: dict | None
    features: list[str]
    limits: dict[str, int]


async def resolve_subscription_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> SubscriptionSummary:
    """Load Subscription and Plan once and build the commercial summary."""
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalars().first()
    commercial = resolve_commercial_status(sub)
    plan_code = sub.plan_code if sub else None
    plan = await get_plan_async(db, plan_code) if plan_code else None
    starts_at = _ensure_aware(sub.starts_at) if sub and sub.starts_at else None
    expires_at = _ensure_aware(sub.expires_at) if sub and sub.expires_at else None
    return SubscriptionSummary(
        status=commercial.status,
        reason=commercial.reason,
        active=commercial.status == "active",
        plan_code=plan_code,
        plan_display_name=plan.display_name if plan else None,
        starts_at=starts_at,
        expires_at=expires_at,
        source=getattr(sub, "source", None) if sub else None,
        entitlement_snapshot=getattr(sub, "entitlement_snapshot", None) if sub else None,
        features=list(plan.features) if plan and plan.features else [],
        limits={
            "monitor_limit": int(plan.monitor_limit),
            "notification_channel_limit": int(plan.notification_channel_limit),
            "message_retention_days": int(plan.message_retention_days),
        }
        if plan
        else {},
    )
