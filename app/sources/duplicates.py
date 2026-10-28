"""Duplicate detection across all sources: same start (±30 min) and a similar title.

Port of `duplicates.ts`. The highest-ranked event (Orbit-native first, then the oldest)
is kept and the others point at it; an exact match (same page or platform id, or the same
title, start and place) is hidden without asking an admin.
"""

import re
import unicodedata
from datetime import datetime
from typing import Any

from app.connectors import js

STOPWORDS = {"the", "a", "an", "and", "of", "for", "with", "at", "in", "on", "to", "by", "x", "vs"}

DUPLICATE_WINDOW_MINUTES = 30
EXACT_WINDOW_HOURS = 6
JACCARD_THRESHOLD = 0.6
SAME_START_MS = 60 * 1000  # starts this close count as the same start

_COMBINING = re.compile("[̀-ͯ]")
_SPACES = re.compile(f"{js.S}+")


def _letters_and_digits(value: str) -> str:
    """`.replace(/[^\\p{L}\\p{N}]+/gu, " ")`."""
    out: list[str] = []
    gap = False
    for ch in value:
        if unicodedata.category(ch)[0] in ("L", "N"):
            if gap:
                out.append(" ")
                gap = False
            out.append(ch)
        else:
            gap = True
    if gap:
        out.append(" ")
    return "".join(out)


def normalise_title(title: str) -> str:
    """Lowercase, no accents or punctuation, "&" → "and", single spaces."""
    text = _COMBINING.sub("", unicodedata.normalize("NFKD", title)).lower().replace("&", " and ")
    return _SPACES.sub(" ", js.trim(_letters_and_digits(text)))


def title_tokens(title: str) -> set[str]:
    return {t for t in normalise_title(title).split(" ") if t and t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    shared = len(a & b)
    return shared / (len(a) + len(b) - shared)


def titles_similar(a: str, b: str) -> bool:
    """Token Jaccard ≥ 0.6, or one normalised title contains the other as whole words."""
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if js.u16len(na) <= js.u16len(nb) else (nb, na)
    if js.u16len(short) >= 8 and f" {short} " in f" {long} ":
        return True
    return jaccard(title_tokens(a), title_tokens(b)) >= JACCARD_THRESHOLD


def is_orbit_native(source: str) -> bool:
    """Events created in Orbit (by organisers or admins) are the originals."""
    return source in ("orbit", "manual")


def source_rank(source: str) -> int:
    """Lower wins: Orbit-native, then manual, then CSV, then automatic sources."""
    return {"orbit": 0, "manual": 1, "csv": 2}.get(source, 3)


def pair_key(a: str, b: str) -> str:
    a, b = str(a), str(b)
    return f"{a}|{b}" if a < b else f"{b}|{a}"


def _ms(value: Any) -> int:
    moment = js.to_datetime(value)
    return js.to_ms(moment) if moment is not None else 0


def _same_identity(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (bool(a.get("external_url")) and a.get("external_url") == b.get("external_url")) or (
        bool(a.get("source_event_id")) and a.get("source_event_id") == b.get("source_event_id")
    )


def _place_key(value: str | None) -> str:
    return normalise_title(value) if value else ""


def certain_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same title, same start and same place (both online, or the same venue or address)."""
    title = normalise_title(a["title"])
    if not title or title != normalise_title(b["title"]):
        return False
    if abs(_ms(a["starts_at"]) - _ms(b["starts_at"])) > SAME_START_MS:
        return False
    if a.get("format") == "online" and b.get("format") == "online":
        return True
    venue = _place_key(a.get("venue_name"))
    address = _place_key(a.get("address"))
    return (bool(venue) and venue == _place_key(b.get("venue_name"))) or (
        bool(address) and address == _place_key(b.get("address"))
    )


def _created_key(value: Any) -> Any:
    moment = value if isinstance(value, datetime) else js.to_datetime(value)
    return moment.timestamp() if moment is not None else 0.0


def find_duplicates(
    events: list[dict[str, Any]], dismissed: set[str] | None = None, window_minutes: int = DUPLICATE_WINDOW_MINUTES
) -> list[dict[str, Any]]:
    """Which events are duplicates of which: [{"id", "duplicate_of", "exact"}]."""
    dismissed = dismissed or set()
    window_ms = window_minutes * 60 * 1000
    exact_window_ms = EXACT_WINDOW_HOURS * 60 * 60 * 1000
    ordered = sorted(events, key=lambda e: (source_rank(e["source"]), _created_key(e["created_at"]), str(e["id"])))
    kept: list[tuple[dict[str, Any], int]] = []
    result = []
    for event in ordered:
        ms = _ms(event["starts_at"])

        def allowed(k: dict[str, Any], event: dict[str, Any] = event) -> bool:
            return not (is_orbit_native(k["source"]) and is_orbit_native(event["source"])) and (
                pair_key(k["id"], event["id"]) not in dismissed
            )

        # Same URL/id only auto-hides when the times are close; a weekly series reusing a page
        # must not collapse all its occurrences.
        exact = next(
            (k for k, k_ms in kept if allowed(k) and abs(k_ms - ms) <= exact_window_ms and _same_identity(k, event)),
            None,
        )
        if exact is not None:
            result.append({"id": event["id"], "duplicate_of": exact["id"], "exact": True})
            continue
        possible = next(
            (k for k, k_ms in kept if allowed(k) and abs(k_ms - ms) <= window_ms and titles_similar(k["title"], event["title"])),
            None,
        )
        if possible is not None:
            result.append({"id": event["id"], "duplicate_of": possible["id"], "exact": certain_duplicate(possible, event)})
        else:
            kept.append((event, ms))
    return result
