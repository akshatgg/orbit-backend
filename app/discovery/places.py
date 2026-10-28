"""Well-known cities and their other spellings, for the Location filter.

Port of the parts of the frontend's `src/lib/places.ts` that search uses. Events that only store a
city (organiser events, plain calendar feeds) still match a country through this table.
"""

import re

_GB = [
    "London", "Manchester", "Birmingham", "Leeds", "Liverpool", "Glasgow", "Edinburgh", "Bristol", "Sheffield",
    "Nottingham", "Leicester", "Cardiff", "Belfast", "Oxford", "Cambridge", "Brighton", "Southampton", "Reading",
    "Coventry", "York", "Bath", "Aberdeen", "Dundee", "Exeter", "Norwich", "Milton Keynes", "Canterbury", "Durham",
    "Lancaster", "Guildford", "Warwick", "St Andrews", "Swansea", "Plymouth", "Portsmouth", "Bournemouth",
]
_IN = [
    "Hyderabad", "Pune", "Ahmedabad", "Jaipur", "Chandigarh", "Mohali", "Lucknow", "Indore", "Bhopal", "Nagpur",
    "Surat", "Coimbatore", "Ghaziabad", "Faridabad", "Kanpur", "Varanasi", "Patna", "Bhubaneswar", "Dehradun",
    "Amritsar", "Ludhiana", "Vadodara", "Goa",
]

# (city, country, other spellings), in the frontend's order.
KNOWN_CITIES: list[tuple[str, str, tuple[str, ...]]] = [
    *[(city, "GB", ()) for city in _GB],
    ("Newcastle", "GB", ("Newcastle upon Tyne", "Newcastle-upon-Tyne")),
    ("Delhi", "IN", ("New Delhi", "NCT of Delhi")),
    ("Mumbai", "IN", ("Bombay", "Navi Mumbai")),
    ("Bengaluru", "IN", ("Bangalore",)),
    ("Chennai", "IN", ("Madras",)),
    ("Kolkata", "IN", ("Calcutta",)),
    ("Gurugram", "IN", ("Gurgaon",)),
    ("Noida", "IN", ("Greater Noida",)),
    ("Kochi", "IN", ("Cochin",)),
    ("Mysuru", "IN", ("Mysore",)),
    ("Thiruvananthapuram", "IN", ("Trivandrum",)),
    ("Visakhapatnam", "IN", ("Vizag",)),
    *[(city, "IN", ()) for city in _IN],
    ("New York", "US", ("New York City", "NYC")),
    ("San Francisco", "US", ()),
    ("Los Angeles", "US", ()),
    ("Paris", "FR", ()),
    ("Berlin", "DE", ()),
    ("Amsterdam", "NL", ()),
    ("Dublin", "IE", ()),
    ("Dubai", "AE", ()),
    ("Singapore", "SG", ()),
    ("Toronto", "CA", ()),
    ("Sydney", "AU", ()),
]

_SPACES = re.compile(r"\s+")


def _key(value: str) -> str:
    return _SPACES.sub(" ", value.strip().lower())


_BY_NAME: dict[str, tuple[str, str, tuple[str, ...]]] = {}
for _known in KNOWN_CITIES:
    for _name in (_known[0], *_known[2]):
        _BY_NAME[_key(_name)] = _known


def canonical_city(city: str) -> str:
    """The usual name for a city ("Bangalore" -> "Bengaluru"); unknown cities are just tidied."""
    known = _BY_NAME.get(_key(city))
    return known[0] if known else _SPACES.sub(" ", city.strip())


def city_variants(city: str) -> list[str]:
    """Every spelling of a city that should match when someone picks it."""
    known = _BY_NAME.get(_key(city))
    return [known[0], *known[2]] if known else [canonical_city(city)]


def known_city_names(country: str) -> list[str]:
    """Known cities of a country, every spelling."""
    return [name for city, code, aliases in KNOWN_CITIES if code == country for name in (city, *aliases)]


_NOT_LOCATION = re.compile(r"[^\w '&-]|_")


def clean_location(raw: str) -> str:
    """Letters, numbers, spaces and a little punctuation (the frontend's `cleanLocation`)."""
    return _SPACES.sub(" ", _NOT_LOCATION.sub(" ", raw)).strip()[:60]
