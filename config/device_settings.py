"""Environment-backed device settings for the edge pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

@dataclass(frozen=True, slots=True)
class DeviceSettings:
    device_id: str
    api_url: str
    api_key: str | None
    model_path: Path
    queue_path: Path
    reconnect_delay_seconds: float
    request_timeout_seconds: float
    queue_max_records: int
    send_empty_detections: bool
    show_preview: bool
    enable_sending: bool
    control_url: str | None
    control_reconnect_seconds: float
    heartbeat_url: str | None
    heartbeat_interval_seconds: float
    supabase_url: str
    device_email: str
    device_password: str

    @classmethod
    def from_environment(cls) -> "DeviceSettings":
        root = Path(__file__).resolve().parent.parent
        load_dotenv(root / ".env")
        api_url = os.getenv("API_URL", "dummy")
        if not api_url:
            raise ValueError("API_URL must be set")

        settings = cls(
            device_id=os.getenv("DEVICE_ID", "edge-device-unknown"),
            api_url=api_url,
            api_key=os.getenv("API_KEY") or None,
            model_path=Path(os.getenv("MODEL_PATH", root / "models" / "yolo26n.onnx")),
            queue_path=Path(os.getenv("QUEUE_PATH", root / "data" / "outbox.jsonl")),
            reconnect_delay_seconds=float(os.getenv("RECONNECT_DELAY_SECONDS", "3")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5")),
            queue_max_records=int(os.getenv("QUEUE_MAX_RECORDS", "1000")),
            send_empty_detections=os.getenv("SEND_EMPTY_DETECTIONS", "false").lower() == "true",
            show_preview=os.getenv("SHOW_PREVIEW", "false").lower() == "true",
            enable_sending=os.getenv("ENABLE_SENDING", "true").lower() == "true",
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
        return settings
