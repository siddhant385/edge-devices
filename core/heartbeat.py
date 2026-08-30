"""Periodic device health reporting to the central server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

import psutil
from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.settings import Settings


class HeartbeatSender:
    """Daemon thread that POSTs device health metrics at a fixed interval."""

    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        settings: Settings,
        cameras_active: int,
        queue_depth_fn: callable,
    ) -> None:
        self._supabase = supabase
        self._device_id = settings.device_id
        self._interval = settings.heartbeat_interval_seconds
        self._timeout = settings.request_timeout_seconds
        self._cameras_active = cameras_active
        self._queue_depth_fn = queue_depth_fn
        self._boot_time = time.monotonic()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self._send()
            except Exception as error:
                logging.warning("Heartbeat failed: %s", error)
            await asyncio.sleep(self._interval)

    async def _send(self) -> None:
        try:
            user_response = await self._supabase.auth.get_user()
            user_id = user_response.user.id
            
            # Find device linked to this auth user
            device_response = await self._supabase.table("devices").select("id").eq("auth_user_id", user_id).single().execute()
            device_uuid = device_response.data["id"]
            
            payload = {
                "is_online": True,
                "last_seen_at": datetime.now(UTC).isoformat(),
            }
            await self._supabase.table("devices").update(payload).eq("id", device_uuid).execute()
        except Exception as error:
            logging.warning("Heartbeat failed: %s", error)

    @staticmethod
    def _read_temperature() -> float | None:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for entries in temps.values():
            for entry in entries:
                if entry.current > 0:
                    return round(entry.current, 1)
        return None
