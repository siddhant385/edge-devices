"""Build alert payloads and apply per-tracker cooldown dedup.

Pure functions. No I/O, no shared state, no asyncio. Easy to unit-test and
easy to extend when a new column lands on ``detections`` or ``alerts``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from plugins.base import FeatureEvent

STALE_TRACKER_TTL_SECONDS = 60.0

# Feature names that produce an alerts row in addition to a detections row.
# Keep in sync with the alert_severity enum check on the server side.
HIGH_SEVERITY_FEATURES = frozenset({"virtual_border", "intrusion_detection"})


def build_payload(
    events: list[FeatureEvent],
    device_id: str,
    camera_id: str,
    severity: str = "critical",
    zone_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Flatten events into a single deliverable payload.

    Stamps:
    - tracker_id per detection (when present)
    - zone_name per detection (from event.detections.data)
    - zone_id per detection (looked up via zone_id_map)
    - evidence_jpeg (first non-None from any event, used once per batch)
    - _severity (consumed by sender when writing alerts rows)
    """
    zone_id_map = zone_id_map or {}
    detections: list[dict[str, Any]] = []
    evidence_jpeg: bytes | None = None

    for event in events:
        zone_names = event.detections.data.get("zone_name") if event.detections.data else None
        class_names = event.detections.data.get("class_name") if event.detections.data else None
        for index, (xyxy, confidence, class_id) in enumerate(
            zip(event.detections.xyxy, event.detections.confidence, event.detections.class_id, strict=True)
        ):
            det: dict[str, Any] = {
                "feature": event.feature,
                "class_id": int(class_id),
                "class_name": str(
                    (class_names[index] if class_names is not None and index < len(class_names) else "unknown")
                ),
                "confidence": round(float(confidence), 4),
                "bbox_xyxy": [round(float(v), 1) for v in xyxy],
            }
            if event.detections.tracker_id is not None:
                det["tracker_id"] = int(event.detections.tracker_id[index])
            if zone_names is not None and index < len(zone_names):
                name = str(zone_names[index])
                det["zone_name"] = name
                if name in zone_id_map:
                    det["zone_id"] = zone_id_map[name]
            if event.evidence_jpeg is not None and evidence_jpeg is None:
                evidence_jpeg = event.evidence_jpeg
            detections.append(det)

    return {
        "device_id": device_id,
        "camera_id": camera_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "detections": detections,
        "evidence_jpeg": evidence_jpeg,
        "_severity": severity,
    }


def cooldown_filter(
    detections: list[dict[str, Any]],
    last_alert_by_tracker: dict[int, float],
    cooldown_seconds: float,
    now: float | None = None,
    suppressed_trackers: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, float]]:
    """Drop duplicate alerts for the same tracker within the cooldown window.

    Optionally drops any tracker_id present in ``suppressed_trackers`` —
    populated by ``AcknowledgementTracker`` when an operator acknowledges
    an alert on the dashboard. Suppression is checked before the cooldown
    so acknowledged trackers never re-fire.

    Returns (kept_detections, updated_last_alert). Mutates the input dict
    in place for the seen-tracker set; the input list is not modified.
    """
    if now is None:
        now = time.time()
    seen: set[int] = set()
    kept: list[dict[str, Any]] = []
    for det in detections:
        tracker_id = det.get("tracker_id")
        if tracker_id is not None:
            if tracker_id in seen:
                continue
            seen.add(tracker_id)
            if suppressed_trackers and tracker_id in suppressed_trackers:
                continue
            last_alert_time = last_alert_by_tracker.get(tracker_id, 0)
            if now - last_alert_time < cooldown_seconds:
                continue
            last_alert_by_tracker[tracker_id] = now
        kept.append(det)

    # Drop stale entries (no recent activity for >60s) so the dict doesn't
    # grow unbounded over the lifetime of the process.
    stale = [t for t, t_time in last_alert_by_tracker.items() if now - t_time > STALE_TRACKER_TTL_SECONDS]
    for t in stale:
        del last_alert_by_tracker[t]

    return kept, last_alert_by_tracker


def build_alert_row(detection_row: dict[str, Any], severity: str) -> dict[str, Any] | None:
    """Build one ``alerts`` row from a freshly-inserted detection row.

    Returns None when the feature is not alert-eligible (i.e. not in
    HIGH_SEVERITY_FEATURES) or when there's no evidence to attach.
    """
    feature = detection_row.get("feature")
    if feature not in HIGH_SEVERITY_FEATURES:
        return None
    if not detection_row.get("evidence_path"):
        return None
    return {
        "device_id": detection_row["device_id"],
        "camera_id": detection_row["camera_id"],
        "timestamp": detection_row.get("timestamp"),
        "detection_id": detection_row.get("id"),
        "evidence_path": detection_row["evidence_path"],
        "has_evidence": True,
        "severity": severity,
        "status": "unacknowledged",
        "raw_payload": {
            "feature": feature,
            "class_name": detection_row.get("class_name"),
            "confidence": detection_row.get("confidence"),
            "tracker_id": detection_row.get("tracker_id"),
            "bbox_xyxy": detection_row.get("bbox_xyxy"),
            "zone_name": detection_row.get("zone_name"),
            "zone_id": detection_row.get("zone_id"),
        },
    }
