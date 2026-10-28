"""Routes whose responses carry Postgres `numeric` values as JSON numbers.

Prices and amounts (`events.price_min/price_max`, `ticket_types.price`,
`registrations.amount`) read back as `Decimal`, which pydantic writes as a string
("12.50") in a route that returns a plain dict. The frontend read them through
PostgREST as numbers, so routers that return raw rows use `NumbersRoute`, which turns
every Decimal in the endpoint's return value into an int or float before FastAPI
serialises it. Money arithmetic elsewhere keeps its Decimals.
"""

import functools
import inspect
from decimal import Decimal
from typing import Any, Callable

from fastapi.routing import APIRoute


def plain_numbers(value: Any) -> Any:
    """`value` with every Decimal (in dicts, lists and tuples) as an int or float."""
    if isinstance(value, Decimal):
        # FastAPI's own Decimal encoding: whole numbers as int, the rest as float.
        exponent = value.as_tuple().exponent
        return int(value) if isinstance(exponent, int) and exponent >= 0 else float(value)
    if isinstance(value, dict):
        return {key: plain_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_numbers(item) for item in value]
    return value


def _with_plain_numbers(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            return plain_numbers(await endpoint(*args, **kwargs))

    else:

        @functools.wraps(endpoint)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            return plain_numbers(endpoint(*args, **kwargs))

    return wrapped


class NumbersRoute(APIRoute):
    """An APIRoute whose dict/list responses have Decimals as JSON numbers."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__(path, _with_plain_numbers(endpoint), **kwargs)
