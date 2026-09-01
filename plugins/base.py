"""Contracts shared by all edge-vision plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Protocol

import numpy as np
import supervision as sv

from config.camera_settings import CameraSettings
from core.ai.processor import OnnxProcessor


@dataclass(frozen=True, slots=True)
class PluginServices:
    """Shared, resource-intensive services supplied to every pipeline."""

    processor: OnnxProcessor
    inference_lock: Lock


@dataclass(slots=True)
class FeatureEvent:
    """A feature-specific detection batch ready for alert delivery."""

    feature: str
    detections: sv.Detections
    evidence_jpeg: bytes | None = None


@dataclass(slots=True)
class FrameContext:
    """Mutable frame state passed through ordered feature plugins."""

    frame: np.ndarray
    captured_at: datetime
    detections: sv.Detections
    events: list[FeatureEvent] = field(default_factory=list)


class VisionPlugin(Protocol):
    """A plugin loaded by name or as an importable ``module:Class`` path."""

    name: str

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None: ...

    def process(self, context: FrameContext) -> list[FeatureEvent]: ...
