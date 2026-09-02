# Plugins Architecture

The edge AI pipeline runs each frame through an ordered plug-in chain. The order is fixed and defined by `PluginManager` (`plugins/manager.py`): **object_detection → object_tracking → virtual_border → intrusion_detection → evidence_capture**. Spatial plugins auto-load `object_detection` and `object_tracking` as silent dependencies when the user enables them.

## FrameContext

`FrameContext` (in `plugins/base.py`) is a mutable per-frame object passed sequentially through the chain. Each plugin reads and mutates it.

| Field | Type | Description |
|---|---|---|
| `frame` | `np.ndarray` (BGR) | The raw camera frame. |
| `captured_at` | `datetime` (UTC) | Capture timestamp. |
| `detections` | `sv.Detections` | Accumulates detections across plugins. |
| `events` | `list[FeatureEvent]` | Per-plugin outputs that may be sent to the server. |

`FeatureEvent` holds a feature name (e.g. `"virtual_border"`), an `sv.Detections` subset, and an optional `evidence_jpeg` byte string populated by `evidence_capture`.

## Built-in Plugins

### 1. `object_detection`

Runs ONNX inference on the frame. The shared `OnnxProcessor` (one per device, used by all cameras under a lock) handles letterboxing, NMS, and class filtering.

- **Reads**: `frame`
- **Writes**: `detections` (full set)
- **Always emits**: an `object_detection` event (filtered out by `main.py` if not in `enabled_plugins`)
- **Config**: `inference_size`, `confidence_threshold`, `nms_threshold`, `target_class_ids`

### 2. `object_tracking`

Assigns persistent IDs via `trackers.ByteTrackTracker`. The `trackers` package replaces the deprecated `sv.ByteTrack`.

- **Reads**: previous `detections`
- **Writes**: `detections` with `tracker_id` populated
- **Auto-loaded** with any spatial plugin

### 3. `intrusion_detection`

Filters detections to only those whose bottom-center anchor lies inside an `sv.PolygonZone`.

- **Reads**: tracked `detections`
- **Writes**: a new event with the in-zone subset
- **Config**: `intrusion_zone_polygon` (required, ≥ 3 points)

### 4. `virtual_border`

Counts objects crossing an `sv.LineZone`. Crossing direction depends on the geometry of the line vector — the line's `(start → end)` direction defines what counts as `in_count` vs `out_count` on the `LineZoneAnnotator`.

- **Reads**: tracked `detections` (requires `tracker_id`)
- **Writes**: a new event with crossed detections
- **Config**: `virtual_border_line` (required, two `[x, y]` points, normalized or absolute)

### 5. `evidence_capture`

JPEG-encodes the full frame once per spatial event, applies a 30 s per-tracker debounce and a 3600 px² minimum bbox gate, and attaches the bytes to the chosen event.

- **Reads**: prior events in `context.events` (prefers spatial events; falls back to `evidence_source_feature`)
- **Writes**: mutates one existing `FeatureEvent.evidence_jpeg`
- **Config**: `evidence_source_feature`, `evidence_max_width`, `evidence_jpeg_quality`

## Preview Annotation

Every plugin can optionally implement `annotate_preview(scene)` to draw on the local preview window. `PluginManager.annotate_preview` calls it on each plugin in order. Built-ins:

- `virtual_border`: draws the line and the live in/out counters (`sv.LineZoneAnnotator`).
- `intrusion_detection`: draws the polygon outline and the in-zone count (`sv.PolygonZoneAnnotator`).

Plugins without an `annotate_preview` method are silently skipped (`getattr` with default `None`).

## Writing a Custom Plugin

Create a Python module that defines a class implementing `VisionPlugin` (`plugins/base.py`):

```python
import supervision as sv
from plugins.base import FeatureEvent, FrameContext, PluginServices


class LprPlugin:
    name = "license_plate_reader"

    def __init__(self, settings, services: PluginServices) -> None:
        # Load your own ONNX model, etc.
        pass

    def process(self, context: FrameContext) -> list[FeatureEvent]:
        if context.detections.class_id is None:
            return []
        # Filter to vehicles (car=2, truck=7, bus=5)
        vehicles = context.detections[
            np.isin(context.detections.class_id, [2, 5, 7])
        ]
        if len(vehicles) == 0:
            return []
        return [FeatureEvent(self.name, vehicles)]

    def annotate_preview(self, scene):
        # Optional: draw your own overlays here
        return scene
```

Register it via `module:ClassName` syntax:

```json
{
  "enabled_plugins": ["object_detection", "object_tracking", "my_pkg.lpr:LprPlugin"]
}
```

The plugin must be importable from the working directory where `main.py` runs.
