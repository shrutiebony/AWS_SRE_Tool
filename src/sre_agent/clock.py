"""Clocks for live AWS and for tests.

Live reads use SystemClock, which follows UTC and waits on the wall clock.
Tests and the local demo use SimulatedClock, which stays under the caller's control.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone


class SimulatedClock:
    """Fixed or stepped time. advance() does not mean that telemetry has arrived in AWS."""

    simulated = True

    def __init__(self, now: datetime | None = None, sleeper: Callable[[float], None] | None = None) -> None:
        self._now = now or datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=timezone.utc)
        self._sleeper = sleeper

    def now(self) -> datetime:
        return self._now

    def iso(self) -> str:
        return self._now.isoformat()

    def advance(self, seconds: float) -> None:
        if self._sleeper is not None:
            self._sleeper(seconds)
        self._now = self._now + timedelta(seconds=seconds)


class SystemClock:
    """Current UTC time. advance() waits, because a recovery check cannot skip ahead of CloudWatch."""

    simulated = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def iso(self) -> str:
        return self.now().isoformat()

    def advance(self, seconds: float) -> None:
        time.sleep(seconds)


# Tests and the local demo keep the simulated clock. Live entry points use SystemClock.
Clock = SimulatedClock


def parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
