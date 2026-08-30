"""Polygon-zone intrusion detection plugin."""

from __future__ import annotations

import numpy as np
import supervision as sv

from config.settings import Settings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class IntrusionDetectionPlugin:
    """Emit only detections whose bottom centre lies in the configured zone."""

    name = "intrusion_detection"

    def __init__(self, settings: Settings, services: PluginServices) -> None:
        if not settings.intrusion_zone_polygon:
            raise ValueError("INTRUSION_ZONE_POLYGON is required for intrusion_detection")
        polygon = np.array(settings.intrusion_zone_polygon, dtype=np.int64)
        self._zone = sv.PolygonZone(polygon=polygon)
        
        # Determine minimum trigger count from settings (default 1)
        self._min_trigger_count = getattr(settings, 'intrusion_min_count', 1)

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        in_zone = self._zone.trigger(context.detections)
        
        # Only trigger an event if the number of detected objects inside the zone meets the threshold
        if in_zone.sum() >= self._min_trigger_count:
            return [FeatureEvent(self.name, context.detections[in_zone])]
            
        return []
