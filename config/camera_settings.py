"""Camera specific AI pipeline settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

REMOTE_PLUGIN_NAMES = frozenset(
    {
        "object_detection",
        "object_tracking",
        "intrusion_detection",
        "evidence_capture",
        "virtual_border",
    }
)

REMOTE_SETTING_NAMES = frozenset(
    {
        "process_every_n_frames",
        "confidence_threshold",
        "nms_threshold",
        "target_class_ids",
        "enabled_plugins",
        "intrusion_zone_polygon",
        "evidence_source_feature",
        "evidence_max_width",
        "evidence_jpeg_quality",
        "virtual_border_line",
    }
)


def _class_ids(value: str | list[int]) -> frozenset[int]:
    if isinstance(value, list):
        return frozenset(value)
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


def _plugins(value: str | list[str] | None) -> tuple[str, ...]:
    if not value:
        return ("object_detection", "evidence_capture")
    if isinstance(value, list):
        plugins = tuple(value)
    else:
        plugins = tuple(item.strip() for item in value.split(",") if item.strip())
    if not plugins:
        return ("object_detection", "evidence_capture")
    return plugins


def _polygon(value: str | list | None) -> tuple[tuple[int, int], ...]:
    if not value:
        return ()
    points = json.loads(value) if isinstance(value, str) else value
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(
            "intrusion_zone_polygon must be a JSON array of at least three [x, y] points"
        )
    return tuple((int(point[0]), int(point[1])) for point in points)


def _border_line(value: str | list | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    points = json.loads(value) if isinstance(value, str) else value
    if not isinstance(points, list) or len(points) != 2:
        raise ValueError(
            "virtual_border_line must be a JSON array of two [x, y] points"
        )
    return (
        float(points[0][0]),
        float(points[0][1]),
        float(points[1][0]),
        float(points[1][1]),
    )


@dataclass(frozen=True, slots=True)
class CameraSettings:
    """Settings for one independently processed camera."""

    camera_id: str
    source: str
    process_every_n_frames: int = 5
    inference_size: int = 640
    confidence_threshold: float = 0.45
    nms_threshold: float = 0.5
    target_class_ids: frozenset[int] = frozenset({0})
    enabled_plugins: tuple[str, ...] = ("object_detection", "evidence_capture")
    intrusion_zone_polygon: tuple[tuple[int, int], ...] = ()
    virtual_border_line: tuple[float, float, float, float] | None = None
    evidence_source_feature: str = "object_detection"
    evidence_max_width: int = 1280
    evidence_jpeg_quality: int = 75

    @classmethod
    def from_dict(cls, data: dict) -> "CameraSettings":
        return cls(
            camera_id=data["id"],
            source=data["source"],
            process_every_n_frames=int(data.get("process_every_n_frames", 5)),
            inference_size=int(data.get("inference_size", 640)),
            confidence_threshold=float(data.get("confidence_threshold", 0.45)),
            nms_threshold=float(data.get("nms_threshold", 0.5)),
            target_class_ids=_class_ids(data.get("target_class_ids", "0")),
            enabled_plugins=_plugins(
                data.get("enabled_plugins", ["object_detection", "evidence_capture"])
            ),
            intrusion_zone_polygon=_polygon(data.get("intrusion_zone_polygon")),
            virtual_border_line=_border_line(data.get("virtual_border_line")),
            evidence_source_feature=str(
                data.get("evidence_source_feature", "object_detection")
            ),
            evidence_max_width=int(data.get("evidence_max_width", 1280)),
            evidence_jpeg_quality=int(data.get("evidence_jpeg_quality", 75)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.camera_id,
            "source": self.source,
            "process_every_n_frames": self.process_every_n_frames,
            "inference_size": self.inference_size,
            "confidence_threshold": self.confidence_threshold,
            "nms_threshold": self.nms_threshold,
            "target_class_ids": list(self.target_class_ids),
            "enabled_plugins": list(self.enabled_plugins),
            "intrusion_zone_polygon": list(self.intrusion_zone_polygon),
            "virtual_border_line": (
                [
                    [self.virtual_border_line[0], self.virtual_border_line[1]],
                    [self.virtual_border_line[2], self.virtual_border_line[3]],
                ]
                if self.virtual_border_line
                else None
            ),
            "evidence_source_feature": self.evidence_source_feature,
            "evidence_max_width": self.evidence_max_width,
            "evidence_jpeg_quality": self.evidence_jpeg_quality,
        }


def _validate(settings: CameraSettings) -> CameraSettings:
    if settings.process_every_n_frames < 1:
        raise ValueError("process_every_n_frames must be at least 1")
    if settings.inference_size < 32:
        raise ValueError("inference_size must be at least 32")
    if not 0.0 <= settings.confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be between 0 and 1")
    if not 0.0 <= settings.nms_threshold <= 1.0:
        raise ValueError("nms_threshold must be between 0 and 1")
    if settings.evidence_max_width < 32:
        raise ValueError("evidence_max_width must be at least 32")
    if not 1 <= settings.evidence_jpeg_quality <= 95:
        raise ValueError("evidence_jpeg_quality must be between 1 and 95")
    return settings


def apply_remote_camera_settings(
    settings: CameraSettings, values: dict[str, object]
) -> CameraSettings:
    """Validate a server command and return updated settings."""
    safe_values = {k: v for k, v in values.items() if k in REMOTE_SETTING_NAMES}
    updates: dict[str, object] = {}
    for name, value in safe_values.items():
        if name in {
            "process_every_n_frames",
            "evidence_max_width",
            "evidence_jpeg_quality",
        }:
            updates[name] = int(value)
        elif name in {"confidence_threshold", "nms_threshold"}:
            updates[name] = float(value)
        elif name == "target_class_ids":
            if not isinstance(value, list):
                raise ValueError("target_class_ids must be an array")
            updates[name] = frozenset(int(item) for item in value)
        elif name == "enabled_plugins":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError("enabled_plugins must be an array of plugin names")
            plugins = tuple(value)
            if not plugins or not set(plugins) <= REMOTE_PLUGIN_NAMES:
                raise ValueError("Server may enable only built-in plugins")
            updates[name] = plugins
        elif name == "intrusion_zone_polygon":
            updates[name] = _polygon(value)
        elif name == "evidence_source_feature":
            if not isinstance(value, str):
                raise ValueError("evidence_source_feature must be a string")
            updates[name] = value
        elif name == "virtual_border_line":
            if value is None:
                updates[name] = None
            else:
                updates[name] = _border_line(value)
    return _validate(replace(settings, **updates))
