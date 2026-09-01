"""Resilient server-to-edge runtime configuration receiver via Supabase Realtime."""

from __future__ import annotations

import logging
from threading import Lock

from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.camera_settings import CameraSettings, apply_remote_camera_settings
from config.device_settings import DeviceSettings
from core.cloud.camera_manager import CameraManager


class RuntimeSettingsStore:
    """Atomically expose server-approved runtime settings to camera workers."""

    def __init__(
        self, device_settings: DeviceSettings, camera_manager: CameraManager
    ) -> None:
        self._device_settings = device_settings
        self._camera_manager = camera_manager

        self._versions: dict[str, str] = {}
        self._generations: dict[str, int] = {}
        self._device_version = ""
        self._device_generation = 0
        self._lock = Lock()

    def snapshot_device(self) -> tuple[DeviceSettings, int]:
        with self._lock:
            return self._device_settings, self._device_generation

    def snapshot_camera(self, camera_id: str) -> tuple[CameraSettings | None, int]:
        with self._lock:
            return self._camera_manager.get_settings(camera_id), self._generations.get(
                camera_id, 0
            )

    def apply_device(self, version: str, values: dict[str, object]) -> bool:
        with self._lock:
            if version and version == self._device_version:
                return False

            from dataclasses import replace

            updates = {}
            for k, v in values.items():
                if hasattr(self._device_settings, k):
                    updates[k] = v

            self._device_settings = replace(self._device_settings, **updates)
            self._device_version = version
            self._device_generation += 1
            return True

    def apply_camera(
        self, camera_id: str, version: str, values: dict[str, object]
    ) -> bool:
        with self._lock:
            current_version = self._versions.get(camera_id, "")
            if version and version == current_version:
                return False

            current_settings = self._camera_manager.get_settings(camera_id)
            if not current_settings:
                return False

            new_settings = apply_remote_camera_settings(current_settings, values)
            self._camera_manager.update_settings(camera_id, new_settings)
            self._versions[camera_id] = version
            self._generations[camera_id] = self._generations.get(camera_id, 0) + 1
            return True


class ControlReceiver:
    """Connect to Supabase Realtime for instant configuration updates."""

    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        device_settings: DeviceSettings,
        store: RuntimeSettingsStore,
        camera_manager: CameraManager,
    ) -> None:
        self._supabase = supabase
        self._device_id = device_settings.device_id
        self._store = store
        self._camera_manager = camera_manager
        self._channels = []

    async def start(self) -> None:
        try:
            # Note: Device linking logic usually relies on pre-authenticated client or system identity.
            # We assume the camera_manager has already synced and created rows in 'cameras' table.

            # Subscribe to device settings
            device_channel = self._supabase.channel(
                f"device-settings-{self._device_id}"
            )
            await device_channel.on_postgres_changes(
                event="UPDATE",
                schema="public",
                table="device_settings",
                callback=lambda payload: self._apply_device_record(
                    payload.get("data", {}).get("record", {})
                ),
            ).subscribe()
            self._channels.append(device_channel)

            cameras = self._camera_manager.load_local_cameras()

            for camera_id in cameras.keys():
                # Subscribe to real-time updates for each camera's settings
                channel = self._supabase.channel(f"camera-settings-{camera_id}")
                await channel.on_postgres_changes(
                    event="UPDATE",
                    schema="public",
                    table="camera_settings",
                    filter=f"camera_id=eq.{camera_id}",
                    callback=lambda payload: self._apply_camera_record(
                        payload.get("data", {}).get("record", {})
                    ),
                ).subscribe()
                self._channels.append(channel)

            logging.info(
                "Subscribed to Supabase Realtime for control settings for cameras: %s",
                list(cameras.keys()),
            )

        except Exception as error:
            logging.error("Failed to subscribe to Supabase Realtime: %s", error)

    async def close(self) -> None:
        for channel in self._channels:
            await self._supabase.remove_channel(channel)
        self._channels.clear()

    def _apply_device_record(self, record: dict[str, object]) -> None:
        """Process incoming settings record directly without JSON encoding overhead."""
        if not record:
            return

        device_id = record.get("device_id")
        values = record.get("settings")
        version = record.get("version")

        if not isinstance(values, dict):
            logging.warning(
                "Realtime payload requires a valid device settings dictionary"
            )
            return

        try:
            if self._store.apply_device(str(version) if version else "", values):
                logging.info(
                    "Applied server runtime configuration for device %s", device_id
                )
        except ValueError as error:
            logging.warning(
                "Rejected server settings for device %s: %s", device_id, error
            )

    def _apply_camera_record(self, record: dict[str, object]) -> None:
        """Process incoming settings record directly without JSON encoding overhead."""
        if not record:
            return

        camera_id = record.get("camera_id")
        values = record.get("settings")
        version = record.get("version")
        logging.info(record)

        if not camera_id or not isinstance(camera_id, str):
            logging.warning("Realtime payload requires a camera_id string")
            return

        if not isinstance(values, dict):
            logging.warning("Realtime payload requires a valid settings dictionary")
            return

        try:
            if self._store.apply_camera(
                camera_id, str(version) if version else "", values
            ):
                logging.info(
                    "Applied server runtime configuration for camera %s", camera_id
                )
        except ValueError as error:
            logging.warning(
                "Rejected server settings for camera %s: %s", camera_id, error
            )
