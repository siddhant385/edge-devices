"""JPEG evidence capture for server-side face and ANPR processing."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from config.settings import Settings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class EvidenceCapturePlugin:
    """Attach one compressed frame to the selected preceding feature event."""

    name = "evidence_capture"

    def __init__(self, settings: Settings, services: PluginServices) -> None:
        self._source_feature = settings.evidence_source_feature
        self._max_width = settings.evidence_max_width
        self._jpeg_quality = settings.evidence_jpeg_quality
        self._last_capture_time_by_tracker: dict[int, float] = {}
        # Min Box area filter (e.g. 60x60 = 3600 pixels minimum)
        self._min_box_area = 3600
        # Time to wait before capturing another evidence of the same object
        self._capture_interval_seconds = 30.0

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        # Spatial Filter: Check if Virtual Border / Intrusion features were triggered
        # If enabled, only upload evidence from those plugins. Otherwise fallback to source_feature.
        target_events = [e for e in context.events if e.feature in ("virtual_border", "intrusion_detection")]
        if not target_events:
            # Fallback to general object_detection
            target_events = [e for e in context.events if e.feature == self._source_feature]
            
        event = next(
            (candidate for candidate in reversed(target_events) if len(candidate.detections)),
            None,
        )
        
        if event is None:
            return []
            
        import time
        now = time.monotonic()
        capture_needed = False

        # Minimum Pixel Gate & Debounce checks
        for i, bbox in enumerate(event.detections.xyxy):
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            area = width * height
            
            # Skip if bounding box is too small for Face/ANPR recognition on server
            if area < self._min_box_area:
                continue

            tracker_id = None
            if hasattr(event.detections, "tracker_id") and event.detections.tracker_id is not None:
                tracker_id = event.detections.tracker_id[i]

            if tracker_id is not None:
                last_capture = self._last_capture_time_by_tracker.get(tracker_id, 0)
                if now - last_capture >= self._capture_interval_seconds:
                    self._last_capture_time_by_tracker[tracker_id] = now
                    capture_needed = True
            else:
                # If no tracker_id, just capture it (fallback)
                capture_needed = True

        if capture_needed:
            event.evidence_jpeg = self._encode(context.frame)
            
            # Prevent memory leak by removing stale trackers not seen for 30 seconds
            stale_trackers = [tid for tid, t in self._last_capture_time_by_tracker.items() if now - t > 30]
            for tid in stale_trackers:
                del self._last_capture_time_by_tracker[tid]

        return []

    def _encode(self, frame) -> bytes:
        image = Image.fromarray(frame[:, :, ::-1])
        if image.width > self._max_width:
            height = round(image.height * self._max_width / image.width)
            image = image.resize((self._max_width, height), Image.Resampling.LANCZOS)
        encoded = BytesIO()
        image.save(encoded, format="JPEG", quality=self._jpeg_quality, optimize=True)
        return encoded.getvalue()
