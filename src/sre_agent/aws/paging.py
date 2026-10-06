"""Pagination and throttle handling shared by every collector."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from sre_agent.aws.errors import CloudError, PartialRead, Throttled

T = TypeVar("T")


@dataclass
class Page:
    items: list
    next_token: str | None = None


def with_backoff(fn: Callable[[], T], sleeper: Callable[[float], None] | None = None, attempts: int = 5) -> T:
    """Retry a call when the reader raises Throttled. Other errors propagate immediately."""
    sleep = sleeper or time.sleep
    delay = 0.05
    last: Throttled | None = None
    for _ in range(attempts):
        try:
            return fn()
        except Throttled as exc:
            last = exc
            sleep(delay)
            delay *= 2
    raise last if last is not None else CloudError("throttled")


def paginate(fetch_page: Callable[[str | None], Page]):
    """Follow next tokens until the reader says the list is finished."""
    token: str | None = None
    items: list = []
    seen: set[str] = set()
    while True:
        try:
            page = fetch_page(token)
        except CloudError as exc:
            if items and token is not None:
                raise PartialRead(str(exc), items) from exc
            raise
        items.extend(page.items)
        if not page.next_token:
            return items
        if page.next_token in seen:
            raise PartialRead("paginator repeated a token; coverage is incomplete", items)
        seen.add(page.next_token)
        token = page.next_token
