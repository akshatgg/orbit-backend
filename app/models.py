"""Aggregate model imports.

Importing this module registers every ORM model on the shared DeclarativeBase so
`Base.metadata` is complete. Alembic's env.py imports Base from here.
"""

from app.db_base import Base  # noqa: F401
from app.admin.models import AdminAuditLog  # noqa: F401
from app.categories.models import Category  # noqa: F401
from app.events.models import Event, EventPrivate  # noqa: F401
from app.groups.models import (  # noqa: F401
    Group,
    GroupAnnouncement,
    GroupEvent,
    GroupEventInterest,
    GroupInvite,
    GroupMember,
)
from app.messaging.models import (  # noqa: F401
    Conversation,
    ConversationMember,
    Message,
    UserBlock,
)
from app.notifications.models import (  # noqa: F401
    Notification,
    NotificationLog,
    NotificationPreference,
    PushSubscription,
)
from app.organisers.models import (  # noqa: F401
    Organiser,
    OrganiserInvite,
    OrganiserMember,
)
from app.payments.models import StripeEvent  # noqa: F401
from app.sources.models import (  # noqa: F401
    EventDuplicateDismissal,
    EventSource,
    SourceEventDetails,
    SourceRun,
)
from app.tickets.models import EventQuestion, Registration, TicketType  # noqa: F401
from app.users.models import (  # noqa: F401
    EmailToken,
    EventInteraction,
    Follow,
    Profile,
    User,
    UserEvent,
)

__all__ = [
    "Base",
    "User",
    "EmailToken",
    "Profile",
    "UserEvent",
    "Follow",
    "EventInteraction",
    "Category",
    "Event",
    "EventPrivate",
    "Organiser",
    "OrganiserMember",
    "OrganiserInvite",
    "TicketType",
    "EventQuestion",
    "Registration",
    "StripeEvent",
    "AdminAuditLog",
    "EventSource",
    "SourceRun",
    "EventDuplicateDismissal",
    "SourceEventDetails",
    "PushSubscription",
    "NotificationPreference",
    "NotificationLog",
    "Notification",
    "Group",
    "GroupMember",
    "GroupInvite",
    "GroupEvent",
    "GroupEventInterest",
    "GroupAnnouncement",
    "Conversation",
    "ConversationMember",
    "Message",
    "UserBlock",
]
