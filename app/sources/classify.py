"""Deterministic category guessing for imported events (port of `classify.ts`).

Scores an event's title, tags and prose against each category's keywords and only answers
when one category clearly wins. Pure: no database, no network.
"""

import re
from typing import Any, Iterable

from app.connectors import js

CLASSIFY_DEFAULTS = {"min_score": 3, "margin": 2}

# Words too common to identify a category: they appear in almost every listing.
STOPWORDS = {
    "and", "or", "the", "of", "a", "an", "for", "in", "on", "at", "to", "with", "by", "from",
    "event", "events", "club", "group", "online", "free",
}

# A hit in the title says more than one buried in the description.
WEIGHT = {"title": 3, "tags": 2, "summary": 1, "description": 1}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(f"{js.S}+")


def normalize(value: str) -> str:
    return js.trim(_SPACES.sub(" ", _NON_ALNUM.sub(" ", value.lower())))


def contains(haystack: str, term: str) -> bool:
    """Whole-word (or whole-phrase) match: "run" must not fire on "running"."""
    return f" {term} " in f" {haystack} "


def _covers(phrase: str, term: str) -> bool:
    return (
        phrase == term
        or phrase.startswith(f"{term} ")
        or phrase.endswith(f" {term}")
        or f" {term} " in phrase
    )


def category_terms(category: dict[str, Any]) -> list[str]:
    """A category's keywords plus the usable words of its name ("Careers fair" → careers, fair)."""
    keywords = [k for k in (normalize(k) for k in (category.get("keywords") or [])) if k]
    name_words = [w for w in normalize(category.get("name") or "").split(" ") if js.u16len(w) >= 3 and w not in STOPWORDS]
    terms = list(dict.fromkeys([*keywords, *name_words]))
    return [t for t in terms if js.u16len(t) >= 3]


def _fields(event: dict[str, Any]) -> dict[str, str]:
    return {
        "title": normalize(event.get("title") or ""),
        "tags": normalize(" ".join(event.get("tags") or [])),
        "summary": normalize(event.get("summary") or ""),
        "description": normalize(event.get("description") or ""),
    }


def classify_event(
    event: dict[str, Any], categories: Iterable[dict[str, Any]], min_score: int = 3, margin: int = 2
) -> dict[str, Any]:
    """Scores every category; `category_id` is None when nothing is clear."""
    fields = _fields(event)
    ranked = []
    for category in categories:
        matched: list[str] = []
        score = 0
        terms = sorted(category_terms(category), key=lambda t: -len(t.split(" ")))
        for term in terms:
            if any(_covers(hit, term) for hit in matched):
                continue
            weight = next((WEIGHT[f] for f in ("title", "tags", "summary", "description") if contains(fields[f], term)), 0)
            if not weight:
                continue
            score += weight * (2 if " " in term else 1)
            matched.append(term)
        ranked.append({"id": str(category["id"]), "score": score, "matched": matched})
    ranked.sort(key=lambda r: (-r["score"], r["id"]))
    best = ranked[0] if ranked else None
    if best is None or best["score"] < min_score:
        return {"category_id": None, "score": best["score"] if best else 0, "matched": best["matched"] if best else [], "reason": "no-match"}
    runner_up = ranked[1] if len(ranked) > 1 else None
    if runner_up and best["score"] - runner_up["score"] < margin:
        return {"category_id": None, "score": best["score"], "matched": best["matched"], "reason": "ambiguous"}
    return {"category_id": best["id"], "score": best["score"], "matched": best["matched"], "reason": "matched"}
