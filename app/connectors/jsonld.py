"""The schema.org Event a page publishes as JSON-LD (port of `src/lib/sources/jsonld.ts`)."""

import json
import re
from typing import Any

from . import js

_SCRIPT = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>", re.I | re.A)


def parse_json(raw: str) -> Any:
    """`JSON.parse`, or MISSING when it throws."""
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return js.MISSING


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def ld_blocks(html: str) -> list[str]:
    return [match.group(1) for match in _SCRIPT.finditer(html)]


def _is_event_type(value: Any) -> bool:
    types = value if isinstance(value, list) else [value]
    return any(
        isinstance(t, str) and (t.endswith("Event") or t in ("Hackathon", "Festival")) for t in types
    )


def first_json_ld_event(html: str) -> dict[str, Any] | None:
    """The first JSON-LD item on the page that is an event, or None."""
    for block in ld_blocks(html):
        data = parse_json(block)
        if data is js.MISSING:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and _is_event_type(item.get("@type", js.MISSING)):
                return item
    return None


def address_country(address: Any) -> str | None:
    """A schema.org PostalAddress's country: a code ("IN") or a Country object's name."""
    value = js.get(address, "addressCountry")
    if isinstance(value, str):
        return js.trim(value) or None
    name = js.get(value, "name") if js.is_object(value) else None
    return (js.trim(name) or None) if isinstance(name, str) else None
