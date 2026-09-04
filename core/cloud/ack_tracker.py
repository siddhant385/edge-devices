"""In-memory record of recently-acknowledged tracker IDs.

When an operator clicks "Acknowledge" on the dashboard, the server flips
``alerts.status`` to 'acknowledged' and Supabase Realtime broadcasts the
update. The edge subscribes (see ``AcknowledgementListener``) and calls
``record(tracker_id)`` here. The next time the sender considers emitting
an alert for that tracker, ``should_suppress`` short-circuits the write.

Pure data, no I/O, no asyncio. The listener owns the dict; the sender
reads it through a reference passed at construction.
"""

from __future__ import annotations

import time
from typing import Iterable


class AcknowledgementTracker:
    """Time-bounded set of tracker_ids whose alerts should be suppressed."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._ttl = ttl_seconds
        self._acknowledged_at: dict[int, float] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def record(self, tracker_id: int, now: float | None = None) -> None:
        """Mark a tracker as acknowledged. Stamps current epoch (or `now`)."""
        if now is None:
            now = time.time()
        self._acknowledged_at[tracker_id] = now

    def should_suppress(self, tracker_id: int, now: float | None = None) -> bool:
        """True if the tracker is within the suppression window."""
        if now is None:
            now = time.time()
        stamp = self._acknowledged_at.get(tracker_id)
        if stamp is None:
            return False
        if now - stamp >= self._ttl:
            # Expired; clean up lazily.
            del self._acknowledged_at[tracker_id]
            return False
        return True

    def prune(self, now: float | None = None) -> int:
        """Drop entries older than the TTL. Returns the number removed."""
        if now is None:
            now = time.time()
        expired = [t for t, t_time in self._acknowledged_at.items() if now - t_time >= self._ttl]
        for t in expired:
            del self._acknowledged_at[t]
        return len(expired)

    def known_ids(self) -> Iterable[int]:
        """Snapshot of currently-tracked tracker IDs. Used for diagnostics/tests."""
        return list(self._acknowledged_at.keys())

    def __len__(self) -> int:
        return len(self._acknowledged_at)
