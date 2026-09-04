"""Base CPU ONNX object-detection plugin."""

from __future__ import annotations

from config.camera_settings import CameraSettings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class ObjectDetectionPlugin:
    """Populate the shared Supervision detections for downstream plugins."""

    name = "object_detection"

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        self._services = services
        self._confidence_threshold = settings.confidence_threshold
        self._nms_threshold = settings.nms_threshold
        self._target_class_ids = settings.target_class_ids

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        # No motion pre-filter. The frame-skip logic in CameraLoop already
        # caps YOLO calls at one per `process_every_n_frames`, so the motion
        # filter was a thin optimization. We removed it because MOG2 has two
        # bad failure modes on real border streams:
        #   1. On reconnect, MOG2 carries over its old background model and
        #      treats the new (different) scene as foreground, then freezes
        #      on the first frame as the new "background", missing real motion.
        #   2. After a single h264 decode error, frames become frozen
        #      duplicates which MOG2 learns as background within ~50 frames,
        #      again missing all subsequent motion.
        # The proper way to skip YOLO on static scenes is `process_every_n_frames`.

        # One CPU ONNX session is shared across cameras to bound edge-device memory and CPU use.
        with self._services.inference_lock:
            # We explicitly update the processor's thresholds before running inference
            # since the processor is shared across multiple cameras with different settings
            self._services.processor._confidence_threshold = self._confidence_threshold
            self._services.processor._nms_threshold = self._nms_threshold
            self._services.processor._target_class_ids = self._target_class_ids

            context.detections = self._services.processor.detect(context.frame)
        return [FeatureEvent(self.name, context.detections)]
