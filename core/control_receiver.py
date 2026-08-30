"""Resilient server-to-edge runtime configuration receiver via SSE."""

from __future__ import annotations

import json
import logging
from threading import Lock

from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.settings import Settings, apply_remote_settings


class RuntimeSettingsStore:
    """Atomically expose server-approved runtime settings to camera workers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = ""
        self._generation = 0
        self._lock = Lock()

    def snapshot(self) -> tuple[Settings, int]:
        with self._lock:
            return self._settings, self._generation

    def apply(self, version: str, values: dict[str, object]) -> bool:
        with self._lock:
            if version == self._version:
                return False
            self._settings = apply_remote_settings(self._settings, values)
            self._version = version
            self._generation += 1
            return True


class ControlReceiver:
    """Connect to a server SSE endpoint for real-time configuration updates."""

    def __init__(self, supabase: AsyncSupabaseClient, settings: Settings, store: RuntimeSettingsStore) -> None:
        self._supabase = supabase
        self._device_id = settings.device_id
        self._store = store
        self._channel = None

    async def start(self) -> None:
        try:
            user_response = await self._supabase.auth.get_user()
            user_id = user_response.user.id
            
            # Find device linked to this auth user
            device_response = await self._supabase.table("devices").select("id, device_id").eq("auth_user_id", user_id).single().execute()
            device_uuid = device_response.data["id"]
            self._device_id = device_response.data["device_id"]
            
            # Get current settings
            settings_response = await self._supabase.table("device_settings").select("version, settings").eq("device_id", device_uuid).execute()
            if settings_response and settings_response.data and len(settings_response.data) > 0:
                self._apply_record(settings_response.data[0])
            
            self._channel = self._supabase.channel(f"settings-{self._device_id}")
            await self._channel.on_postgres_changes(
                event="UPDATE",
                schema="public",
                table="device_settings",
                filter=f"device_id=eq.{device_uuid}",
                callback=lambda payload: self._apply_record(payload["record"]),
            ).subscribe()
            logging.info("Subscribed to Supabase Realtime for control settings")
        except Exception as error:
            logging.error("Failed to subscribe to Supabase Realtime: %s", error)

    async def close(self) -> None:
        if self._channel is not None:
            await self._supabase.remove_channel(self._channel)

    def _apply_record(self, record: dict[str, object]) -> None:
        self._dispatch("settings", json.dumps({
            "version": record.get("version"),
            "settings": record.get("settings"),
        }))

    def _dispatch(self, event_type: str, data: str) -> None:
        if event_type not in ("settings", ""):
            return
        try:
            command = json.loads(data)
        except json.JSONDecodeError as error:
            logging.warning("SSE payload is not valid JSON: %s", error)
            return
        if not isinstance(command, dict):
            logging.warning("SSE payload must be a JSON object")
            return
        version = command.get("version")
        values = command.get("settings")
        if not isinstance(version, str) or not version:
            logging.warning("SSE payload requires a non-empty version")
            return
        if not isinstance(values, dict):
            logging.warning("SSE payload requires a settings object")
            return
        try:
            if self._store.apply(version, values):
                logging.info("Applied server runtime configuration version %s", version)
        except ValueError as error:
            logging.warning("Rejected server settings version %s: %s", version, error)
