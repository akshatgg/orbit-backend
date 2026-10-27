from dataclasses import dataclass
from typing import Any, Generic, Sequence, TypeVar

from fastapi import Query

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
