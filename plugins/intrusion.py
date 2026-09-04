"""Polygon-zone intrusion detection plugin (multi-zone)."""

from __future__ import annotations

import numpy as np
import supervision as sv

from config.camera_settings import CameraSettings, NamedZone
from plugins.base import FeatureEvent, FrameContext, PluginServices


def _resolve_zones(settings: CameraSettings) -> list[NamedZone]:
    """Back-compat: build a single default zone from intrusion_zone_polygon
    when no explicit zones are configured. New operators should use `zones`.
    """
    if settings.zones:
        return list(settings.zones)
    if settings.intrusion_zone_polygon:
        return [
            NamedZone(
                name="default",
                polygon=tuple((float(x), float(y)) for x, y in settings.intrusion_zone_polygon),
                target_class_ids=(0,),
                min_count=1,
            )
        ]
    raise ValueError(
        "intrusion_detection requires either `zones` (list of NamedZone) "
        "or the legacy `intrusion_zone_polygon` to be set"
    )


class IntrusionDetectionPlugin:
    """Emit one event per triggered zone whose BOTTOM_CENTER lies inside it."""

    name = "intrusion_detection"

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        self._zones: list[NamedZone] = _resolve_zones(settings)
        # Compiled sv.PolygonZone is per-resolution; rebuild only when frame size changes.
        self._compiled: list[sv.PolygonZone | None] = [None] * len(self._zones)
        self._last_resolution: tuple[int, int] | None = None

    def _ensure_resolution(self, h: int, w: int) -> None:
        if self._last_resolution == (w, h):
            return
        for i, zone in enumerate(self._zones):
            arr = np.array(zone.polygon, dtype=np.float32)
            if (arr <= 1.0).all():
                arr = arr.copy()
                arr[:, 0] *= w
                arr[:, 1] *= h
            self._compiled[i] = sv.PolygonZone(
                polygon=arr.astype(np.int64),
                triggering_anchors=[sv.Position.BOTTOM_CENTER],
            )
        self._last_resolution = (w, h)

    @staticmethod
    def _filter_by_class(dets: sv.Detections, class_ids: tuple[int, ...]) -> np.ndarray:
        if not class_ids or dets.class_id is None:
            return np.ones(len(dets), dtype=bool)
        return np.isin(dets.class_id, list(class_ids))

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        if context.detections is None or len(context.detections) == 0:
            return []

        h, w = context.frame.shape[:2]
        self._ensure_resolution(h, w)

        events: list[FeatureEvent] = []
        for zone, compiled in zip(self._zones, self._compiled):
            if compiled is None:
                continue
            class_mask = self._filter_by_class(context.detections, zone.target_class_ids)
            if not class_mask.any():
                continue
            in_zone = compiled.trigger(context.detections[class_mask])
            if int(in_zone.sum()) < zone.min_count:
                continue
            subset = context.detections[class_mask][in_zone]
            data = dict(subset.data) if subset.data else {}
            data["zone_name"] = np.array([zone.name] * len(subset))
            zone_id = getattr(context, "settings", None)
            # zone_id is resolved by the sender from camera_settings.zone_id_map;
            # we just stamp zone_name here. zone_id is added in sender via
            # a per-event lookup keyed on data["zone_name"][0].
            events.append(FeatureEvent(self.name, subset))
            events[-1].detections.data = data
        return events

    def annotate_preview(self, scene: np.ndarray) -> np.ndarray:
        h, w = scene.shape[:2]
        self._ensure_resolution(h, w)
        annotator = sv.PolygonZoneAnnotator()
        for compiled in self._compiled:
            if compiled is not None:
                scene = annotator.annotate(scene, compiled)
        return scene
