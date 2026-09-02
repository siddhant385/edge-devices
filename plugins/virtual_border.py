"""Virtual border line-crossing detection plugin."""

from __future__ import annotations

import supervision as sv

from config.camera_settings import CameraSettings
from plugins.base import FeatureEvent, FrameContext, PluginServices


class VirtualBorderPlugin:
    """Emit detections that cross a virtual border line between two points."""

    name = "virtual_border"

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        if not settings.virtual_border_line:
            raise ValueError(
                "VIRTUAL_BORDER_LINE is required for virtual_border"
            )
        self._raw = settings.virtual_border_line
        self._line_zone: sv.LineZone | None = None
        self._frame_size: tuple[int, int] | None = None

    def _ensure_zone(self, frame) -> sv.LineZone:
        height, width = frame.shape[:2]
        if self._line_zone is not None and self._frame_size == (width, height):
            return self._line_zone
        self._frame_size = (width, height)
        start_x, start_y, end_x, end_y = self._raw
        if all(0.0 <= c <= 1.0 for c in (start_x, start_y, end_x, end_y)):
            start_x *= width
            end_x *= width
            start_y *= height
            end_y *= height
        if (start_x, start_y) == (end_x, end_y):
            raise ValueError(
                "VIRTUAL_BORDER_LINE start and end points must differ "
                f"(got ({start_x:.0f}, {start_y:.0f}) -> ({end_x:.0f}, {end_y:.0f}))"
            )
        self._line_zone = sv.LineZone(
            start=sv.Point(x=start_x, y=start_y),
            end=sv.Point(x=end_x, y=end_y),
            triggering_anchors=(sv.Position.BOTTOM_CENTER,),
            minimum_crossing_threshold=1,
        )
        return self._line_zone

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        if context.detections.tracker_id is None:
            return []
        zone = self._ensure_zone(context.frame)
        crossed_in, crossed_out = zone.trigger(context.detections)
        crossed = crossed_in | crossed_out
        if not crossed.any():
            return []
        return [FeatureEvent(self.name, context.detections[crossed])]

    def annotate_preview(self, scene):
        if self._line_zone is None:
            self._ensure_zone(scene)
        return sv.LineZoneAnnotator().annotate(scene, self._line_zone)
