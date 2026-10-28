"""Tags imported events with the interests people pick in onboarding (port of `interests.ts`).

Pure keyword scoring, no model: the event's own words decide most tags, and what the
platform says about the event (its category, a group's topics) adds weighted evidence.
The interest values are the frontend taxonomy's (`app/discovery/taxonomy.py`, a port of
`src/lib/taxonomy.ts`): production tags already use them.
"""

from typing import Any, Iterable

from app.connectors.base import HINT_WEIGHT
from app.discovery import taxonomy

from .classify import contains, normalize

# The interest values, career then personal, each once: the taxonomy's own list.
ALL_INTERESTS: tuple[str, ...] = tuple(value for value, _ in taxonomy.ALL_INTERESTS)
_VALUES = set(ALL_INTERESTS)

# Words and phrases (already normalised) that signal each interest.
INTEREST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "consulting": (
        "consulting", "consultant", "consultants", "consultancy", "case interview", "case competition",
    ),
    "finance": (
        "finance", "fintech", "banking", "investment", "investing", "investor", "investors", "trading",
        "traders", "stock market", "stocks", "forex", "crypto", "bitcoin", "tradfi", "defi", "accounting",
        "venture capital", "wealth", "financial",
    ),
    "technology": (
        "tech", "technology", "software", "developer", "developers", "coding", "programming", "engineering",
        "engineers", "ai", "artificial intelligence", "machine learning", "llm", "llms", "genai", "chatgpt",
        "claude", "agentic", "ai agents", "multi agent", "web3", "blockchain", "cloud", "devops",
        "cybersecurity", "hackathon", "javascript", "python",
    ),
    "marketing": (
        "marketing", "marketers", "branding", "seo", "advertising", "social media marketing",
        "content marketing",
    ),
    "entrepreneurship": (
        "startup", "startups", "founder", "founders", "entrepreneur", "entrepreneurs", "entrepreneurship",
        "pitch night", "small business", "business owners", "online business", "smes", "bootstrapping",
        "business networking", "business referral", "referral meetup", "b2b", "export import",
        "business strategy",
    ),
    "product": (
        "product management", "product manager", "product managers", "product growth", "product design",
        "product leaders", "product owner", "product owners",
    ),
    "data": (
        "data", "analytics", "data science", "data scientist", "data engineering", "sql", "power bi",
        "tableau", "statistics",
    ),
    "sustainability": (
        "sustainability", "sustainable", "climate", "esg", "net zero", "environment", "environmental",
        "renewable", "renewables", "recycling", "zero waste",
    ),
    "healthcare": (
        "healthcare", "health tech", "healthtech", "medical", "medicine", "pharma", "biotech",
        "life sciences", "nhs", "clinical",
    ),
    "public-sector": ("policy", "public policy", "government", "civil service", "public sector", "politics"),
    "law": ("law", "legal", "lawyer", "lawyers", "solicitor", "solicitors", "barrister", "attorney"),
    "creative": (
        "design", "designer", "designers", "ux", "photography", "photographers", "film", "filmmaking",
        "creative", "creatives", "creators", "content creators", "movie", "movies", "filmmakers",
        "illustration", "animation", "fashion",
    ),
    "ai": (
        "ai", "artificial intelligence", "machine learning", "deep learning", "llm", "llms", "genai",
        "generative ai", "chatgpt", "claude", "openai", "agentic", "ai agents", "multi agent",
        "prompt engineering", "neural networks",
    ),
    "crypto": (
        "crypto", "cryptocurrency", "bitcoin", "ethereum", "web3", "blockchain", "defi", "nft", "nfts",
        "solana", "dao",
    ),
    "careers": (
        "career", "careers", "career growth", "job", "jobs", "job search", "hiring", "hired", "recruitment",
        "recruiting", "resume", "cv", "interview", "interviews", "internship", "internships", "placement",
        "placements", "job fair", "career fair", "mentorship", "mentoring", "upskilling", "get job",
        "job seekers",
    ),
    "science": (
        "science", "scientific", "scientists", "researchers", "physics", "chemistry", "biology", "astronomy",
        "space", "mathematics", "maths", "math", "stem", "neuroscience", "quantum",
    ),
    "socialising": (
        "social", "socials", "socialising", "socializing", "friends", "new friends", "make friends", "mixer",
        "meet new people", "hangout", "party", "bowling", "pub", "speed friending",
    ),
    "fitness": (
        "fitness", "sport", "sports", "running", "run club", "gym", "workout", "yoga", "football", "cricket",
        "badminton", "tennis", "pickleball", "cycling", "climbing", "bouldering", "swimming", "basketball",
        "volleyball", "marathon",
    ),
    "travel": (
        "travel", "travellers", "travelers", "travelling", "traveling", "trip", "trips", "trek", "trekking",
        "hiking", "hike", "outdoors", "outdoor", "adventure", "backpacking", "road trip", "camping",
    ),
    "arts": (
        "art", "arts", "culture", "cultural", "museum", "gallery", "theatre", "theater", "painting",
        "drawing", "history", "language exchange",
    ),
    "music": (
        "music", "concert", "gig", "live music", "dj", "karaoke", "nightlife", "jam session", "band",
        "singing",
    ),
    "food": (
        "food", "foodie", "foodies", "drinks", "dinner", "brunch", "coffee", "wine", "beer", "cooking",
        "baking", "restaurant", "tasting", "food drink",
    ),
    "wellbeing": (
        "wellbeing", "well being", "wellness", "mental health", "meditation", "mindfulness", "self care",
        "breathwork", "journaling", "gardening", "plants",
    ),
    "volunteering": (
        "volunteer", "volunteers", "volunteering", "charity", "fundraiser", "community service", "cleanup",
        "clean up", "nonprofit", "non profit",
    ),
    "gaming": (
        "gaming", "gamers", "video games", "board games", "board game", "esports", "game night",
        "games night", "chess",
    ),
    "books": (
        "book", "books", "book club", "reading", "readers", "literature", "poetry", "poems", "writing",
        "writers", "authors", "storytelling", "novel", "novels",
    ),
    "growth": (
        "personal growth", "personal development", "self improvement", "self development", "leadership",
        "coaching", "life coaching", "mindset", "productivity", "public speaking", "toastmasters",
        "confidence", "motivation", "habits", "goal setting",
    ),
    "hobbies": (
        "hobby", "hobbies", "crafts", "craft", "crafting", "diy", "knitting", "crochet", "sewing", "pottery",
        "woodworking", "origami", "calligraphy", "lego", "model making",
    ),
}

