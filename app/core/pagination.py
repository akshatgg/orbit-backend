import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Sequence, TypeVar
from uuid import UUID

from fastapi import HTTPException, Query, status

from app.core.config import settings

T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    items: Sequence[Any]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


def pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
) -> tuple[int, int]:
    return page, page_size


def offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


# ─── keyset cursors ─────────────────────────────────────────────────────────
#
# Lists that grow (messages, notifications, conversations, feeds, the
# directory) page by keyset, never OFFSET: the response carries `next_cursor`,
# an opaque string the client sends back as `cursor` to get the next page.

CURSOR_LIMIT_MAX = 50


def encode_cursor(*values: Any) -> str:
    """Opaque, URL-safe cursor from the sort key of the last row."""

    def plain(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        return value

    raw = json.dumps([plain(v) for v in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, kinds: str) -> list[Any] | None:
    """The values of a cursor, typed by `kinds` (one letter each: t=timestamp, u=uuid,
    s=string, b=bool). None for no cursor; 422 for one that doesn't parse."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(values, list) or len(values) != len(kinds):
            raise ValueError
        out: list[Any] = []
        for kind, value in zip(kinds, values):
            if kind == "t":
                out.append(datetime.fromisoformat(value))
            elif kind == "u":
                out.append(str(UUID(value)))
            elif kind == "b":
                out.append(bool(value))
            else:
                out.append(str(value))
        return out
    except Exception:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid cursor") from None
