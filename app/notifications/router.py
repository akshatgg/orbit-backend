"""Notification preferences and push subscriptions for the signed-in user."""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.notifications.channels import (
    StoredSubscription,
    delete_subscriptions,
    push_available,
    send_push,
)
from app.notifications.messages import push_test_message
from app.notifications.models import NotificationPreference, PushSubscription
from app.notifications.schemas import (
    EndpointBody,
    NotificationPreferencesUpdate,
    PushSubscriptionBody,
)
from app.notifications.service import DEFAULT_PREFERENCES
from app.sql import functions as fn

router = APIRouter(tags=["notifications"])
logger = logging.getLogger(__name__)


def _preferences(prefs: NotificationPreference | None, user_id: Any) -> dict[str, Any]:
    if prefs is None:
        return {"user_id": user_id, **DEFAULT_PREFERENCES}
    return {
        "user_id": prefs.user_id,
        "push_enabled": prefs.push_enabled,
        "email_enabled": prefs.email_enabled,
        "remind_before_hours": prefs.remind_before_hours,
        "deadline_reminders": prefs.deadline_reminders,
        "updated_at": prefs.updated_at,
    }


@router.get("/me/notification-preferences")
async def get_notification_preferences(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """The caller's notification preferences, or the defaults when none are saved."""
    prefs = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user["id"]
            )
        )
    ).scalar_one_or_none()
    return _preferences(prefs, user["id"])


@router.put("/me/notification-preferences")
async def put_notification_preferences(
    body: NotificationPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Save the caller's notification preferences."""
    values = {
        key: value
        for key, value in {**DEFAULT_PREFERENCES, **body.model_dump(exclude_unset=True)}.items()
        if value is not None
    }
    values["updated_at"] = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(NotificationPreference)
        .values(user_id=user["id"], **values)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={key: value for key, value in values.items()},
        )
    )
    prefs = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.user_id == user["id"]
            )
        )
    ).scalar_one()
    return _preferences(prefs, user["id"])


@router.post("/push/subscriptions")
async def save_push_subscription(
    body: PushSubscriptionBody,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Store this browser's push subscription.

    `old_endpoint` is the subscription it replaces when the browser rotates one
    (the service worker's pushsubscriptionchange); it is removed.
    """
    subscription_id = await fn.save_push_subscription(
        db, body.endpoint, body.keys.p256dh, body.keys.auth, body.user_agent
    )
    if body.old_endpoint and body.old_endpoint != body.endpoint:
        await db.execute(
            delete(PushSubscription).where(
                PushSubscription.user_id == user["id"],
                PushSubscription.endpoint == body.old_endpoint,
            )
        )
    return {"id": subscription_id}


@router.delete("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def delete_push_subscription(
    body: EndpointBody,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_user),
) -> Response:
    """Remove one of the caller's push subscriptions."""
    await db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user["id"],
            PushSubscription.endpoint == body.endpoint,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/push/test")
async def push_test(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Send a test push to every device of the caller.

    503 push_not_configured · 400 no_push_subscriptions · 502 push_unreachable
    (nothing delivered; devices the push service says are gone are removed).
    """
    if not push_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "push_not_configured")
    rows = (
        await db.execute(
            select(PushSubscription).where(PushSubscription.user_id == user["id"])
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_push_subscriptions")
    result = await send_push(
        [StoredSubscription(r.endpoint, r.p256dh, r.auth) for r in rows], push_test_message()
    )
    await delete_subscriptions(db, result.expired_endpoints)
    counts = {"delivered": result.delivered, "failed": result.failed, "expired": result.expired}
    logger.info("[push/test] %s %s", user["id"], counts)
    if result.delivered == 0:
        # Commit the removal of expired devices before answering with an error.
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "push_unreachable")
    return {"ok": True, **counts}