# Where a keyword appears decides how much it says: the title most, a passing mention least.
WEIGHT = {"title": 3, "tags": 3, "summary": 2, "description": 1}

MIN_SCORE = 3  # a title/tag hit, a summary hit backed by one more, or three description keywords
MAX_INTERESTS = 4

# Platform category names (Meetup, Luma, Eventbrite and Fatsoma) → interests.
PLATFORM_CATEGORIES: dict[str, tuple[str, ...]] = {
    "art culture": ("arts",),
    "career business": ("entrepreneurship", "careers"),
    "community environment": ("sustainability", "volunteering"),
    "dancing": ("music", "fitness"),
    "games": ("gaming",),
    "health wellbeing": ("wellbeing",),
    "hobbies passions": ("hobbies",),
    "identity language": ("arts",),
    "movements politics": ("public-sector",),
    "music": ("music",),
    "parents family": ("socialising",),
    "religion spirituality": ("wellbeing",),
    "science education": ("science",),
    "social activities": ("socialising",),
    "sports fitness": ("fitness",),
    "support coaching": ("growth",),
    "technology": ("technology",),
    "travel outdoor": ("travel",),
    "writing": ("books",),
    "ai": ("ai", "technology"),
    "arts culture": ("arts",),
    "books": ("books",),
    "climate": ("sustainability",),
    "crypto": ("crypto",),
    "family": ("socialising",),
    "fitness": ("fitness",),
    "food drink": ("food",),
    "running": ("fitness",),
    "tech": ("technology",),
    "wellness": ("wellbeing",),
    "auto boat air": ("hobbies",),
    "business professional": ("entrepreneurship",),
    "charity causes": ("volunteering",),
    "community culture": ("socialising",),
    "family education": ("growth",),
    "fashion beauty": ("creative",),
    "film media entertainment": ("creative",),
    "government politics": ("public-sector",),
    "health wellness": ("wellbeing",),
    "hobbies special interest": ("hobbies",),
    "home lifestyle": ("hobbies",),
    "performing visual arts": ("arts",),
    "science technology": ("technology", "science"),
    "seasonal holiday": ("socialising",),
    "arts performance": ("arts",),
    "business conferences": ("entrepreneurship",),
    "classes": ("growth",),
    "club nights": ("music", "socialising"),
    "comedy": ("arts",),
    "dating": ("socialising",),
    "festivals": ("music",),
    "fun things": ("socialising",),
    "gigs": ("music",),
}

__all__ = ["HINT_WEIGHT", "ALL_INTERESTS", "infer_interests", "with_interests"]


def _hint_interests(label: str) -> list[str]:
    """Interests one hint points to: a known platform category, else its words against the keywords."""
    name = normalize(label)
    if not name:
        return []
    if name in PLATFORM_CATEGORIES:
        return list(PLATFORM_CATEGORIES[name])
    return [value for value, terms in INTEREST_KEYWORDS.items() if any(contains(name, term) for term in terms)]


def infer_interests(event: dict[str, Any], hints: Iterable[dict[str, Any]] = ()) -> list[str]:
    """The interests an event is clearly about, best first."""
    from_hints: dict[str, int] = {}
    for hint in hints:
        for value in _hint_interests(hint["label"]):
            from_hints[value] = from_hints.get(value, 0) + hint["weight"]
    fields = {
        "title": normalize(event.get("title") or ""),
        "tags": normalize(" ".join(event.get("tags") or [])),
        "summary": normalize(event.get("summary") or ""),
        "description": normalize(event.get("description") or ""),
    }
    scored = []
    for value, terms in INTEREST_KEYWORDS.items():
        score = 0
        for term in terms:
            score += next(
                (WEIGHT[f] for f in ("title", "tags", "summary", "description") if contains(fields[f], term)), 0
            )
        scored.append((value, score + from_hints.get(value, 0)))
    kept = [(v, s) for v, s in scored if v in _VALUES and s >= MIN_SCORE]
    kept.sort(key=lambda item: -item[1])  # stable: ties keep the taxonomy order
    return [v for v, _ in kept[:MAX_INTERESTS]]


def with_interests(event: dict[str, Any], hints: Iterable[dict[str, Any]] = ()) -> list[str]:
    """The event's tags plus any inferred interests it's missing (existing tags first, never removed)."""
    tags = list(event.get("tags") or [])
    return [*tags, *[i for i in infer_interests(event, hints) if i not in tags]]
