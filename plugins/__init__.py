"""Pluggable edge-vision features."""

from plugins.base import FeatureEvent, PluginServices, VisionPlugin
from plugins.manager import PluginManager

__all__ = ("FeatureEvent", "PluginManager", "PluginServices", "VisionPlugin")
