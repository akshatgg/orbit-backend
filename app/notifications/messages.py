"""What a notification says: push title/body and the branded email.

Pure (no I/O). Ported from the frontend's `src/lib/notifications/messages.ts`
and `templates.ts`; the words, subjects and layout match it. Dates render in UK
time, whatever the server's timezone.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")

Audience = Literal["going", "unpaid", "saved"]
RegistrationUpdate = Literal[
    "approved",
    "off_waitlist",
    "confirmed",
    "paid",
    "awaiting_payment",
    "pending_review",
    "declined",
]

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

TICKETS_PATH = "/tickets"


@dataclass
class EmailContent:
    subject: str
    preheader: str
    heading: str
    paragraphs: list[str]
    details: list[tuple[str, str]]
    cta: tuple[str, str]  # (label, url)
    # Replaces the default "you saved an event" footer line (and drops the reminders link).
    footer: Optional[str] = None


@dataclass
class Message:
    title: str
    body: str
    url: str
    tag: str
    email: EmailContent
    actions: list[dict[str, str]] = field(default_factory=list)


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str


# ─── formatting (src/lib/format.ts) ─────────────────────────────────────────


def _uk(value: datetime) -> datetime:
    return value.astimezone(TZ)


def _day(value: datetime) -> str:
    """'Thu 5 Nov'"""
    d = _uk(value)
    return f"{_WEEKDAYS[d.weekday()][:3]} {d.day} {_MONTHS[d.month - 1][:3]}"


def _time(value: datetime) -> str:
    return _uk(value).strftime("%H:%M")


def format_event_date(value: datetime) -> str:
    """'Thu 5 Nov · 18:00'"""
    return f"{_day(value)} · {_time(value)}"


def format_time_range(start: datetime, end: Optional[datetime] = None) -> str:
    """'18:00 – 20:00'"""
    return f"{_time(start)} – {_time(end)}" if end else _time(start)


def format_long_date(value: datetime) -> str:
    """'Thursday, 5 November 2026'"""
    d = _uk(value)
    return f"{_WEEKDAYS[d.weekday()]}, {d.day} {_MONTHS[d.month - 1]} {d.year}"


def relative_day(starts_at: datetime, now: datetime) -> str:
    """'today', 'tomorrow' or 'on Thu 16 Oct' in UK time."""
    diff = (_uk(starts_at).date() - _uk(now).date()).days
    if diff == 0:
        return "today"
    if diff == 1:
        return "tomorrow"
    return f"on {_day(starts_at)}"


def esc(value: str) -> str:
    """Escapes text for HTML. Paragraphs may contain <b>/<i> we add ourselves."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _where(e: dict[str, Any]) -> str:
    if e.get("format") == "online":
        return "Online"
    place = ", ".join(v for v in (e.get("venue_name"), e.get("address")) if v)
    if e.get("format") == "hybrid":
        return f"{place} (or online)" if place else "Hybrid"
    return place or "Venue to be confirmed"


def _when_detail(e: dict[str, Any]) -> tuple[str, str]:
    return (
        "When",
        f"{format_long_date(e['starts_at'])} · {format_time_range(e['starts_at'], e.get('ends_at'))}",
    )


def _event_details(e: dict[str, Any]) -> list[tuple[str, str]]:
    return [_when_detail(e), ("Where", _where(e))]


def _event_path(e: dict[str, Any]) -> str:
    return f"/events/{e['id']}"


# ─── reminders (cron) ───────────────────────────────────────────────────────


