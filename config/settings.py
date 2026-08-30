"""Environment-backed settings for the edge pipeline."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


REMOTE_PLUGIN_NAMES = frozenset(
    {"object_detection", "object_tracking", "intrusion_detection", "evidence_capture", "virtual_border"}
)
REMOTE_SETTING_NAMES = frozenset(
    {
        "process_every_n_frames",
        "confidence_threshold",
        "nms_threshold",
        "target_class_ids",
        "send_empty_detections",
        "enabled_plugins",
        "intrusion_zone_polygon",
        "evidence_source_feature",
        "evidence_max_width",
        "evidence_jpeg_quality",
        "virtual_border_line",
    }
)

def _class_ids(value: str) -> frozenset[int]:
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


def _plugins(value: str) -> tuple[str, ...]:
    plugins = tuple(item.strip() for item in value.split(",") if item.strip())
    if not plugins:
        raise ValueError("ENABLED_PLUGINS must include at least one plugin")
    return plugins


def _polygon(value: str) -> tuple[tuple[int, int], ...]:
    if not value:
        return ()
    points = json.loads(value)
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("INTRUSION_ZONE_POLYGON must be a JSON array of at least three [x, y] points")
    return tuple((int(point[0]), int(point[1])) for point in points)


def _border_line(value: str) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    points = json.loads(value)
    if not isinstance(points, list) or len(points) != 2:
        raise ValueError("VIRTUAL_BORDER_LINE must be a JSON array of two [x, y] points")
    return (int(points[0][0]), int(points[0][1]), int(points[1][0]), int(points[1][1]))


@dataclass(frozen=True, slots=True)
class CameraSettings:
    """Identity and source for one independently processed camera."""

    camera_id: str
    source: str


def _validate(settings: "Settings") -> "Settings":
    if settings.process_every_n_frames < 1:
        raise ValueError("PROCESS_EVERY_N_FRAMES must be at least 1")
    if settings.inference_size < 32:
        raise ValueError("INFERENCE_SIZE must be at least 32")
    if not 0.0 <= settings.confidence_threshold <= 1.0:
        raise ValueError("CONFIDENCE_THRESHOLD must be between 0 and 1")
    if not 0.0 <= settings.nms_threshold <= 1.0:
        raise ValueError("NMS_THRESHOLD must be between 0 and 1")
    if settings.show_preview and len(settings.cameras) > 1:
        raise ValueError("SHOW_PREVIEW supports one camera only; use headless mode for CAMERAS")
    if settings.evidence_max_width < 32:
        raise ValueError("EVIDENCE_MAX_WIDTH must be at least 32")
    if not 1 <= settings.evidence_jpeg_quality <= 95:
        raise ValueError("EVIDENCE_JPEG_QUALITY must be between 1 and 95")
    return settings


def _cameras(value: str, camera_id: str, camera_source: str) -> tuple[CameraSettings, ...]:
    if not value:
        if not camera_source:
            raise ValueError("CAMERA_SOURCE must be set when CAMERAS is not configured")
        return (CameraSettings(camera_id=camera_id, source=camera_source),)
    items = json.loads(value)
    if not isinstance(items, list) or not items:
        raise ValueError("CAMERAS must be a non-empty JSON array")
    cameras = tuple(
        CameraSettings(camera_id=str(item["id"]), source=str(item["source"])) for item in items
    )
    if any(not camera.camera_id or not camera.source for camera in cameras):
        raise ValueError("Every CAMERAS entry requires non-empty id and source values")
    if len({camera.camera_id for camera in cameras}) != len(cameras):
        raise ValueError("Each camera in CAMERAS must have a unique id")
    return cameras


@dataclass(frozen=True, slots=True)
class Settings:
    device_id: str
    camera_id: str
    camera_source: str
    api_url: str
    api_key: str | None
    model_path: Path
    queue_path: Path
    process_every_n_frames: int
    inference_size: int
    confidence_threshold: float
    nms_threshold: float
    target_class_ids: frozenset[int]
    reconnect_delay_seconds: float
    request_timeout_seconds: float
    queue_max_records: int
    send_empty_detections: bool
    show_preview: bool
    enable_sending: bool
    enabled_plugins: tuple[str, ...]
    intrusion_zone_polygon: tuple[tuple[int, int], ...]
    virtual_border_line: tuple[int, int, int, int] | None
    cameras: tuple[CameraSettings, ...]
    evidence_source_feature: str
    evidence_max_width: int
    evidence_jpeg_quality: int
    control_url: str | None
    control_reconnect_seconds: float
    heartbeat_url: str | None
    heartbeat_interval_seconds: float
    supabase_url: str
    device_email: str
    device_password: str

    @classmethod
    def from_environment(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        load_dotenv(root / ".env")
        camera_source = os.getenv("CAMERA_SOURCE", "")
        api_url = os.getenv("API_URL", "dummy")
        if not api_url:
            raise ValueError("API_URL must be set")
        camera_id = os.getenv("CAMERA_ID", "camera-unknown")

        settings = cls(
            device_id=os.getenv("DEVICE_ID", "edge-device-unknown"),
            camera_id=camera_id,
            camera_source=camera_source,
            api_url=api_url,
            api_key=os.getenv("API_KEY") or None,
            model_path=Path(os.getenv("MODEL_PATH", root / "models" / "yolo26n.onnx")),
            queue_path=Path(os.getenv("QUEUE_PATH", root / "data" / "outbox.jsonl")),
            process_every_n_frames=int(os.getenv("PROCESS_EVERY_N_FRAMES", "5")),
            inference_size=int(os.getenv("INFERENCE_SIZE", "640")),
            confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.45")),
            nms_threshold=float(os.getenv("NMS_THRESHOLD", "0.5")),
            target_class_ids=_class_ids(os.getenv("TARGET_CLASS_IDS", "0,2,3,5,7")),
            reconnect_delay_seconds=float(os.getenv("RECONNECT_DELAY_SECONDS", "3")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5")),
            queue_max_records=int(os.getenv("QUEUE_MAX_RECORDS", "1000")),
            send_empty_detections=os.getenv("SEND_EMPTY_DETECTIONS", "false").lower() == "true",
            show_preview=os.getenv("SHOW_PREVIEW", "false").lower() == "true",
            enable_sending=os.getenv("ENABLE_SENDING", "true").lower() == "true",
            enabled_plugins=_plugins(os.getenv("ENABLED_PLUGINS", "object_detection")),
            intrusion_zone_polygon=_polygon(os.getenv("INTRUSION_ZONE_POLYGON", "")),
            virtual_border_line=_border_line(os.getenv("VIRTUAL_BORDER_LINE", "")),
            cameras=_cameras(os.getenv("CAMERAS", ""), camera_id, camera_source),
            evidence_source_feature=os.getenv("EVIDENCE_SOURCE_FEATURE", "object_detection"),
            evidence_max_width=int(os.getenv("EVIDENCE_MAX_WIDTH", "1280")),
            evidence_jpeg_quality=int(os.getenv("EVIDENCE_JPEG_QUALITY", "75")),
            control_url=os.getenv("CONTROL_URL") or None,
            control_reconnect_seconds=float(os.getenv("CONTROL_RECONNECT_SECONDS", "5")),
            heartbeat_url=os.getenv("HEARTBEAT_URL") or None,
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "60")),
            supabase_url=os.getenv("SUPABASE_URL", ""),
            device_email=os.getenv("DEVICE_EMAIL", ""),
            device_password=os.getenv("DEVICE_PASSWORD", ""),
        )
        if not settings.supabase_url:
            raise ValueError("SUPABASE_URL must be set")
        if not settings.device_email:
            raise ValueError("DEVICE_EMAIL must be set")
        if not settings.device_password:
            raise ValueError("DEVICE_PASSWORD must be set")
        if settings.control_reconnect_seconds < 1:
            raise ValueError("CONTROL_RECONNECT_SECONDS must be at least 1")
        if settings.heartbeat_interval_seconds < 10:
            raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be at least 10")
        return _validate(settings)


def apply_remote_settings(settings: Settings, values: dict[str, object]) -> Settings:
    """Validate a server command without allowing device-owned settings to change."""
    unknown = set(values) - REMOTE_SETTING_NAMES
    if unknown:
        raise ValueError(f"Server attempted to change local-only settings: {', '.join(sorted(unknown))}")
    updates: dict[str, object] = {}
    for name, value in values.items():
        if name in {"process_every_n_frames", "evidence_max_width", "evidence_jpeg_quality"}:
            updates[name] = int(value)
        elif name in {"confidence_threshold", "nms_threshold"}:
            updates[name] = float(value)
        elif name == "target_class_ids":
            if not isinstance(value, list):
                raise ValueError("target_class_ids must be an array")
            updates[name] = frozenset(int(item) for item in value)
        elif name == "send_empty_detections":
            if not isinstance(value, bool):
                raise ValueError("send_empty_detections must be a boolean")
            updates[name] = value
        elif name == "enabled_plugins":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("enabled_plugins must be an array of plugin names")
            plugins = tuple(value)
            if not plugins or not set(plugins) <= REMOTE_PLUGIN_NAMES:
                raise ValueError("Server may enable only built-in plugins")
            updates[name] = plugins
        elif name == "intrusion_zone_polygon":
            updates[name] = _polygon(json.dumps(value))
        elif name == "evidence_source_feature":
            if not isinstance(value, str):
                raise ValueError("evidence_source_feature must be a string")
            updates[name] = value
        elif name == "virtual_border_line":
            if value is None:
                updates[name] = None
            else:
                updates[name] = _border_line(json.dumps(value))
    return _validate(replace(settings, **updates))
