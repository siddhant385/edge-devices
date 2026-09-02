"""Trackers-backed object tracking plugin."""

from __future__ import annotations

import supervision as sv
from trackers import ByteTrackTracker

from config.camera_settings import CameraSettings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class ObjectTrackingPlugin:
    """Assign persistent tracker IDs to detections from an earlier plugin."""

    name = "object_tracking"

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        self._tracker = ByteTrackTracker()
        self._smoother = (
            sv.DetectionsSmoother() if hasattr(sv, "DetectionsSmoother") else None
        )

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        class_names = context.detections.data.get("class_name")

        context.detections = self._tracker.update(context.detections)

        if self._smoother:
            context.detections = self._smoother.update_with_detections(context.detections)

        if (
            class_names is not None
            and "class_name" not in context.detections.data
            and len(context.detections) == len(class_names)
        ):
            context.detections.data["class_name"] = class_names

        return [FeatureEvent(self.name, context.detections)]