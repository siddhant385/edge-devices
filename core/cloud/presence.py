"""Realtime presence channel that signals device liveness to the server.

The edge opens one Supabase Realtime presence channel and periodically
refreshes its presence payload with ``track()``. Each refresh triggers
our own ``on_presence_sync`` callback, which writes
``devices.last_seen_at = now()`` to Postgres. A pg_cron job marks
``is_online = false`` if ``last_seen_at`` is older than 90 s.

Why self-write from the edge instead of a server-side webhook:
Supabase Realtime presence is an in-memory CRDT inside the Realtime
cluster. There is no way to subscribe Postgres or an Edge Function to
presence events - presence only flows back to other WebSocket clients
on the same channel. Since the edge is one of those clients, it sees
its own join/sync events and can write the timestamp itself. The only
costs are: one track() per REFRESH_SECONDS (default 30 s, sent over
the already-open WebSocket) and one tiny REST upsert per sync event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from realtime.types import RealtimeChannelOptions
from supabase._async.client import AsyncClient as AsyncSupabaseClient

REFRESH_SECONDS = 30.0
OFFLINE_AFTER_SECONDS = 90.0


class PresencePublisher:
    """Owns one presence channel and keeps ``devices.last_seen_at`` fresh."""

    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        device_uuid: str,
        device_id: str,
    ) -> None:
        self._supabase = supabase
        self._device_uuid = device_uuid
        self._device_id = device_id
        self._channel = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._stop_refresh = asyncio.Event()

    async def start(self) -> None:
        topic = f"device-presence:{self._device_uuid}"
        options: RealtimeChannelOptions = {"config": {"presence": {"enabled": True}}}
        self._channel = self._supabase.channel(topic, options)

        self._channel.on_presence_sync(self._on_sync)
        self._channel.on_presence_join(self._on_join)
        self._channel.on_presence_leave(self._on_leave)
        await self._channel.subscribe()
        await self._track_now()

        self._stop_refresh.clear()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name=f"presence-refresh-{self._device_id}"
        )
        logging.info(
            "Presence channel %s tracking device %s (refresh=%.0fs, offline after=%.0fs)",
            topic,
            self._device_id,
            REFRESH_SECONDS,
            OFFLINE_AFTER_SECONDS,
        )

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._stop_refresh.set()
            try:
                await asyncio.wait_for(self._refresh_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._refresh_task.cancel()
            self._refresh_task = None

        if self._channel is None:
            return
        try:
            await self._channel.untrack()
        except Exception as error:
            logging.debug("Presence untrack failed (channel already closed): %s", error)
        try:
            await self._supabase.remove_channel(self._channel)
        except Exception as error:
            logging.debug("Presence channel remove failed: %s", error)
        self._channel = None

    def _presence_payload(self) -> dict[str, str]:
        return {
            "device_id": self._device_id,
            "device_uuid": self._device_uuid,
            "online_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _track_now(self) -> None:
        if self._channel is None:
            return
        try:
            await self._channel.track(self._presence_payload())
        except Exception as error:
            logging.warning("Presence track failed: %s", error)

    async def _refresh_loop(self) -> None:
        while not self._stop_refresh.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_refresh.wait(), timeout=REFRESH_SECONDS
                )
                return
            except asyncio.TimeoutError:
                pass
            await self._track_now()

    def _on_sync(self) -> None:
        # Fires on every join/leave reconciliation including our own track().
        # Schedule the upsert without blocking the realtime callback thread.
        asyncio.create_task(self._upsert_last_seen())

    def _on_join(self, key: str, _current, new_presences) -> None:
        for p in new_presences:
            logging.info(
                "Presence join key=%s device=%s", key, p.get("device_id")
            )

    def _on_leave(self, key: str, _current, left_presences) -> None:
        for p in left_presences:
            logging.warning(
                "Presence leave key=%s device=%s - pg_cron will mark offline after %.0fs",
                key,
                p.get("device_id"),
                OFFLINE_AFTER_SECONDS,
            )

    async def _upsert_last_seen(self) -> None:
        try:
            await (
                self._supabase.table("devices")
                .update(
                    {
                        "last_seen_at": datetime.now(timezone.utc).isoformat(),
                        "is_online": True,
                    }
                )
                .eq("id", self._device_uuid)
                .execute()
            )
        except Exception as error:
            logging.warning("Failed to upsert devices.last_seen_at: %s", error)
