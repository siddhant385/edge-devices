"""Realtime subscription that records operator acknowledgements.

When the operator clicks "Acknowledge" on the dashboard, the server updates
``alerts.status`` to 'acknowledged'. Supabase Realtime broadcasts that UPDATE
on the ``alerts`` table. This listener catches it, extracts the tracker_id
(stashed in ``alerts.raw_payload->>'tracker_id'`` when the edge wrote it),
and tells the ``AcknowledgementTracker`` to suppress future alerts for
that tracker.

The tracker is shared with ``AlertSender`` (constructor arg) so the sender
can check it inside ``cooldown_filter`` and short-circuit the write.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from supabase._async.client import AsyncClient as AsyncSupabaseClient

from core.cloud.ack_tracker import AcknowledgementTracker

logger = logging.getLogger(__name__)

# Server-side alert_status enum values. We only suppress on 'acknowledged'.
_SUPPRESS_STATUSES = {"acknowledged", "resolved", "false_positive"}


class AcknowledgementListener:
    """Subscribes to ``alerts`` Realtime for this device's UUID."""

    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        device_uuid: str,
        tracker: AcknowledgementTracker,
    ) -> None:
        self._supabase = supabase
        self._device_uuid = device_uuid
        self._tracker = tracker
        self._channel = None
        # The realtime callback runs in the supabase client's internal loop.
        # We must not block it with I/O; recording into the dict is sync and
        # fast, so a plain lock is fine.
        self._lock = threading.Lock()

    async def start(self) -> None:
        topic = f"alerts-ack:{self._device_uuid}"
        self._channel = self._supabase.channel(topic)
        await self._channel.on_postgres_changes(
            event="UPDATE",
            schema="public",
            table="alerts",
            filter=f"device_id=eq.{self._device_uuid}",
            callback=self._on_update,
        ).subscribe()
        logger.info(
            "AcknowledgementListener subscribed to alerts UPDATE for device %s (ttl=%.0fs)",
            self._device_uuid,
            self._tracker.ttl_seconds,
        )

    async def close(self) -> None:
        if self._channel is None:
            return
        try:
            await self._supabase.remove_channel(self._channel)
        except Exception as error:
            logger.debug("Acknowledgement channel remove failed: %s", error)
        self._channel = None

    def _on_update(self, payload: dict[str, Any]) -> None:
        try:
            record = (payload.get("data") or {}).get("record") or {}
            if not record:
                return
            new_status = record.get("status")
            if new_status not in _SUPPRESS_STATUSES:
                return
            tracker_id = self._extract_tracker_id(record)
            if tracker_id is None:
                return
            with self._lock:
                self._tracker.record(tracker_id)
            logger.info(
                "Suppression recorded: tracker_id=%s status=%s alert_id=%s",
                tracker_id, new_status, record.get("id"),
            )
        except Exception as error:
            logger.warning("AcknowledgementListener callback error: %s", error)

    @staticmethod
    def _extract_tracker_id(record: dict[str, Any]) -> int | None:
        raw = record.get("raw_payload")
        if not isinstance(raw, dict):
            return None
        tracker_id = raw.get("tracker_id")
        if tracker_id is None:
            return None
        try:
            return int(tracker_id)
        except (TypeError, ValueError):
            return None
