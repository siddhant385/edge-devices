"""HTTP alert delivery with a bounded durable JSONL outbox using async/await."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from supabase._async.client import AsyncClient as AsyncSupabaseClient
import aiofiles

from plugins.base import FeatureEvent


class AlertSender:
    """POST detection batches asynchronously and retain failed payloads for later retry."""

    def __init__(
        self, supabase: AsyncSupabaseClient, queue_path: Path, max_queue_records: int
    ) -> None:
        self._supabase = supabase
        self._queue_path = queue_path
        self._max_queue_records = max_queue_records
        
        # Concurrency semaphore to throttle max simultaneous outgoing requests
        self._semaphore = asyncio.Semaphore(3)
        self._queue_lock = asyncio.Lock()
        # Start a background task for continuous flushing if desired
        self._flush_task: asyncio.Task | None = None
        self._device_uuid_by_id: dict[str, str] = {}
        self._camera_uuid_by_key: dict[tuple[str, str], str] = {}

    async def start(self):
        """Start the background flusher and resolve device/camera UUIDs from auth context"""
        # Get current user's device
        user_response = await self._supabase.auth.get_user()
        user_id = user_response.user.id
        
        # Find device linked to this auth user
        device_response = await self._supabase.table("devices").select("id, device_id").eq("auth_user_id", user_id).single().execute()
        self._device_uuid = device_response.data["id"]
        self._device_id_str = device_response.data["device_id"]
        
        # Find cameras for this device
        cameras_response = await self._supabase.table("cameras").select("id, camera_id").eq("device_id", self._device_uuid).execute()
        self._camera_uuid_by_key = {cam["camera_id"]: cam["id"] for cam in cameras_response.data}
        
        self._flush_task = asyncio.create_task(self._periodic_flush())

    async def send(self, events: list[FeatureEvent], device_id: str, camera_id: str) -> bool:
        payload = self._payload(events, device_id, camera_id)
        
        # Don't wait for the post to finish, schedule it asynchronously
        # This makes the detection loop super fast
        asyncio.create_task(self._send_or_queue(payload))
        return True

    async def _send_or_queue(self, payload: dict[str, Any]) -> None:
        """Attempt to send. If it fails, put it in the local file queue."""
        async with self._semaphore:
            success = await self._post(payload)
        
        if not success:
            await self._enqueue(payload)

    async def _periodic_flush(self):
        """Background task that periodically checks the queue file and flushes it to the server"""
        while True:
            try:
                await asyncio.sleep(10) # check queue every 10 seconds
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in periodic flush: {e}")

    async def flush(self) -> bool:
        async with self._queue_lock:
            records = await self._read_queue()
            if not records:
                return True
            
            delivered = 0
            for payload in records:
                async with self._semaphore:
                    success = await self._post(payload)
                if not success:
                    break
                delivered += 1
            
            if delivered:
                await self._write_queue(records[delivered:])
            return delivered == len(records)

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()

    async def queue_depth(self) -> int:
        async with self._queue_lock:
            records = await self._read_queue()
            return len(records)

    async def _camera_uuid(self, camera_key: str) -> str:
        if camera_key not in self._camera_uuid_by_key:
            # Auto-register camera
            camera_payload = {
                "device_id": self._device_uuid,
                "camera_id": camera_key,
                "name": camera_key,
            }
            # Look up first just in case
            response = await self._supabase.table("cameras").select("id").eq("device_id", self._device_uuid).eq("camera_id", camera_key).execute()
            if response.data and len(response.data) > 0:
                self._camera_uuid_by_key[camera_key] = response.data[0]["id"]
            else:
                insert_response = await self._supabase.table("cameras").insert(camera_payload).execute()
                self._camera_uuid_by_key[camera_key] = insert_response.data[0]["id"]
                logging.info("Auto-registered new camera: %s", camera_key)
                
        return self._camera_uuid_by_key[camera_key]

    async def update_camera_status(self, camera_key: str, is_online: bool) -> None:
        try:
            camera_uuid = await self._camera_uuid(camera_key)
            await self._supabase.table("cameras").update({
                "is_online": is_online
            }).eq("id", camera_uuid).execute()
        except Exception as error:
            logging.warning("Failed to update camera status for %s: %s", camera_key, error)

    async def _post(self, payload: dict[str, Any]) -> bool:
        try:
            camera_key = str(payload["camera_id"])
            camera_uuid = await self._camera_uuid(camera_key)
            if not camera_uuid:
                logging.error("Camera %s not found for device %s", camera_key, self._device_id_str)
                return False
            
            evidence_jpeg = payload.pop("evidence_jpeg", None)
            evidence_path = None
            if evidence_jpeg:
                date = datetime.now(UTC).strftime("%Y-%m-%d")
                alert_uuid = str(uuid.uuid4())
                path = f"{self._device_id_str}/{camera_key}/{date}/{alert_uuid}.jpg"
                
                # Native async upload via supabase-py's storage3 async client
                await self._supabase.storage.from_("evidence").upload(
                    path,
                    evidence_jpeg,
                    {"content-type": "image/jpeg"}
                )
                evidence_path = path

            detections = []
            tracker_dedup = set()
            for detection in payload.get("detections", []):
                # Throttle duplicate detections of the same object in rapid succession
                tracker_id = detection.get("tracker_id")
                if tracker_id is not None:
                    if tracker_id in tracker_dedup:
                        continue
                    tracker_dedup.add(tracker_id)
                    
                # Store the most recent alert time for the tracker globally
                now = time.time()
                if tracker_id is not None:
                    last_alert_time = getattr(self, "_last_alert_by_tracker", {}).get(tracker_id, 0)
                    # Limit alert frequency to one per tracker per 5 seconds
                    if now - last_alert_time < 5.0:
                        continue
                    if not hasattr(self, "_last_alert_by_tracker"):
                        self._last_alert_by_tracker = {}
                    self._last_alert_by_tracker[tracker_id] = now
                    # Clean up old trackers from memory
                    stale = [t for t, t_time in self._last_alert_by_tracker.items() if now - t_time > 60]
                    for t in stale:
                        del self._last_alert_by_tracker[t]

                # Make sure evidence is NOT inserted into the detections table
                clean_detection = {k: v for k, v in detection.items() if k != "evidence"}
                # Only attach evidence path if this detection came from the evidence capture feature
                # or if it's the first detection in the batch as a fallback
                det_evidence_path = None
                if evidence_path and clean_detection.get("feature") == "evidence_capture":
                    det_evidence_path = evidence_path
                elif evidence_path and len(detections) == 0:
                     det_evidence_path = evidence_path
                
                detections.append({
                    "device_id": self._device_uuid,
                    "camera_id": camera_uuid,
                    "timestamp": payload.get("timestamp"),
                    "evidence_path": det_evidence_path,
                    **clean_detection
                })
            
            if detections:
                await self._supabase.table("detections").insert(detections).execute()
            return True
        except Exception as error:
            logging.warning("Alert delivery failed: %s (Type: %s)", error, type(error).__name__)
            return False

    def _payload(self, events: list[FeatureEvent], device_id: str, camera_id: str) -> dict[str, Any]:
        alerts = []
        evidence_jpeg = None
        for event in events:
            for index, (xyxy, confidence, class_id) in enumerate(
                zip(event.detections.xyxy, event.detections.confidence, event.detections.class_id, strict=True)
            ):
                alert = {
                    "feature": event.feature,
                    "class_id": int(class_id),
                    "class_name": str(event.detections.data.get("class_name", ["unknown"] * len(event.detections))[index]),
                    "confidence": round(float(confidence), 4),
                    "bbox_xyxy": [round(float(value), 1) for value in xyxy],
                }
                if event.detections.tracker_id is not None:
                    alert["tracker_id"] = int(event.detections.tracker_id[index])
                
                if event.evidence_jpeg is not None and index == 0 and evidence_jpeg is None:
                    evidence_jpeg = event.evidence_jpeg
                    
                alerts.append(alert)
        return {
            "device_id": device_id,
            "camera_id": camera_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "detections": alerts,
            "evidence_jpeg": evidence_jpeg,
        }

    async def _enqueue(self, payload: dict[str, Any]) -> None:
        async with self._queue_lock:
            records = await self._read_queue()
            records.append(payload)
            await self._write_queue(records[-self._max_queue_records :])

    async def _read_queue(self) -> list[dict[str, Any]]:
        if not self._queue_path.exists():
            return []
        records = []
        try:
            async with aiofiles.open(self._queue_path, mode="r", encoding="utf-8") as f:
                async for line in f:
                    try:
                        record = json.loads(line)
                        if record.get("evidence_jpeg"):
                            record["evidence_jpeg"] = base64.b64decode(record["evidence_jpeg"])
                        records.append(record)
                    except json.JSONDecodeError:
                        logging.warning("Ignoring malformed queued alert")
        except Exception:
            return []
        return records

    async def _write_queue(self, records: list[dict[str, Any]]) -> None:
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._queue_path.with_suffix(".tmp")
        serializable_records = [self._serializable_record(record) for record in records]
        content = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in serializable_records)
        async with aiofiles.open(temporary_path, mode="w", encoding="utf-8") as f:
            await f.write(content)
        temporary_path.replace(self._queue_path)

    @staticmethod
    def _serializable_record(record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("evidence_jpeg")
        if not isinstance(value, bytes):
            return record
        serialized = record.copy()
        serialized["evidence_jpeg"] = base64.b64encode(value).decode("ascii")
        return serialized
