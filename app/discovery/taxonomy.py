"""Interest options and tag synonyms (port of the frontend's `src/lib/taxonomy.ts`).

Profiles store these values in `career_interests` / `personal_interests`; events are tagged and
filtered by them.
"""

CAREER_INTERESTS: list[tuple[str, str]] = [
    ("consulting", "Consulting"),
    ("finance", "Finance & banking"),
    ("technology", "Technology"),
    ("marketing", "Marketing"),
    ("entrepreneurship", "Entrepreneurship"),
    ("product", "Product management"),
    ("data", "Data & analytics"),
    ("sustainability", "Sustainability & ESG"),
    ("healthcare", "Healthcare & life sciences"),
    ("public-sector", "Public sector & policy"),
    ("law", "Law"),
    ("creative", "Creative industries"),
    ("ai", "AI"),
    ("crypto", "Crypto & Web3"),
    ("careers", "Career growth"),
    ("science", "Science & research"),
]

PERSONAL_INTERESTS: list[tuple[str, str]] = [
    ("socialising", "Socialising"),
    ("fitness", "Fitness & sport"),
    ("travel", "Travel"),
    ("sustainability", "Sustainability"),
    ("arts", "Arts & culture"),
    ("music", "Music & nightlife"),
    ("food", "Food & drink"),
    ("wellbeing", "Wellbeing"),
    ("volunteering", "Volunteering"),
    ("gaming", "Gaming"),
    ("books", "Books & writing"),
    ("growth", "Personal growth"),
    ("hobbies", "Hobbies & crafts"),
]


def _unique(options: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for value, label in options:
        if value not in seen:
            seen.add(value)
            out.append((value, label))
    return out


# Every interest once (sustainability is in both lists, with its career label).
ALL_INTERESTS: list[tuple[str, str]] = _unique([*CAREER_INTERESTS, *PERSONAL_INTERESTS])

# Free-text tags that mean the same thing as an interest value.
TAG_SYNONYMS: dict[str, str] = {
    "tech": "technology",
    "artificial intelligence": "ai",
    "machine learning": "ai",
    "web3": "crypto",
    "blockchain": "crypto",
    "career": "careers",
    "jobs": "careers",
    "reading": "books",
    "book club": "books",
    "writing": "books",
    "startups": "entrepreneurship",
    "startup": "entrepreneurship",
    "banking": "finance",
    "investment banking": "finance",
    "esg": "sustainability",
    "net zero": "sustainability",
    "analytics": "data",
    "data science": "data",
    "product management": "product",
    "policy": "public-sector",
    "running": "fitness",
    "sport": "fitness",
    "sports": "fitness",
    "social": "socialising",
    "art": "arts",
    "arts & culture": "arts",
    "culture": "arts",
    "food & drink": "food",
    "nightlife": "music",
}


def interest_tags(value: str) -> list[str]:
    """The tags that count as an interest: its own value plus every synonym of it."""
    return [value, *(tag for tag, target in TAG_SYNONYMS.items() if target == value)]
