"""Notification preferences and push subscriptions for the signed-in user."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.notifications.models import NotificationPreference, PushSubscription
from app.notifications.schemas import (
    EndpointBody,
    NotificationPreferencesUpdate,
    PushSubscriptionBody,
)
from app.notifications.service import DEFAULT_PREFERENCES, send_email
from app.sql import functions as fn

router = APIRouter(tags=["notifications"])


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
    """Store this browser's push subscription for the frontend service worker."""
    subscription_id = await fn.save_push_subscription(
        db, body.endpoint, body.keys.p256dh, body.keys.auth, body.user_agent
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
    user: dict = Depends(require_user),
) -> dict[str, int]:
    """Email the caller a test message; push subscriptions are stored for the service worker, delivery isn't implemented yet."""
    to = user.get("email")
    if not to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your account has no email address")
    html = (
        "<p>This is a test from Orbit.</p>"
        "<p>Your notification settings are working. Push delivery will arrive through "
        "your browser's service worker.</p>"
    )
    sent = 1 if await send_email(to, "Orbit test notification", html) else 0
    return {"sent": sent}