def event_reminder_message(e: dict[str, Any], audience: Audience, now: datetime) -> Message:
    day = relative_day(e["starts_at"], now)
    time = format_time_range(e["starts_at"]).strip()
    when = f"{day[0].upper()}{day[1:]} at {time}"
    tag = f"reminder-{e['id']}"
    title = e["title"]

    if audience == "going":
        return Message(
            title=f"{when}: {title}",
            body=f"You're going. {_where(e)}. Have your ticket ready.",
            url=TICKETS_PATH,
            tag=tag,
            actions=[
                {"action": "ticket", "title": "My ticket", "url": TICKETS_PATH},
                {"action": "event", "title": "Event details", "url": _event_path(e)},
            ],
            email=EmailContent(
                subject=f"Reminder: {title} is {day}",
                preheader=f"{when} · {_where(e)}",
                heading=f"See you {day}!",
                paragraphs=[
                    f"You're going to <b>{esc(title)}</b>. Here are the details so you're all set."
                ],
                details=_event_details(e),
                cta=("View your ticket", TICKETS_PATH),
            ),
        )

    if audience == "unpaid":
        return Message(
            title=f"{when}: {title}",
            body="Your place isn't confirmed yet. Finish paying to secure it.",
            url=_event_path(e),
            tag=tag,
            email=EmailContent(
                subject=f"Finish booking {title} ({day})",
                preheader="Your place isn't confirmed until you pay.",
                heading="Your ticket is waiting",
                paragraphs=[
                    f"<b>{esc(title)}</b> is {day}. Complete your payment to lock in your place."
                ],
                details=_event_details(e),
                cta=("Complete payment", _event_path(e)),
            ),
        )

    can_register = e.get("registration_mode") in ("tickets", "external")
    return Message(
        title=f"{when}: {title}",
        body=(
            f"You saved this. {_where(e)}. Grab a spot if you're going."
            if can_register
            else f"You saved this. {_where(e)}."
        ),
        url=_event_path(e),
        tag=tag,
        email=EmailContent(
            subject=f"{title} is {day}",
            preheader=f"{when} · {_where(e)}",
            heading=f"Coming up {day}",
            paragraphs=[
                f"You saved <b>{esc(title)}</b>."
                + (" If you're going, make sure you've registered." if can_register else "")
            ],
            details=_event_details(e),
            cta=("View event" if can_register else "See details", _event_path(e)),
        ),
    )


def deadline_reminder_message(e: dict[str, Any], now: datetime) -> Message:
    deadline = e.get("registration_deadline") or e["starts_at"]
    hours = max(1, math.floor((deadline - now).total_seconds() / 3600 + 0.5))  # Math.round
    closes = f"in {hours} hour{'' if hours == 1 else 's'}" if hours < 24 else relative_day(deadline, now)
    closes_at = format_event_date(deadline)
    label = (
        (e.get("external_label") or "Apply now")
        if e.get("registration_mode") == "external"
        else "Register now"
    )
    title = e["title"]
    return Message(
        title=f"Registration closes {closes}",
        body=f"{title}: you saved this but haven't registered yet.",
        url=_event_path(e),
        tag=f"deadline-{e['id']}",
        actions=[{"action": "event", "title": label, "url": _event_path(e)}],
        email=EmailContent(
            subject=f"Last chance: registration for {title} closes {closes}",
            preheader=f"Closes {closes_at}. Don't miss out.",
            heading="Don't miss the deadline",
            paragraphs=[
                f"You saved <b>{esc(title)}</b> but haven't registered yet. "
                f"Registration closes <b>{esc(closes_at)}</b>."
            ],
            details=[("Deadline", closes_at), _when_detail(e)],
            cta=(label, _event_path(e)),
        ),
    )


# ─── event changed by the organiser ─────────────────────────────────────────


def event_changed_message(e: dict[str, Any], change: Literal["updated", "cancelled"]) -> Message:
    title = e["title"]
    if change == "cancelled":
        reason = (e.get("cancel_reason") or "").strip()
        return Message(
            title=f"Cancelled: {title}",
            body=(
                f'The organiser cancelled this event. "{reason}"'
                if reason
                else "The organiser cancelled this event."
            ),
            url=_event_path(e),
            tag=f"event-{e['id']}",
            email=EmailContent(
                subject=f"Cancelled: {title}",
                preheader=f"{title} on {format_event_date(e['starts_at'])} won't go ahead.",
                heading="This event has been cancelled",
                paragraphs=[
                    f"Sorry, <b>{esc(title)}</b> on {esc(format_event_date(e['starts_at']))} won't go ahead.",
                    *([f"The organiser said: <i>&ldquo;{esc(reason)}&rdquo;</i>"] if reason else []),
                    "You don't need to do anything. If you paid, the organiser will be in touch about a refund.",
                ],
                details=[],
                cta=("Find something else", "/"),
            ),
        )
    return Message(
        title=f"Event updated: {title}",
        body=f"New time/place: {format_event_date(e['starts_at'])} · {_where(e)}",
        url=_event_path(e),
        tag=f"event-{e['id']}",
        email=EmailContent(
            subject=f"Updated: {title} has a new time or place",
            preheader=f"Now {format_event_date(e['starts_at'])} · {_where(e)}",
            heading="Heads up: details changed",
            paragraphs=[f"The organiser updated <b>{esc(title)}</b>. Here's the latest:"],
            details=_event_details(e),
            cta=("View event", _event_path(e)),
        ),
    )


