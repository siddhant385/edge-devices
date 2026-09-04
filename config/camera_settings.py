"""Camera specific AI pipeline settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import NamedTuple

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
        "cooldown_seconds",
        "severity",
        "zones",
        "zone_id_map",
    }
)

# Fields whose owner is the edge device, set at install time in the local
# camera JSON. The cloud can store values for display but must never push
# them back - sync_with_cloud() filters them out at merge time. Use this
# frozenset everywhere you need to distinguish "physical, edge-owned"
# from "tuning, cloud-pushable".
PHYSICAL_FIELDS = frozenset(
    {
        "source",          # RTSP URL of the camera
        "latitude",        # GPS latitude (set at install)
        "longitude",       # GPS longitude (set at install)
        "location",        # human-readable location label
    }
)

TUNING_FIELDS = REMOTE_SETTING_NAMES | frozenset({"inference_size"})


class NamedZone(NamedTuple):
    """One named intrusion zone for a camera.

    `polygon` uses normalized (0..1) coordinates by convention; the intrusion
    plugin scales it to the actual frame resolution on first use. Pass absolute
    pixel coords if you want - the plugin auto-detects on first frame
    (any coordinate > 1.0 is treated as absolute).
    """

    name: str
    polygon: tuple[tuple[float, float], ...]
    target_class_ids: tuple[int, ...] = (0,)
    min_count: int = 1


def _class_ids(value: str | list[int]) -> frozenset[int]:
    if isinstance(value, list):
        return frozenset(value)
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


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


def _zone(value: dict) -> NamedZone:
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("zone.name must be a non-empty string")
    raw_polygon = value.get("polygon")
    if not isinstance(raw_polygon, list) or len(raw_polygon) < 3:
        raise ValueError(
            f"zone '{name}' polygon must be an array of at least three [x, y] points"
        )
    polygon = tuple((float(p[0]), float(p[1])) for p in raw_polygon)
    target = value.get("target_class_ids", (0,))
    if not isinstance(target, (list, tuple)):
        raise ValueError(f"zone '{name}' target_class_ids must be an array")
    target_t = tuple(int(t) for t in target)
    min_count = int(value.get("min_count", 1))
    if min_count < 1:
        raise ValueError(f"zone '{name}' min_count must be >= 1")
    return NamedZone(name=name, polygon=polygon, target_class_ids=target_t, min_count=min_count)


def _zones(value: object) -> tuple[NamedZone, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("zones must be an array")
    return tuple(_zone(item) for item in value)


def _zone_id_map(value: object) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("zone_id_map must be an object")
    return {str(k): str(v) for k, v in value.items()}


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
    latitude: float | None = None
    longitude: float | None = None
    cooldown_seconds: float = 5.0
    severity: str = "critical"
    zones: tuple[NamedZone, ...] = ()
    zone_id_map: dict[str, str] = field(default_factory=dict)
    location: str | None = None

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
            latitude=_opt_float(data.get("latitude")),
            longitude=_opt_float(data.get("longitude")),
            cooldown_seconds=float(data.get("cooldown_seconds", 5.0)),
            severity=str(data.get("severity", "critical")),
            zones=_zones(data.get("zones")),
            zone_id_map=_zone_id_map(data.get("zone_id_map")),
            location=(str(data["location"]) if data.get("location") else None),
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
            "latitude": self.latitude,
            "longitude": self.longitude,
            "cooldown_seconds": self.cooldown_seconds,
            "severity": self.severity,
            "zones": [
                {
                    "name": z.name,
                    "polygon": [list(p) for p in z.polygon],
                    "target_class_ids": list(z.target_class_ids),
                    "min_count": z.min_count,
                }
                for z in self.zones
            ],
            "zone_id_map": dict(self.zone_id_map),
            "location": self.location,
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
    if settings.latitude is not None and not -90.0 <= settings.latitude <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if settings.longitude is not None and not -180.0 <= settings.longitude <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    if (settings.latitude is None) != (settings.longitude is None):
        raise ValueError("latitude and longitude must be set together or both omitted")
    if settings.cooldown_seconds < 0.0:
        raise ValueError("cooldown_seconds must be >= 0")
    if settings.severity not in {"info", "warning", "critical"}:
        raise ValueError("severity must be one of: info, warning, critical")
    zone_names = [z.name for z in settings.zones]
    if len(zone_names) != len(set(zone_names)):
        raise ValueError("zone names must be unique within a camera")
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
        elif name in {"latitude", "longitude"}:
            updates[name] = _opt_float(value)
        elif name == "cooldown_seconds":
            updates[name] = float(value)
        elif name == "severity":
            if not isinstance(value, str):
                raise ValueError("severity must be a string")
            updates[name] = value
        elif name == "zones":
            updates[name] = _zones(value)
        elif name == "zone_id_map":
            updates[name] = _zone_id_map(value)
        elif name == "location":
            updates[name] = (str(value) if value else None)
    return _validate(replace(settings, **updates))
