"""Notification preferences, push subscriptions and the email sender."""

from .service import notify_event_change, send_email

__all__ = ["send_email", "notify_event_change"]
