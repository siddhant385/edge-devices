"""Remote Procedure Call Command Executor."""

from __future__ import annotations

import asyncio
import logging

import cv2
from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.device_settings import DeviceSettings
from utils.frame_broker import FrameBroker

logger = logging.getLogger(__name__)


def _compress_frame(frame) -> bytes:
    """Synchronous CPU-bound JPEG compression."""
    # Resize slightly if too large to save bandwidth, or just compress directly.
    # We use a modest quality for fast upload.
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
    success, buffer = cv2.imencode(".jpg", frame, encode_param)
    if not success:
        raise ValueError("Failed to encode frame to JPEG")
    return buffer.tobytes()


class CommandExecutor:
    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        device_settings: DeviceSettings,
        frame_broker: FrameBroker,
    ):
        self.supabase = supabase
        self.device_id = device_settings.device_id
        self.frame_broker = frame_broker
        self._channel = None

    async def start(self) -> None:
        try:
            # We must subscribe using the textual slug 'sid-laptop'
            # because the frontend is inserting 'sid-laptop' into the text column 'device_id'.
            channel_name = f"commands-{self.device_id}"
            logger.info("Initializing Supabase Realtime channel '%s'...", channel_name)

            self._channel = self.supabase.channel(channel_name)

            def handle_payload(payload):
                logger.debug(
                    "Raw Realtime payload received on %s: %s", channel_name, payload
                )
                asyncio.create_task(
                    self._handle_command(payload.get("data", {}).get("record", {}))
                )

            # DO NOT use filter=f"device_id=eq.{self.device_id}" if it's failing silently.
            # Listen to ALL inserts in device_commands, and filter them out manually in python.
            # This guarantees we will see the event if it fires.
            await self._channel.on_postgres_changes(
                event="INSERT",
                schema="public",
                table="device_commands",
                callback=handle_payload,
            ).subscribe()

            logger.info(
                "Successfully Subscribed to command executor realtime events for device %s",
                self.device_id,
            )
        except Exception as e:
            logger.error("Failed to start CommandExecutor: %s", e)

    async def close(self) -> None:
        if self._channel is not None:
            await self.supabase.remove_channel(self._channel)

    async def _handle_command(self, record: dict) -> None:
        if not record:
            return

        command_id = record.get("id")
        command_name = record.get("command")
        camera_uuid = record.get("camera_id")
        target_device = record.get("device_id")
        payload = record.get("payload", {})

        # Manual fallback filter in case Supabase's Realtime filter fails
        if target_device != self.device_id:
            logger.debug(
                "Ignored command %s for different device %s", command_id, target_device
            )
            return

        if not command_id or not command_name:
            return

        # camera_uuid is natively the UUID now
        logger.info(
            "Received command %s (ID: %s) for camera %s",
            command_name,
            command_id,
            camera_uuid,
        )

        try:
            # Mark processing
            logger.info("Marking command %s as processing...", command_id)
            await self.supabase.table("device_commands").update(
                {"status": "processing"}
            ).eq("id", command_id).execute()

            result = {}
            if command_name in ("capture_snapshot", "snapshot"):
                result = await self._execute_capture_snapshot(
                    command_id, camera_uuid, payload
                )
            else:
                raise ValueError(f"Unknown command: {command_name}")

            # Mark completed
            logger.info("Marking command %s as completed...", command_id)
            await self.supabase.table("device_commands").update(
                {"status": "completed", "result": result}
            ).eq("id", command_id).execute()

            logger.info("Command %s completed successfully", command_id)

        except Exception as e:
            logger.error("Command %s failed: %s", command_id, e)
            await self.supabase.table("device_commands").update(
                {"status": "failed", "result": {"error": str(e)}}
            ).eq("id", command_id).execute()

    async def _execute_capture_snapshot(
        self, command_id: str, camera_id: str, payload: dict
    ) -> dict:
        """Capture a snapshot from the FrameBroker and upload to Supabase Storage."""
        if not camera_id:
            raise ValueError("camera_id is required for capture_snapshot command")

        logger.info("Executing snapshot capture for camera %s", camera_id)

        # 1. Fetch from Broker
        frame = self.frame_broker.get_latest_frame(camera_id)
        if frame is None:
            raise RuntimeError(
                f"No active frame available for camera {camera_id}. Stream might be down."
            )

        # 2. Compress asynchronously to avoid blocking the event loop
        try:
            image_bytes = await asyncio.to_thread(_compress_frame, frame)
        except Exception as e:
            raise RuntimeError(f"Frame compression failed: {e}")

        filename = f"{self.device_id}/{camera_id}_{command_id}_snapshot.jpg"

        # 3. Upload to Storage
        try:
            # We use synchronous-like call if the async wrapper doesn't support storage directly,
            # but supabase-py async client supports storage mostly.
            res = await self.supabase.storage.from_("snapshots").upload(
                path=filename,
                file=image_bytes,
                file_options={"content-type": "image/jpeg"},
            )
        except Exception as e:
            raise RuntimeError(f"Storage upload failed: {e}")

        # 4. Construct Public URL
        url = await self.supabase.storage.from_("snapshots").get_public_url(filename)

        return {"image_url": url}