# ─── registration status changed ────────────────────────────────────────────


def registration_message(e: dict[str, Any], update: RegistrationUpdate, status: str) -> Message:
    tag = f"registration-{e['id']}"
    date = format_event_date(e["starts_at"])
    title = e["title"]
    t = esc(title)

    def payment() -> Message:
        heading = (
            "You've been accepted!"
            if update == "approved"
            else "A spot opened up!"
            if update == "off_waitlist"
            else "Complete your booking"
        )
        return Message(
            title=(
                f"Accepted: {title}"
                if update == "approved"
                else f"A spot opened up: {title}"
                if update == "off_waitlist"
                else f"Complete your booking: {title}"
            ),
            body="Pay now to secure your place.",
            url=_event_path(e),
            tag=tag,
            email=EmailContent(
                subject=f"{heading} Complete payment for {title}",
                preheader="Your place is held, pay to confirm it.",
                heading=heading,
                paragraphs=[
                    f"Your place at <b>{t}</b> is waiting. Complete your payment to confirm it."
                ],
                details=_event_details(e),
                cta=("Complete payment", _event_path(e)),
            ),
        )

    if status == "awaiting_payment" or update == "awaiting_payment":
        return payment()

    if update in ("approved", "off_waitlist", "confirmed", "paid"):
        heading = {
            "approved": "You're in!",
            "off_waitlist": "A spot opened up. You're in!",
            "paid": "Payment received. You're in!",
        }.get(update, "You're going!")
        why = (
            f"The organiser accepted you for <b>{t}</b>."
            if update == "approved"
            else f"You've moved off the waitlist for <b>{t}</b>."
            if update == "off_waitlist"
            else f"Your place at <b>{t}</b> is confirmed."
        )
        return Message(
            title=f"{heading} {title}",
            body=f"{date}. Your ticket is ready in ORBIT.",
            url=TICKETS_PATH,
            tag=tag,
            actions=[{"action": "ticket", "title": "View ticket", "url": TICKETS_PATH}],
            email=EmailContent(
                subject=f"{heading} {title}",
                preheader=f"{date} · your QR ticket is in ORBIT",
                heading=heading,
                paragraphs=[f"{why} Show the QR code on your ticket at the door."],
                details=_event_details(e),
                cta=("View your ticket", TICKETS_PATH),
            ),
        )

    if update == "pending_review":
        return Message(
            title=f"Application received: {title}",
            body="The organiser will review it. We'll let you know.",
            url=_event_path(e),
            tag=tag,
            email=EmailContent(
                subject=f"Your application for {title} is with the organiser",
                preheader="We'll tell you as soon as they decide.",
                heading="You're in the queue",
                paragraphs=[
                    f"Your application for <b>{t}</b> is with the organiser. "
                    "We'll let you know as soon as they decide."
                ],
                details=_event_details(e),
                cta=("View event", _event_path(e)),
            ),
        )

    # declined
    return Message(
        title=f"Update on {title}",
        body="The organiser couldn't offer you a place this time.",
        url="/",
        tag=tag,
        email=EmailContent(
            subject=f"Update on your registration for {title}",
            preheader="There are plenty more events to explore.",
            heading="Not this time",
            paragraphs=[
                f"Unfortunately the organiser couldn't offer you a place at <b>{t}</b>.",
                "There's plenty more coming up. Here are events picked for you.",
            ],
            details=[],
            cta=("Explore events", "/"),
        ),
    )


