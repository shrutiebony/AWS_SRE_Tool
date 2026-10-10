"""Run independent reads together. Call order in the result matches the input."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: list[T], workers: int = 8) -> list[R]:
    rows = list(items)
    if len(rows) <= 1:
        return [fn(item) for item in rows]
    with ThreadPoolExecutor(max_workers=min(workers, len(rows))) as pool:
        return list(pool.map(fn, rows))
