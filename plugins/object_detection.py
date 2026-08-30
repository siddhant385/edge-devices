"""Base CPU ONNX object-detection plugin."""

from __future__ import annotations

from config.settings import Settings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class ObjectDetectionPlugin:
    """Populate the shared Supervision detections for downstream plugins."""

    name = "object_detection"

    def __init__(self, settings: Settings, services: PluginServices) -> None:
        self._services = services
        self._confidence_threshold = settings.confidence_threshold
        self._nms_threshold = settings.nms_threshold
        self._target_class_ids = settings.target_class_ids

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        # Fast pre-filter
        if not self._services.processor.has_motion(context.frame):
            import supervision as sv
            context.detections = sv.Detections.empty()
            return []

        # One CPU ONNX session is shared across cameras to bound edge-device memory and CPU use.
        with self._services.inference_lock:
            context.detections = self._services.processor.detect(
                context.frame,
                confidence_threshold=self._confidence_threshold,
                nms_threshold=self._nms_threshold,
                target_class_ids=self._target_class_ids,
            )
        return [FeatureEvent(self.name, context.detections)]
