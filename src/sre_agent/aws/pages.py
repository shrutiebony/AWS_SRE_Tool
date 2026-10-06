"""Follow continuation tokens. A failed later page is a partial read, not full coverage."""

from __future__ import annotations

from collections.abc import Callable

from sre_agent.aws.errors import CloudError, PartialRead


def drain_pages(fetch_page: Callable[[str | None], tuple[list, str | None]]) -> list:
    token: str | None = None
    items: list = []
    seen: set[str] = set()
    while True:
        try:
            page_items, next_token = fetch_page(token)
        except CloudError as exc:
            if items and token is not None:
                raise PartialRead(str(exc), items) from exc
            raise
        items.extend(page_items)
        if not next_token:
            return items
        if next_token in seen:
            raise PartialRead("paginator repeated a token; coverage is incomplete", items)
        seen.add(next_token)
        token = next_token
