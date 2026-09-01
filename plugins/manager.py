"""Plugin loading and ordered per-frame feature execution."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import supervision as sv

from config.camera_settings import CameraSettings
from plugins.base import (FeatureEvent, FrameContext, PluginServices,
                          VisionPlugin)
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

    def __init__(self, settings: CameraSettings, services: PluginServices) -> None:
        self.settings = settings
        
        # 1. Start with the plugins explicitly requested by the user
        plugins_to_load = set(settings.enabled_plugins)
        
        # 2. Dependency Management:
        # Spatial plugins REQUIRE object detection (and sometimes tracking) to function.
        # We silently load them to do the math, even if the user didn't ask for their events.
        requires_detection = {"virtual_border", "intrusion_detection", "object_tracking"}
        if requires_detection.intersection(plugins_to_load):
            plugins_to_load.add("object_detection")

        # 3. Ensure strict execution order (Detection MUST run before Border/Intrusion)
        execution_order = [
            "object_detection",
            "object_tracking",
            "virtual_border",
            "intrusion_detection",
            "evidence_capture"
        ]
        
        # Sort the specs safely
        ordered_specs = [p for p in execution_order if p in plugins_to_load]
        ordered_specs.extend([p for p in plugins_to_load if p not in execution_order])

        # 4. Store BOTH the spec name and the loaded plugin instance as a tuple
        self._plugins = [
            (spec, self._load(spec, settings, services)) for spec in ordered_specs
        ]

    def process(self, frame) -> tuple[sv.Detections, list[FeatureEvent]]:
        context = FrameContext(
            frame=frame,
            captured_at=datetime.now(UTC),
            detections=sv.Detections.empty(),
        )
        
        for spec, plugin in self._plugins:
            # The plugin runs and does its math (populating context.detections)
            plugin_events = plugin.process(context)
            
            # --- EVENT FILTERING ---
            # Only keep the events if this plugin was EXPLICITLY enabled in settings.
            # If it was silently loaded as a dependency (like object_detection for virtual_border),
            # its events are dropped!
            if spec in self.settings.enabled_plugins:
                context.events.extend(plugin_events)
                
        return context.detections, context.events

    @staticmethod
    def _load(plugin_spec: str, settings: CameraSettings, services: PluginServices) -> VisionPlugin:
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
