# Configuration Reference

The edge AI pipeline reads configuration from two sources:

1. **Environment variables** (`.env`) for device-wide settings (Supabase credentials, model path, retry timings).
2. **Per-camera JSON files** in `cameras/` for camera-specific settings (RTSP URL, plugins, polygon, line, etc.).

Runtime overrides can be pushed by the server via Supabase Realtime (`device_settings` table) for a curated allow-list of fields.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DEVICE_ID` | `edge-device-unknown` | The device's slug (e.g. `edge-border-north-1`). Must match the `device_id` column in `devices`. |
| `SUPABASE_URL` | (required) | Your project URL, e.g. `https://abc.supabase.co`. |
| `API_KEY` | (required) | Supabase anon/publishable key. |
| `DEVICE_EMAIL` | (required) | Virtual email used for `sign_in_with_password` (e.g. `edge-001@devices.ibvap.internal`). |
| `DEVICE_PASSWORD` | (required) | Password provisioned when the device was registered. |
| `API_URL` | `dummy` | Legacy FastAPI endpoint. No longer used; left for backward compatibility. |
| `MODEL_PATH` | `models/yolo26n.onnx` | Path to the ONNX model file. |
| `QUEUE_PATH` | `data/outbox.jsonl` | Local durable outbox file. |
| `RECONNECT_DELAY_SECONDS` | `3` | Delay before reconnecting to a dropped RTSP stream. |
| `REQUEST_TIMEOUT_SECONDS` | `5` | HTTP timeout for outgoing Supabase calls. |
| `QUEUE_MAX_RECORDS` | `1000` | Max outbox rows before oldest are dropped (FIFO). |
| `SEND_EMPTY_DETECTIONS` | `false` | If true, send alerts even when no objects were detected. |
| `SHOW_PREVIEW` | `false` | Display annotated OpenCV window. Press `Q` or `Esc` to stop. |
| `ENABLE_SENDING` | `true` | If false, run inference locally but never POST to Supabase. |
| `CONTROL_RECONNECT_SECONDS` | `5` | Delay before resubscribing to Supabase Realtime after a drop. |

## Per-Camera JSON (`cameras/*.json`)

Each file describes one camera. Filename does not need to match `id`. Required keys: `id`, `source`. All other keys have defaults.

```json
{
  "id": "496b6c20-5fa2-4932-a934-463da216d706",
  "source": "rtsp://user:pass@192.168.1.100:8554/stream",
  "process_every_n_frames": 5,
  "inference_size": 640,
  "confidence_threshold": 0.45,
  "nms_threshold": 0.5,
  "target_class_ids": [0],
  "enabled_plugins": ["object_detection", "object_tracking", "virtual_border", "evidence_capture"],
  "intrusion_zone_polygon": [[100, 100], [1180, 100], [1180, 620], [100, 620]],
  "zones": [
    {"name": "front_gate", "polygon": [[0.10, 0.20], [0.45, 0.20], [0.45, 0.65], [0.10, 0.65]], "target_class_ids": [0], "min_count": 1},
    {"name": "back_door",  "polygon": [[0.60, 0.55], [0.90, 0.55], [0.90, 0.90], [0.60, 0.90]], "target_class_ids": [0, 2], "min_count": 1}
  ],
  "zone_id_map": {"front_gate": "11111111-1111-1111-1111-111111111111"},
  "virtual_border_line": [[0.38, 0.57], [0.71, 0.36]],
  "evidence_source_feature": "virtual_border",
  "evidence_max_width": 1280,
  "evidence_jpeg_quality": 75,
  "latitude": 30.9010,
  "longitude": 75.8573,
  "cooldown_seconds": 5.0,
  "severity": "critical"
}
```

Notes:

- `virtual_border_line` and `intrusion_zone_polygon` accept **normalized coordinates in `[0.0, 1.0]`**. They are scaled to the actual frame resolution on first frame. Absolute pixel coordinates also work but only at the configured resolution.
- `zones` is the multi-zone replacement for `intrusion_zone_polygon`. Each entry: `name` (unique per camera), `polygon` (≥ 3 points, normalized), optional `target_class_ids` (defaults to `[0]`), optional `min_count` (defaults to 1, must be ≥ 1). When `zones` is set, `intrusion_zone_polygon` is ignored. When only `intrusion_zone_polygon` is set, the plugin behaves as before with a single default-named zone.
- `zone_id_map` maps `zone.name` → server-side `zones.id` uuid. The edge stamps `zone_name` on every event; the sender resolves it to `zone_id` using this map and writes it to `detections.zone_id`. Populate this from the operator UI after creating rows in the `zones` table. Empty map → `zone_id` is `NULL`.
- `target_class_ids` is a JSON array of COCO class indices (e.g. `0` for person, `2` for car, `7` for truck). Defaults to `[0]` if omitted.
- `enabled_plugins` accepts the built-in names listed below. `object_detection` and `object_tracking` are auto-loaded when spatial plugins are enabled, so you don't need to list them explicitly.
- `latitude` / `longitude` are pushed to `cameras.coordinates` on boot. The `fn_sync_camera_coords()` trigger back-fills `detections.camera_coords` and `alerts.camera_coords` automatically.
- `cooldown_seconds` controls per-tracker dedup on the alert sender (default 5s; 0 to disable).
- `severity` is one of `info`, `warning`, `critical` (default `critical`). Written to `alerts.severity` for high-severity features.

## Remote Settings (Hot Reload)

The server can push updates to these fields through Supabase Realtime (`device_settings` table). Anything outside this list is rejected client-side.

- `process_every_n_frames` (int)
- `inference_size` (int, ≥ 32)
- `confidence_threshold` (float, 0.0–1.0)
- `nms_threshold` (float, 0.0–1.0)
- `target_class_ids` (array of int)
- `enabled_plugins` (array of plugin names)
- `intrusion_zone_polygon` (array of `[x, y]`) — legacy, prefer `zones`
- `zones` (array of `{name, polygon, target_class_ids?, min_count?}`)
- `zone_id_map` (object `{name: uuid}`) — pushed by server after creating `zones` rows
- `virtual_border_line` (two `[x, y]` points or `null`)
- `evidence_source_feature` (string)
- `evidence_max_width` (int, ≥ 32)
- `evidence_jpeg_quality` (int, 1–95)
- `latitude` (float, -90..90), `longitude` (float, -180..180) — must be set together
- `cooldown_seconds` (float, ≥ 0)
- `severity` (string, one of `info`/`warning`/`critical`)

On every successful push, `ControlReceiver` rebuilds the affected `PluginManager` instance so the new thresholds/zones take effect without restarting the camera loop.