def push_test_message() -> Message:
    return Message(
        title="ORBIT notifications are on",
        body="You'll hear from us before events you've saved or booked.",
        url="/profile",
        tag="orbit-test",
        email=EmailContent(
            subject="ORBIT notifications are on",
            preheader="This is a test.",
            heading="You're all set",
            paragraphs=["This is a test notification from ORBIT."],
            details=[],
            cta=("Open ORBIT", "/"),
        ),
    )


# ─── organisation decisions (admin approves / rejects) ──────────────────────


def organiser_decision_message(org: dict[str, Any], decision: Literal["approved", "rejected"]) -> Message:
    name = org["name"]
    footer = "You're getting this because you applied to host events on ORBIT."
    if decision == "approved":
        return Message(
            title="You're approved — start hosting on ORBIT",
            body=f"{name} is live. Create your first event in Studio.",
            url="/organiser",
            tag="organiser-decision",
            email=EmailContent(
                subject="You're approved — start hosting on ORBIT",
                preheader=f"{name} can now create and publish events.",
                heading="You're approved",
                paragraphs=[
                    f"<b>{esc(name)}</b> has been approved. You can now create events, "
                    "take registrations and check guests in.",
                    "You can also invite teammates from Studio → Settings → Team.",
                ],
                details=[],
                cta=("Open Studio", "/organiser"),
                footer=footer,
            ),
        )
    note = (org.get("review_note") or "").strip()
    return Message(
        title="Update on your ORBIT organisation",
        body=f"{name} wasn't approved: {note}" if note else f"{name} wasn't approved this time.",
        url="/organiser",
        tag="organiser-decision",
        email=EmailContent(
            subject="Update on your ORBIT organisation",
            preheader=f"{name} wasn't approved this time.",
            heading="Your organisation wasn't approved",
            paragraphs=[
                f"We couldn't approve <b>{esc(name)}</b> this time.",
                *([f"<b>Note from the ORBIT team:</b> {esc(note)}"] if note else []),
                "You're welcome to update your details and apply again.",
            ],
            details=[],
            cta=("Open Studio", "/organiser"),
            footer=footer,
        ),
    )


# ─── circles & communities (in-app inbox companions) ────────────────────────


def group_invite_message(group: dict[str, Any], inviter: str, path: str) -> Message:
    """Someone invited you to a Circle or a Community."""
    what = "circle" if group["kind"] == "circle" else "community"
    name = group["name"]
    title = f"{inviter} invited you to {name}"
    return Message(
        title=title,
        body=f"Join the {what} on ORBIT to see the events they share.",
        url=path,
        tag=f"group-invite-{group['id']}",
        email=EmailContent(
            subject=title,
            preheader=f"You're invited to the {what} {name} on ORBIT.",
            heading="You're invited",
            paragraphs=[
                f"<b>{esc(inviter)}</b> invited you to join the {what} <b>{esc(name)}</b> on ORBIT.",
                "Accept to see the events its members share and who's going.",
            ],
            details=[],
            cta=("See the invite", path),
            footer="You're getting this because someone invited you to a group on ORBIT.",
        ),
    )


def direct_message_push(sender: str, body: str, path: str, conversation_id: str) -> Message:
    """A new direct message. Push only: messages never go by email."""
    preview = body if len(body) <= 120 else body[:117].rstrip() + "..."
    return Message(
        title=sender,
        body=preview,
        url=path,
        tag=f"dm-{conversation_id}",
        email=EmailContent(
            subject=f"New message from {sender}",
            preheader=preview,
            heading=f"New message from {sender}",
            paragraphs=[esc(preview)],
            details=[],
            cta=("Open ORBIT", path),
        ),
    )


# ─── push payload and email rendering (templates.ts) ────────────────────────


def push_payload(message: Message) -> dict[str, Any]:
    """The JSON the service worker's push handler reads."""
    return {
        "title": message.title,
        "body": message.body,
        "url": message.url,
        "tag": message.tag,
        "actions": message.actions,
    }


