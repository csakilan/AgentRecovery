"""Deterministic time for executor tests; no production deadline needs a real wait."""

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 10, 20, tzinfo=UTC)
        self.elapsed = 0.0
        self.sleeps = []

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)
