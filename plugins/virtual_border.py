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
        start_x, start_y, end_x, end_y = settings.virtual_border_line
        self._line_zone = sv.LineZone(
            start=sv.Point(x=start_x, y=start_y),
            end=sv.Point(x=end_x, y=end_y),
        )

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        if context.detections.tracker_id is None:
            return []
        crossed_in, crossed_out = self._line_zone.trigger(context.detections)
        crossed = crossed_in | crossed_out
        if not crossed.any():
            return []
        return [FeatureEvent(self.name, context.detections[crossed])]
