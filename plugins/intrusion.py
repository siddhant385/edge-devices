"""Polygon-zone intrusion detection plugin."""

from __future__ import annotations

import numpy as np
import supervision as sv

from config.camera_settings import CameraSettings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class IntrusionDetectionPlugin:
    """Emit only detections whose bottom centre lies in the configured zone."""

    name = "intrusion_detection"

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        raw_polygon = getattr(settings, "intrusion_zone_polygon", None)
        if not raw_polygon:
            raise ValueError(
                "INTRUSION_ZONE_POLYGON is required for intrusion_detection"
            )

        self._raw_polygon = np.array(raw_polygon, dtype=np.float32)
        self._min_trigger_count = getattr(settings, "intrusion_min_count", 1)

        # Check if coordinates are normalized (0.0 to 1.0)
        self._is_normalized = (self._raw_polygon <= 1.0).all()

        self._zone: sv.PolygonZone | None = None
        self._last_resolution: tuple[int, int] | None = None

        # If absolute coordinates were provided, instantiate immediately
        if not self._is_normalized:
            self._zone = sv.PolygonZone(
                polygon=self._raw_polygon.astype(np.int64),
                triggering_anchors=[
                    sv.Position.BOTTOM_CENTER
                ],  # Detect based on feet on ground
            )

    def _ensure_zone(self, height: int, width: int) -> sv.PolygonZone:
        """Dynamically scale normalized coordinates to the current camera resolution."""
        current_resolution = (width, height)
        if self._zone is None or (
            self._is_normalized and self._last_resolution != current_resolution
        ):
            scaled_poly = self._raw_polygon.copy()
            scaled_poly[:, 0] *= width
            scaled_poly[:, 1] *= height

            self._zone = sv.PolygonZone(
                polygon=scaled_poly.astype(np.int64),
                triggering_anchors=[sv.Position.BOTTOM_CENTER],
            )
            self._last_resolution = current_resolution

        return self._zone

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        # Fast exit if no objects detected
        if context.detections is None or len(context.detections) == 0:
            return []

        h, w = context.frame.shape[:2]
        zone = self._ensure_zone(h, w)

        # Vectorized point-in-polygon evaluation
        in_zone = zone.trigger(context.detections)

        # Trigger event only when minimum object threshold is satisfied
        if in_zone.sum() >= self._min_trigger_count:
            intruders = context.detections[in_zone]
            return [FeatureEvent(self.name, intruders)]

        return []

    def annotate_preview(self, scene: np.ndarray) -> np.ndarray:
        h, w = scene.shape[:2]
        zone = self._ensure_zone(h, w)
        return sv.PolygonZoneAnnotator().annotate(scene, zone)
