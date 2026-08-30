"""Plugin loading and ordered per-frame feature execution."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import supervision as sv

from config.settings import Settings
from plugins.base import FeatureEvent, FrameContext, PluginServices, VisionPlugin
from plugins.evidence_capture import EvidenceCapturePlugin
from plugins.intrusion import IntrusionDetectionPlugin
from plugins.object_detection import ObjectDetectionPlugin
from plugins.object_tracking import ObjectTrackingPlugin
from plugins.virtual_border import VirtualBorderPlugin


BUILTIN_PLUGINS: dict[str, type[VisionPlugin]] = {
    "object_detection": ObjectDetectionPlugin,
    "object_tracking": ObjectTrackingPlugin,
    "intrusion_detection": IntrusionDetectionPlugin,
    "evidence_capture": EvidenceCapturePlugin,
    "virtual_border": VirtualBorderPlugin,
}


class PluginManager:
    """Load configured plugins once and run them in configuration order."""

    def __init__(self, settings: Settings, services: PluginServices) -> None:
        self._plugins = [
            self._load(plugin_spec, settings, services) for plugin_spec in settings.enabled_plugins
        ]

    def process(self, frame) -> tuple[sv.Detections, list[FeatureEvent]]:
        context = FrameContext(
            frame=frame,
            captured_at=datetime.now(UTC),
            detections=sv.Detections.empty(),
        )
        for plugin in self._plugins:
            context.events.extend(plugin.process(context))
        return context.detections, context.events

    @staticmethod
    def _load(plugin_spec: str, settings: Settings, services: PluginServices) -> VisionPlugin:
        plugin_class = BUILTIN_PLUGINS.get(plugin_spec)
        if plugin_class is None:
            module_name, separator, class_name = plugin_spec.partition(":")
            if not separator or not module_name or not class_name:
                available = ", ".join(BUILTIN_PLUGINS)
                raise ValueError(
                    f"Unknown plugin '{plugin_spec}'. Use a built-in plugin ({available}) "
                    "or an import path such as package.module:PluginClass."
                )
            module = importlib.import_module(module_name)
            plugin_class = getattr(module, class_name)
        plugin = plugin_class(settings, services)
        if not hasattr(plugin, "process") or not hasattr(plugin, "name"):
            raise TypeError(f"Plugin '{plugin_spec}' must define name and process(context)")
        return plugin