# Brand tokens (mirror globals.css; email clients can't read CSS variables).
_BG = "#05040c"
_SURFACE = "#0e0d1a"
_SURFACE2 = "#161428"
_LINE = "#24213b"
_FG = "#ffffff"
_MUTED = "#a9a3c4"
_VIOLET = "#b57cf6"
_BLUE = "#3f79fd"
_GRADIENT = "linear-gradient(135deg,#de9ffb 0%,#3f79fd 35%,#f4e4d4 70%,#fdb570 100%)"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,Helvetica,Arial,sans-serif"


def absolute_url(path: str, app_url: str) -> str:
    """Absolute URL for an app path (emails need full links)."""
    if re.match(r"^https?://", path, re.IGNORECASE):
        return path
    return f"{app_url.rstrip('/')}{'' if path.startswith('/') else '/'}{path}"


def html_to_text(html: str) -> str:
    """HTML fragment → plain text (for the text/plain part)."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (
        ("&ldquo;", "“"),
        ("&rdquo;", "”"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&amp;", "&"),
    ):
        text = text.replace(entity, char)
    return text


def render_email(message: Message, app_url: str) -> RenderedEmail:
    m = message.email
    cta_label, cta_path = m.cta
    cta_url = absolute_url(cta_path, app_url)
    settings_url = absolute_url("/profile", app_url)

    footer_html = (
        esc(m.footer)
        if m.footer
        else "You're getting this because of an event you saved or registered for on ORBIT.\n"
        f'          <a href="{esc(settings_url)}" style="color:{_VIOLET};">Manage reminders</a>'
    )
    footer_text = m.footer or (
        "You're getting this because of an event you saved or registered for on ORBIT. "
        f"Manage reminders: {settings_url}"
    )

    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:{_MUTED};">{p}</p>'
        for p in m.paragraphs
    )

    details = ""
    if m.details:
        rows = "".join(
            f'<tr><td style="padding:12px 16px;{f"border-top:1px solid {_LINE};" if i else ""}">\n'
            f'              <div style="font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:{_VIOLET};">{esc(label)}</div>\n'
            f'              <div style="margin-top:3px;font-size:14px;line-height:1.5;color:{_FG};">{esc(value)}</div>\n'
            "            </td></tr>"
            for i, (label, value) in enumerate(m.details)
        )
        details = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:6px 0 22px;background:{_SURFACE2};border:1px solid {_LINE};border-radius:14px;">\n'
            f"        {rows}\n"
            "      </table>"
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{esc(m.subject)}</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:{_FONT};color:{_FG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(m.preheader)}&#8203;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};">
  <tr><td align="center" style="padding:28px 14px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:{_SURFACE};border:1px solid {_LINE};border-radius:20px;overflow:hidden;">
      <tr><td align="center" bgcolor="{_BLUE}" style="background:{_BLUE};background-image:{_GRADIENT};padding:26px 20px;">
        <div style="font-size:22px;font-weight:800;letter-spacing:0.42em;color:{_BG};padding-left:0.42em;">ORBIT</div>
        <div style="margin-top:4px;font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:{_BG};opacity:0.75;">Explore opportunities</div>
      </td></tr>
      <tr><td style="padding:28px 24px 8px;">
        <h1 style="margin:0 0 14px;font-size:22px;line-height:1.3;font-weight:700;color:{_FG};">{esc(m.heading)}</h1>
        {paragraphs}
        {details}
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 24px;">
          <tr><td align="center" bgcolor="{_VIOLET}" style="border-radius:12px;background:{_VIOLET};background-image:{_GRADIENT};">
            <a href="{esc(cta_url)}" style="display:inline-block;padding:13px 26px;font-size:15px;font-weight:700;color:{_BG};text-decoration:none;border-radius:12px;">{esc(cta_label)}</a>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:16px 24px 24px;border-top:1px solid {_LINE};">
        <p style="margin:0;font-size:12px;line-height:1.6;color:{_MUTED};">
          {footer_html}
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    text = "\n".join(
        [
            "ORBIT",
            "",
            m.heading,
            "",
            *(html_to_text(p) for p in m.paragraphs),
            *(["", *(f"{label}: {value}" for label, value in m.details)] if m.details else []),
            "",
            f"{cta_label}: {cta_url}",
            "",
            "--",
            footer_text,
        ]
    )
    return RenderedEmail(subject=m.subject, html=html, text=text)
