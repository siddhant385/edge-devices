"""HTTP alert delivery: orchestrates payload build, outbox, and Supabase writes.

This module is intentionally thin. Concrete work lives in:
- core.cloud.payload  : payload shape + cooldown filter + alert-row shape
- core.cloud.outbox   : durable JSONL queue with cap enforcement

AlertSender is a stateful object holding the supabase client, the
in-memory cooldown map, and the background flusher task.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from supabase._async.client import AsyncClient as AsyncSupabaseClient

from core.cloud.ack_tracker import AcknowledgementTracker
from core.cloud.outbox import Outbox
from core.cloud.payload import (
    HIGH_SEVERITY_FEATURES,
    build_alert_row,
    build_payload,
    cooldown_filter,
)
from plugins.base import FeatureEvent


logger = logging.getLogger(__name__)


class AlertSender:
    """POST detection batches asynchronously; retain failed payloads for later retry."""

    def __init__(
        self,
        supabase: AsyncSupabaseClient,
        queue_path: Path,
        max_queue_records: int = 500,
        device_uuid: str | None = None,
        device_id_str: str | None = None,
        ack_tracker: AcknowledgementTracker | None = None,
    ) -> None:
        self._supabase = supabase
        self._device_uuid = device_uuid
        self._device_id_str = device_id_str
        self._outbox = Outbox(queue_path, max_records=max_queue_records)
        self._ack_tracker = ack_tracker

        self._semaphore = asyncio.Semaphore(3)
        self._flush_task: asyncio.Task | None = None
        self._last_alert_by_tracker: dict[int, float] = {}

    async def start(self) -> None:
        if not self._device_uuid:
            logger.error("AlertSender started without a resolved device UUID.")
            return
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("AlertSender started for device %s", self._device_id_str)

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()

    async def send(
        self,
        events: list[FeatureEvent],
        device_id: str,
        camera_id: str,
        cooldown_seconds: float = 5.0,
        severity: str = "critical",
        zone_id_map: dict[str, str] | None = None,
    ) -> bool:
        if not events:
            return True
        payload = build_payload(events, device_id, camera_id, severity, zone_id_map or {})
        if not payload["detections"]:
            return True
        asyncio.create_task(self._send_or_queue(payload, cooldown_seconds))
        return True

    async def flush(self) -> bool:
        records = await self._outbox.read_all()
        if not records:
            return True
        delivered = 0
        for payload in records:
            cooldown = float(payload.get("_cooldown_seconds", 5.0))
            async with self._semaphore:
                ok = await self._post(payload, cooldown)
            if not ok:
                break
            delivered += 1
        if delivered > 0:
            await self._outbox.write_tail(records[delivered:])
        return delivered == len(records)

    # --- internals ----------------------------------------------------

    async def _send_or_queue(self, payload: dict[str, Any], cooldown_seconds: float) -> None:
        async with self._semaphore:
            ok = await self._post(payload, cooldown_seconds)
        if not ok:
            await self._outbox.append(payload)

    async def _periodic_flush(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.error("Error in periodic flush: %s", error)

    async def _post(self, payload: dict[str, Any], cooldown_seconds: float) -> bool:
        try:
            if not self._device_uuid:
                return False
            camera_uuid = str(payload["camera_id"])

            suppressed = self._suppressed_tracker_ids()

            valid_detections, self._last_alert_by_tracker = cooldown_filter(
                payload.get("detections", []),
                self._last_alert_by_tracker,
                cooldown_seconds=cooldown_seconds,
                suppressed_trackers=suppressed,
            )
            if not valid_detections:
                return True  # everything was deduped; nothing to send

            evidence_path = await self._upload_evidence(payload, camera_uuid)

            inserted_rows = await self._insert_detections(valid_detections, payload, camera_uuid, evidence_path)
            if inserted_rows:
                await self._insert_alerts(inserted_rows, payload)

            return True
        except Exception as error:
            logger.warning(
                "Detection delivery failed: %s (Type: %s)",
                error, type(error).__name__,
            )
            return False

    def _suppressed_tracker_ids(self) -> set[int] | None:
        """Return the set of tracker IDs currently suppressed by operator ack.

        Returns None when no tracker is configured so cooldown_filter can
        skip the membership check (slightly faster hot path).
        """
        if self._ack_tracker is None:
            return None
        return {t for t in self._ack_tracker.known_ids() if self._ack_tracker.should_suppress(t)}

    async def _upload_evidence(self, payload: dict[str, Any], camera_uuid: str) -> str | None:
        evidence_jpeg = payload.get("evidence_jpeg")
        if not evidence_jpeg:
            return None
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        alert_uuid = str(uuid.uuid4())
        path = f"{self._device_id_str}/{camera_uuid}/{date_str}/{alert_uuid}.jpg"
        file_options: dict[str, str] = {
            "content-type": "image/jpeg",
            "x-goog-meta-device-id": self._device_id_str or "",
            "x-goog-meta-camera-id": camera_uuid,
            "x-goog-meta-alert-id": alert_uuid,
            "x-goog-meta-timestamp": str(payload.get("timestamp") or ""),
        }
        await self._supabase.storage.from_("evidence").upload(
            path, evidence_jpeg, file_options,
        )
        return path

    async def _insert_detections(
        self,
        valid_detections: list[dict[str, Any]],
        payload: dict[str, Any],
        camera_uuid: str,
        evidence_path: str | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, det in enumerate(valid_detections):
            clean = {k: v for k, v in det.items() if k != "evidence"}
            det_evidence_path = None
            if evidence_path and (
                clean.get("feature") == "evidence_capture" or index == 0
            ):
                det_evidence_path = evidence_path
            rows.append({
                "device_id": self._device_uuid,
                "camera_id": camera_uuid,
                "timestamp": payload.get("timestamp"),
                "evidence_path": det_evidence_path,
                **clean,
            })

        if not rows:
            return []
        response = await self._supabase.table("detections").insert(rows).execute()
        return getattr(response, "data", None) or []

    async def _insert_alerts(
        self,
        inserted_detection_rows: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        severity = payload.get("_severity", "critical")
        # inserted_detection_rows are in the same order as the rows we sent.
        # Each db_row carries the actual evidence_path that was inserted into
        # the detections table (only the first detection of a high-severity
        # event gets the evidence_path; the rest get None). Build the alert
        # row from the db_row so we use the *actual* inserted evidence_path,
        # not the original payload (which never carried evidence_path).
        alerts: list[dict[str, Any]] = []
        for db_row in inserted_detection_rows:
            if not db_row.get("evidence_path"):
                # No evidence attached to this detection row. Skip alert
                # creation — the detection itself is still in the database
                # for analytics; only the dashboard alert is suppressed.
                continue
            alert = build_alert_row(db_row, severity)
            if alert is not None:
                alerts.append(alert)
        if not alerts:
            logger.debug(
                "No alert rows built from %d inserted detection(s) "
                "(no evidence_path or non-alert feature)",
                len(inserted_detection_rows),
            )
            return
        try:
            await self._supabase.table("alerts").insert(alerts).execute()
            logger.info("Inserted %d alert row(s)", len(alerts))
        except Exception as error:
            logger.warning(
                "Alert creation failed: %s (Type: %s)", error, type(error).__name__,
            )
