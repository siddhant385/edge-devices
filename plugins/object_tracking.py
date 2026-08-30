"""Supervision-backed object tracking plugin."""

from __future__ import annotations

import supervision as sv

from config.settings import Settings
from plugins.base import FeatureEvent, FrameContext, PluginServices

class ObjectTrackingPlugin:
    """Assign persistent tracker IDs to detections from an earlier plugin."""

    name = "object_tracking"

    def __init__(self, settings: Settings, services: PluginServices) -> None:
        # We also want to smooth the bounding boxes to avoid jitter
        try:
            from supervision.trackers.byte_tracker.core import ByteTrack
            self._tracker = ByteTrack()
            self._modern_tracker = True
        except ImportError:
            # Fallback to legacy
            if hasattr(sv, "SVTracker"):
                self._tracker = sv.SVTracker()
            elif hasattr(sv, "ByteTrack"):
                self._tracker = sv.ByteTrack()
            else:
                raise RuntimeError("No compatible tracker found in supervision package")
            self._modern_tracker = False
            
        self._smoother = sv.DetectionsSmoother() if hasattr(sv, "DetectionsSmoother") else None

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        # Keep class_name metadata which gets wiped by update_with_detections in some versions of supervision
        if "class_name" in context.detections.data:
            class_names = context.detections.data["class_name"]
            
            if getattr(self, "_modern_tracker", False):
                context.detections = self._tracker.update_with_detections(context.detections)
            else:
                context.detections = self._tracker.update_with_detections(context.detections)
                
            if self._smoother:
                context.detections = self._smoother.update_with_detections(context.detections)
            # Reattach the metadata
            if len(context.detections) > 0 and len(context.detections) == len(class_names):
                context.detections.data["class_name"] = class_names
        else:
            if getattr(self, "_modern_tracker", False):
                context.detections = self._tracker.update_with_detections(context.detections)
            else:
                context.detections = self._tracker.update_with_detections(context.detections)
                
            if self._smoother:
                context.detections = self._smoother.update_with_detections(context.detections)
            
        return [FeatureEvent(self.name, context.detections)]
