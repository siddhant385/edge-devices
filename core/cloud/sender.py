"""HTTP alert delivery with a bounded durable JSONL outbox using async/await."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiofiles
from supabase._async.client import AsyncClient as AsyncSupabaseClient

from plugins.base import FeatureEvent

_HIGH_SEVERITY_FEATURES = frozenset({"virtual_border", "intrusion_detection"})


class AlertSender:
    """POST detection batches asynchronously and retain failed payloads for later retry."""

    def __init__(
        self, supabase: AsyncSupabaseClient, queue_path: Path, max_queue_records: int = 500,
        device_uuid: str = None, device_id_str: str = None
    ) -> None:
        self._supabase = supabase
        self._queue_path = queue_path
        self._max_queue_records = max_queue_records

        self._semaphore = asyncio.Semaphore(3)
        self._queue_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        
        self._device_uuid = device_uuid
        self._device_id_str = device_id_str
        self._last_alert_by_tracker: dict[int, float] = {}

    async def start(self) -> None:
        """Start background flusher."""
        try:
            if not self._device_uuid:
                logging.error("AlertSender started without a resolved device UUID.")
                return

            self._flush_task = asyncio.create_task(self._periodic_flush())
            logging.info("AlertSender started for device %s", self._device_id_str)
        except Exception as error:
            logging.error("Failed to initialize AlertSender: %s", error)

    async def send(self, events: list[FeatureEvent], device_id: str, camera_id: str) -> bool:
        if not events:
            return True

        payload = self._payload(events, device_id, camera_id)
        if not payload["detections"]:
            return True

        # Non-blocking async dispatch
        asyncio.create_task(self._send_or_queue(payload))
        return True

    async def _send_or_queue(self, payload: dict[str, Any]) -> None:
        async with self._semaphore:
            success = await self._post(payload)

        if not success:
            await self._enqueue(payload)

    async def _periodic_flush(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error in periodic flush: %s", e)

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

            if delivered > 0:
                await self._write_queue(records[delivered:])
            return delivered == len(records)

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()

    async def _post(self, payload: dict[str, Any]) -> bool:
        try:
            if not self._device_uuid:
                return False

            # The payload camera_id is now natively a True Postgres UUID
            camera_uuid = str(payload["camera_id"])

            # 1. Throttling & Deduplication (Do this BEFORE uploading images)
            now = time.time()
            valid_detections = []
            tracker_dedup = set()

            for det in payload.get("detections", []):
                tracker_id = det.get("tracker_id")
                if tracker_id is not None:
                    if tracker_id in tracker_dedup:
                        continue
                    tracker_dedup.add(tracker_id)

                    last_alert_time = self._last_alert_by_tracker.get(tracker_id, 0)
                    if now - last_alert_time < 5.0:  # 5-second cooldown
                        continue
                    self._last_alert_by_tracker[tracker_id] = now

                valid_detections.append(det)

            # Cleanup stale tracking IDs
            stale_ids = [t for t, t_time in self._last_alert_by_tracker.items() if now - t_time > 60]
            for t in stale_ids:
                del self._last_alert_by_tracker[t]

            if not valid_detections:
                return True  # Nothing valid to report, skip quietly

            # 2. Upload Evidence Image (Safe .get() instead of .pop() to prevent data loss on retry)
            evidence_jpeg = payload.get("evidence_jpeg")
            evidence_path = None

            if evidence_jpeg:
                date_str = datetime.now(UTC).strftime("%Y-%m-%d")
                alert_uuid = str(uuid.uuid4())
                evidence_path = f"{self._device_id_str}/{camera_uuid}/{date_str}/{alert_uuid}.jpg"
                # logging.debug("[MOCK]Uploaded evidence to supabase storage")
                await self._supabase.storage.from_("evidence").upload(
                    evidence_path,
                    evidence_jpeg,
                    {"content-type": "image/jpeg"}
                )

            # 3. Insert into 'detections' so the database trigger can wake up the ai-worker
            detections_to_insert = []
            for det in valid_detections:
                clean_det = {k: v for k, v in det.items() if k != "evidence"}
                
                det_evidence_path = None
                # Only attach evidence if it's an evidence_capture event, or as a fallback for the first item
                if evidence_path and (clean_det.get("feature") == "evidence_capture" or len(detections_to_insert) == 0):
                    det_evidence_path = evidence_path

                detections_to_insert.append({
                    "device_id": self._device_uuid,
                    "camera_id": camera_uuid,
                    "timestamp": payload.get("timestamp"),
                    "evidence_path": det_evidence_path,
                    **clean_det
                })

            if detections_to_insert:
                insert_response = (
                    await self._supabase.table("detections")
                    .insert(detections_to_insert)
                    .execute()
                )

                # 4. Create high-severity alerts for spatial events with evidence
                inserted = getattr(insert_response, "data", None) or []
                alerts_to_insert = []
                for row, payload in zip(inserted, detections_to_insert):
                    feature = payload.get("feature")
                    if feature not in _HIGH_SEVERITY_FEATURES:
                        continue
                    if not payload.get("evidence_path"):
                        continue
                    alerts_to_insert.append(
                        {
                            "device_id": payload["device_id"],
                            "camera_id": payload["camera_id"],
                            "timestamp": payload.get("timestamp"),
                            "detection_id": row.get("id"),
                            "evidence_path": payload["evidence_path"],
                            "has_evidence": True,
                            "severity": "critical",
                            "status": "unacknowledged",
                            "raw_payload": {
                                "feature": feature,
                                "class_name": payload.get("class_name"),
                                "confidence": payload.get("confidence"),
                                "tracker_id": payload.get("tracker_id"),
                                "bbox_xyxy": payload.get("bbox_xyxy"),
                            },
                        }
                    )

                if alerts_to_insert:
                    try:
                        await self._supabase.table("alerts").insert(
                            alerts_to_insert
                        ).execute()
                    except Exception as alert_error:
                        logging.warning(
                            "Alert creation failed: %s (Type: %s)",
                            alert_error,
                            type(alert_error).__name__,
                        )

            return True

        except Exception as error:
            logging.warning("Detection delivery failed: %s (Type: %s)", error, type(error).__name__)
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

                if event.evidence_jpeg is not None and evidence_jpeg is None:
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
        """Fast O(1) append to durable local JSONL log. Prevents SD card wear."""
        async with self._queue_lock:
            try:
                self._queue_path.parent.mkdir(parents=True, exist_ok=True)
                serialized = self._serializable_record(payload)
                line = json.dumps(serialized, separators=(",", ":")) + "\n"

                # FIXED: Open in append ("a") mode instead of reading/rewriting the whole file
                async with aiofiles.open(self._queue_path, mode="a", encoding="utf-8") as f:
                    await f.write(line)
            except Exception as e:
                logging.error("Failed to append detection to local outbox: %s", e)

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
                        continue
        except Exception:
            return []
        return records

    async def _write_queue(self, records: list[dict[str, Any]]) -> None:
        """Used by flush() to cleanly rewrite the queue after successful deliveries."""
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
