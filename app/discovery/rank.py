"""Transparent, rule-based recommendations (port of the frontend's `src/lib/ranking/index.ts`).

Every event gets a score made of named signals, each with a weight and a short reason, so a feed
can say why something was recommended. Pure apart from `load_viewer`; the clock is passed in.
Event inputs are the card dicts `app.events.service.card` builds (with `counts`).
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.discovery.taxonomy import CAREER_INTERESTS, PERSONAL_INTERESTS, TAG_SYNONYMS
from app.tickets.models import Registration
from app.users.models import Follow, Profile, UserEvent

WEIGHTS = {
    "careerInterest": 30,
    "careerInterestExtra": 8,
    "careerInterestMax": 40,
    "followedOrganiser": 25,
    "personalInterest": 15,
    "personalInterestExtra": 5,
    "personalInterestMax": 20,
    "preferredCategory": 12,
    "courseMatch": 10,
    "deadlineSoon": 10,
    "availability": 8,
    "sameUniversity": 6,
    "goalFit": 5,
    "popularityMax": 6,
    "preferredFormat": 5,
    "featured": 5,
    "startingSoon": 4,
    "fullWithWaitlist": -15,
    "fullNoWaitlist": -20,
    "registrationClosed": -25,
}

POPULARITY_CAP = 50
POPULARITY_REASON_MIN = 10
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# Registrations that already put the event in the user's Tickets tab.
ACTIVE_REGISTRATION = ("going", "pending", "waitlisted", "awaiting_payment")

CAREER_PHRASE = {
    "consulting": "consulting",
    "finance": "finance",
    "technology": "tech",
    "marketing": "marketing",
    "entrepreneurship": "entrepreneurship",
    "product": "product management",
    "data": "data & analytics",
    "sustainability": "sustainability",
    "healthcare": "healthcare",
    "public-sector": "policy",
    "law": "law",
    "creative": "the creative industries",
    "ai": "AI",
    "crypto": "crypto & web3",
    "careers": "career growth",
    "science": "science & research",
}

PERSONAL_PHRASE = {
    "socialising": "socialising",
    "fitness": "fitness",
    "travel": "travel",
    "sustainability": "sustainability",
    "arts": "arts & culture",
    "music": "music",
    "food": "food & drink",
    "wellbeing": "wellbeing",
    "volunteering": "volunteering",
    "gaming": "gaming",
    "books": "books & writing",
    "growth": "personal growth",
    "hobbies": "hobbies & crafts",
}

# Category slugs that imply an interest even when the organiser didn't tag it.
CATEGORY_TOPICS = {
    "careers-fair": ["careers"],
    "employer-presentation": ["careers"],
    "fitness": ["fitness"],
    "social": ["socialising"],
    "networking": ["socialising"],
    "arts-culture": ["arts"],
    "sustainability": ["sustainability"],
    "entrepreneurship": ["entrepreneurship"],
}

FORMAT_PHRASE = {"in_person": "it's in person", "online": "it's online", "hybrid": "it's hybrid"}

WEEKDAY_NAMES = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_PART_PLURAL = {"morning": "mornings", "day": "afternoons", "evening": "evenings"}
# Parts of the day used for availability: UK local hours [start, end).
DAY_PARTS = [("morning", 6, 12), ("day", 12, 17), ("evening", 17, 24)]

NETWORKING_TAGS = {"networking", "network", "mixer", "meetup", "meetups"}
# Too generic to recommend something on their own ("it's in person").
WEAK_REASONS = {"preferredFormat", "startingSoon"}

USER_TYPES = ("student", "graduate", "professional", "other")

UK = ZoneInfo("Europe/London")

_CAREER_VALUES = {value for value, _ in CAREER_INTERESTS}
_PERSONAL_VALUES = {value for value, _ in PERSONAL_INTERESTS}

# JavaScript's String.prototype.trim whitespace set.
_JS_SPACE = "\t\n\v\f\r                  　﻿"
_UNDERSCORES = re.compile(r"_+")
_SPACES = re.compile(f"[{_JS_SPACE}]+")


def _js_round(value: float) -> int:
    """`Math.round`: halves go up."""
    return math.floor(value + 0.5)


def _round2(value: float) -> float:
    return _js_round(value * 100) / 100


def norm(value: str) -> str:
    return _SPACES.sub(" ", _UNDERSCORES.sub("-", value.strip(_JS_SPACE).lower()))


def _join_pair(items: list[str]) -> str:
    return " & ".join(items[:2])


# ─── Viewer ─────────────────────────────────────────────────────────────────


@dataclass
class Viewer:
    """Who is looking: the ranking fields of their profile, follows and event state."""

    profile: dict[str, Any] = field(default_factory=dict)
    followed: set[UUID] = field(default_factory=set)
    dismissed: set[UUID] = field(default_factory=set)
    external_registered: set[UUID] = field(default_factory=set)
    registrations: dict[UUID, str] = field(default_factory=dict)


PROFILE_FIELDS = (
    "career_interests", "personal_interests", "preferred_categories", "preferred_formats",
    "availability", "university", "department", "course", "goals", "user_type",
)


async def load_viewer(db: AsyncSession, user: dict | None, *, state: bool = True) -> Viewer:
    """The viewer for a signed-in user, or an anonymous one (no profile, follows or state)."""
    if not user:
        return Viewer()
    uid = user["id"]
    profile = await db.get(Profile, uid)
    viewer = Viewer(
        profile={name: getattr(profile, name) for name in PROFILE_FIELDS} if profile else {},
        followed=set((await db.scalars(select(Follow.organiser_id).where(Follow.user_id == uid))).all()),
    )
    if state:
        for event_id, kind in (
            await db.execute(select(UserEvent.event_id, UserEvent.kind).where(UserEvent.user_id == uid))
        ).all():
            if kind == "dismissed":
                viewer.dismissed.add(event_id)
            elif kind == "external_registered":
                viewer.external_registered.add(event_id)
        viewer.registrations = dict(
            (
                await db.execute(
                    select(Registration.event_id, Registration.status).where(Registration.user_id == uid)
                )
            ).all()
        )
    return viewer


# ─── Signals ────────────────────────────────────────────────────────────────


def event_topics(event: dict) -> set[str]:
    """Normalised topics an event is about: its tags (+ synonyms) and what its category implies."""
    topics: set[str] = set()
    for raw in event.get("tags") or []:
        tag = norm(raw)
        if not tag:
            continue
        topics.add(tag)
        topics.add(tag.replace(" ", "-"))
        synonym = TAG_SYNONYMS.get(tag)
        if synonym:
            topics.add(synonym)
    slug = (event.get("category") or {}).get("slug")
    if slug:
        topics.update(CATEGORY_TOPICS.get(slug, []))
    return topics


def goal_fit(event: dict, goals: list[str] | None) -> str | None:
    if not goals:
        return None
    category = event.get("category") or {}
    group = category.get("group")
    slug = category.get("slug")
    networking = slug == "networking" or any(norm(tag) in NETWORKING_TAGS for tag in event.get("tags") or [])
    if "network" in goals and networking:
        return "it's a chance to grow your network"
    if "career" in goals and group == "career":
        return "it fits your career goals"
    if "social" in goals and group == "social":
        return "it's a good way to meet people"
    return None


def uk_weekday_hour(moment: datetime) -> tuple[str, int]:
    local = moment.astimezone(UK)
    return WEEKDAY_KEYS[local.weekday()], local.hour


def day_part_for(hour: int) -> str | None:
    return next((name for name, start, end in DAY_PARTS if start <= hour < end), None)


def parse_availability(raw: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if not isinstance(raw, dict):
        return out
    for day, parts in raw.items():
        if not isinstance(parts, list):
            continue
        values = {part for part in parts if isinstance(part, str)}
        if values:
            out[str(day)[:3].lower()] = values
    return out


def effective_user_type(profile: dict) -> str:
    if profile.get("user_type") in USER_TYPES:
        return profile["user_type"]
    return "student" if (profile.get("university") or "").strip(_JS_SPACE) else "other"


def _deadline_reason(left: timedelta) -> str:
    if left < HOUR * 1.5:
        return "registration closes within the hour"
    if left < DAY:
        return f"registration closes in {_js_round(left / HOUR)} hours"
    days = _js_round(left / DAY)
    return f"registration closes in {days} day{'' if days == 1 else 's'}"


def _is_full(event: dict) -> bool:
    counts = event.get("counts")
    if event.get("registration_mode") != "tickets" or event.get("capacity") is None or not counts:
        return False
    return event["capacity"] - counts.get("going", 0) - counts.get("pending", 0) <= 0


def _signal(key: str, weight: float, reason: str | None) -> dict:
    return {"key": key, "weight": weight, "reason": reason}


def score_signals(event: dict, profile: dict, followed: set, now: datetime) -> list[dict]:
    """All matching signals for one event, strongest first."""
    signals: list[dict] = []
    starts = event["starts_at"]
    topics = event_topics(event)

    career = [c for c in profile.get("career_interests") or [] if c in _CAREER_VALUES or c in CAREER_PHRASE]
    career_hits = [c for c in career if c in topics]
    if career_hits:
        weight = min(
            WEIGHTS["careerInterestMax"],
            WEIGHTS["careerInterest"] + (len(career_hits) - 1) * WEIGHTS["careerInterestExtra"],
        )
        phrases = [CAREER_PHRASE.get(c, c.replace("-", " ")) for c in career_hits[:2]]
        signals.append(_signal("careerInterest", weight, f"you're into {_join_pair(phrases)}"))

    organiser_id = event.get("organiser_id")
    if organiser_id and organiser_id in followed:
        organiser = event.get("organiser")
        signals.append(
            _signal(
                "followedOrganiser",
                WEIGHTS["followedOrganiser"],
                f"you follow {organiser['name']}" if organiser else "you follow the organiser",
            )
        )

    personal_hits = [
        p
        for p in profile.get("personal_interests") or []
        if (p in _PERSONAL_VALUES or p in PERSONAL_PHRASE) and p not in career_hits and p in topics
    ]
    if personal_hits:
        weight = min(
            WEIGHTS["personalInterestMax"],
            WEIGHTS["personalInterest"] + (len(personal_hits) - 1) * WEIGHTS["personalInterestExtra"],
        )
        phrases = [PERSONAL_PHRASE.get(p, p.replace("-", " ")) for p in personal_hits[:2]]
        signals.append(_signal("personalInterest", weight, f"you enjoy {_join_pair(phrases)}"))

    category = event.get("category")
    if category and category.get("slug") in (profile.get("preferred_categories") or []):
        signals.append(
            _signal("preferredCategory", WEIGHTS["preferredCategory"], f"you like {category['name'].lower()} events")
        )

    # Only students and graduates have a university and a course.
    academic = effective_user_type(profile) in ("student", "graduate")

    if academic:
        course_hit = next(
            (
                value
                for value in (profile.get("course"), profile.get("department"))
                if value and len(value.strip(_JS_SPACE)) > 2 and norm(value) in topics
            ),
            None,
        )
        if course_hit:
            signals.append(_signal("courseMatch", WEIGHTS["courseMatch"], f"it's aimed at {course_hit} students"))

    deadline = event.get("registration_deadline")
    if deadline and event.get("registration_mode") != "none":
        left = deadline - now
        if left <= timedelta(0):
            signals.append(_signal("registrationClosed", WEIGHTS["registrationClosed"], None))
        elif left <= 3 * DAY:
            signals.append(_signal("deadlineSoon", WEIGHTS["deadlineSoon"], _deadline_reason(left)))

    availability = parse_availability(profile.get("availability"))
    if availability:
        weekday, hour = uk_weekday_hour(starts)
        part = day_part_for(hour)
        if part and part in availability.get(weekday, set()):
            signals.append(
                _signal(
                    "availability",
                    WEIGHTS["availability"],
                    f"you're free on {WEEKDAY_NAMES.get(weekday, weekday)} {DAY_PART_PLURAL.get(part, part)}",
                )
            )

    university = event.get("university")
    if academic and university and profile.get("university") and norm(university) == norm(profile["university"]):
        signals.append(_signal("sameUniversity", WEIGHTS["sameUniversity"], f"it's for {university} students"))

    goal_reason = goal_fit(event, profile.get("goals"))
    if goal_reason:
        signals.append(_signal("goalFit", WEIGHTS["goalFit"], goal_reason))

    going = (event.get("counts") or {}).get("going", 0)
    if going > 0:
        weight = _js_round(min(going, POPULARITY_CAP) / POPULARITY_CAP * WEIGHTS["popularityMax"] * 100) / 100
        signals.append(
            _signal("popularity", weight, f"{going} people are going" if going >= POPULARITY_REASON_MIN else None)
        )

    formats = profile.get("preferred_formats") or []
    if formats:
        fmt = event.get("format")
        fits = fmt in formats or (fmt == "hybrid" and ("in_person" in formats or "online" in formats))
        if fits:
            signals.append(_signal("preferredFormat", WEIGHTS["preferredFormat"], FORMAT_PHRASE.get(fmt)))

    if event.get("is_featured"):
        signals.append(_signal("featured", WEIGHTS["featured"], "it's featured on ORBIT"))

    until = starts - now
    if timedelta(0) < until <= 2 * DAY:
        signals.append(
            _signal(
                "startingSoon",
                WEIGHTS["startingSoon"],
                "it's happening very soon" if until <= 12 * HOUR else "it's coming up in the next two days",
            )
        )

    if _is_full(event):
        waitlist = event.get("waitlist_enabled")
        waitlist = True if waitlist is None else waitlist
        signals.append(
            _signal("full", WEIGHTS["fullWithWaitlist"] if waitlist else WEIGHTS["fullNoWaitlist"], None)
        )

    return sorted(signals, key=lambda s: -s["weight"])


def build_reason(signals: list[dict]) -> str | None:
    """"Recommended because A and B" from the two strongest positive reasons."""
    reasons = [
        s["reason"]
        for s in sorted(
            (s for s in signals if s["weight"] > 0 and s["reason"] and s["key"] not in WEAK_REASONS),
            key=lambda s: -s["weight"],
        )[:2]
    ]
    return f"Recommended because {' and '.join(reasons)}" if reasons else None


def exclusion_for(event: dict, viewer: Viewer, now: datetime) -> str | None:
    """Why an event should not be recommended at all, or None."""
    if event.get("status") != "published":
        return "unavailable"
    end = event.get("ends_at") or event["starts_at"]
    if end < now:
        return "ended"
    event_id = event["id"]
    if event_id in viewer.dismissed:
        return "dismissed"
    if viewer.registrations.get(event_id) in ACTIVE_REGISTRATION:
        return "registered"
    if event_id in viewer.external_registered:
        return "registered"
    return None


def _order(item: dict) -> tuple:
    """Highest score first; ties go to the event that starts sooner, then by id."""
    return (-item["score"], item["starts_at"], str(item["id"]))


def rank_events(events: list[dict], viewer: Viewer, now: datetime, *, keep_personal: bool = False) -> list[dict]:
    """Scores, filters and sorts events for one viewer; each card gains `score` and `reason`.

    `keep_personal` keeps events the viewer already has (registered / dismissed), as search does.
    Unpublished and finished events are always dropped.
    """
    kept: list[dict] = []
    for event in events:
        signals = score_signals(event, viewer.profile, viewer.followed, now)
        excluded = exclusion_for(event, viewer, now)
        if excluded and not (keep_personal and excluded in ("dismissed", "registered")):
            continue
        event["score"] = _round2(sum(s["weight"] for s in signals))
        event["reason"] = build_reason(signals)
        kept.append(event)
    return sorted(kept, key=_order)


def _by_start(item: dict) -> tuple:
    return (item["starts_at"], str(item["id"]))


def sort_results(ranked: list[dict], sort: str, now: datetime) -> list[dict]:
    """`relevance` keeps the ranking; `date` is soonest first; `deadline` puts open deadlines first."""
    if sort == "relevance":
        return ranked
    if sort == "deadline":
        def open_deadline(item: dict) -> tuple:
            deadline = item.get("registration_deadline")
            is_open = deadline is not None and deadline > now
            return (0 if is_open else 1, deadline if is_open else now, *_by_start(item))

        return sorted(ranked, key=open_deadline)
    return sorted(ranked, key=_by_start)
