"""JavaScript semantics the importers depend on, so Python output matches the TypeScript engine.

The stored event text, URLs and ids were produced by Orbit's TypeScript importers. A
Python port that measured, trimmed or parsed differently would rewrite every row on its
first run, so the small JS behaviours that matter are reproduced here:

- string length and slicing in UTF-16 code units (emoji count as two);
- `\\s` and `trim()` over JavaScript's whitespace set (not Python's);
- `Number(x)`, truthiness and `String(number)`;
- `new Date(string)` for the ISO forms platforms send, and `toISOString()`.
"""

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# What JavaScript's \s, \S and String.prototype.trim() treat as whitespace.
WS_CHARS = (
    "\t\n\v\f\r \u00a0\u1680"
    + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_WS_CLASS = "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
S = f"[{_WS_CLASS}]"  # JavaScript \s
NS = f"[^{_WS_CLASS}]"  # JavaScript \S
S_IN = _WS_CLASS  # for use inside a larger character class

MISSING: Any = type("Missing", (), {"__repr__": lambda self: "undefined", "__bool__": lambda self: False})()
"""JavaScript `undefined`: a key that isn't there (distinct from JSON null / None)."""


def trim(value: str) -> str:
    return value.strip(WS_CHARS)


def trim_end(value: str) -> str:
    return value.rstrip(WS_CHARS)


def u16len(value: str) -> int:
    """`string.length`: UTF-16 code units."""
    return len(value) + sum(1 for ch in value if ord(ch) > 0xFFFF)


def u16slice(value: str, end: int) -> str:
    """`string.slice(0, end)` in UTF-16 units (a surrogate pair is never split)."""
    if end <= 0:
        return ""
    units = 0
    for index, ch in enumerate(value):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > end:
            return value[:index]
    return value


def u16index(value: str, codepoint_index: int) -> int:
    """UTF-16 offset of a code point index."""
    return u16len(value[:codepoint_index])


def get(obj: Any, key: str) -> Any:
    """`obj?.[key]`: MISSING unless obj is a plain object holding the key."""
    if isinstance(obj, dict):
        return obj.get(key, MISSING)
    return MISSING


def truthy(value: Any) -> bool:
    if value is MISSING or value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True  # objects and arrays, even empty ones


def is_object(value: Any) -> bool:
    """`value && typeof value === "object"` (arrays count, as in JS)."""
    return isinstance(value, (dict, list))


def record(value: Any) -> dict | None:
    """A plain object (not an array), else None."""
    return value if isinstance(value, dict) else None


def string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def number(value: Any) -> float:
    """`Number(value)`."""
    if value is MISSING:
        return math.nan
    if value is None or value is False:
        return 0.0
    if value is True:
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = trim(value)
        if text == "":
            return 0.0
        if _DECIMAL.match(text):
            return float(text)
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        for prefix, base in (("0x", 16), ("0o", 8), ("0b", 2)):
            if text[:2].lower() == prefix:
                try:
                    return float(int(text[2:], base))
                except ValueError:
                    return math.nan
        return math.nan
    if isinstance(value, list):
        if not value:
            return 0.0
        if len(value) == 1:
            return number(value[0] if not isinstance(value[0], (dict, list)) else to_string(value[0]))
        return math.nan
    return math.nan


def finite(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def num_str(value: float | int) -> str:
    """`String(number)` for the numbers importers print (ids, coordinates, cents)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == int(value) and abs(value) < 1e21:
            return str(int(value))
        text = repr(value)
        if "e" in text:
            mantissa, exponent = text.split("e")
            sign = "-" if exponent.startswith("-") else "+"
            return f"{mantissa}e{sign}{exponent.lstrip('+-').lstrip('0') or '0'}"
        return text
    return str(value)


def to_string(value: Any) -> str:
    if value is None:
        return "null"
    if value is MISSING:
        return "undefined"
    if isinstance(value, (bool, int, float)):
        return num_str(value)
    if isinstance(value, list):
        return ",".join("" if v is None or v is MISSING else to_string(v) for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def round_half_up(value: float) -> float:
    """`Math.round`."""
    return float(math.floor(value + 0.5))


# ─── Dates ───────────────────────────────────────────────────────────────────

_ISO = re.compile(
    r"^([+-]\d{6}|\d{4})(?:-(\d{2})(?:-(\d{2}))?)?"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_date(value: Any) -> int | None:
    """`new Date(value).getTime()` for ISO-8601 strings, in ms; None where JS gives NaN.

    A date-time without an offset is read as UTC: the TypeScript engine ran on Vercel,
    whose functions run in UTC.
    """
    if not isinstance(value, str):
        return None
    match = _ISO.match(trim(value))
    if not match:
        return None
    year_s, month_s, day_s, hour_s, minute_s, second_s, fraction_s, offset_s = match.groups()
    try:
        year = int(year_s)
        month = int(month_s or 1)
        day = int(day_s or 1)
        hour = int(hour_s or 0)
        minute = int(minute_s or 0)
        second = int(second_s or 0)
        if hour == 24 and minute == 0 and second == 0 and not fraction_s:
            hour, extra_day = 0, 1
        else:
            extra_day = 0
        moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    ms = int((moment - _EPOCH) // timedelta(milliseconds=1)) + extra_day * 86_400_000
    if fraction_s:
        ms += int((fraction_s + "00")[:3])
    if offset_s and offset_s not in ("Z", "z"):
        sign = 1 if offset_s[0] == "+" else -1
        digits = offset_s[1:].replace(":", "")
        offset_minutes = int(digits[:2]) * 60 + int(digits[2:4])
        if int(digits[:2]) > 23 or int(digits[2:4]) > 59:
            return None
        ms -= sign * offset_minutes * 60_000
    return ms


def iso(ms: int) -> str:
    """`new Date(ms).toISOString()`."""
    moment = _EPOCH + timedelta(milliseconds=ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def iso_or_none(value: Any) -> str | None:
    ms = parse_date(value)
    return iso(ms) if ms is not None else None


def to_ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int((moment - _EPOCH) // timedelta(milliseconds=1))


def from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def to_datetime(value: Any) -> datetime | None:
    """An ISO string or datetime as an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    ms = parse_date(value)
    return from_ms(ms) if ms is not None else None
