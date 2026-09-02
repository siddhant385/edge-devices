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
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | Interval between device-online pings. Must be ≥ 10. |

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
  "virtual_border_line": [[0.38, 0.57], [0.71, 0.36]],
  "evidence_source_feature": "virtual_border",
  "evidence_max_width": 1280,
  "evidence_jpeg_quality": 75
}
```

Notes:

- `virtual_border_line` and `intrusion_zone_polygon` accept **normalized coordinates in `[0.0, 1.0]`**. They are scaled to the actual frame resolution on first frame. Absolute pixel coordinates also work but only at the configured resolution.
- `target_class_ids` is a JSON array of COCO class indices (e.g. `0` for person, `2` for car, `7` for truck). Defaults to `[0]` if omitted.
- `enabled_plugins` accepts the built-in names listed below. `object_detection` and `object_tracking` are auto-loaded when spatial plugins are enabled, so you don't need to list them explicitly.

## Remote Settings (Hot Reload)

The server can push updates to these fields through Supabase Realtime (`device_settings` table). Anything outside this list is rejected client-side.

- `process_every_n_frames` (int)
- `inference_size` (int, ≥ 32)
- `confidence_threshold` (float, 0.0–1.0)
- `nms_threshold` (float, 0.0–1.0)
- `target_class_ids` (array of int)
- `enabled_plugins` (array of plugin names)
- `intrusion_zone_polygon` (array of `[x, y]`)
- `virtual_border_line` (two `[x, y]` points or `null`)
- `evidence_source_feature` (string)
- `evidence_max_width` (int, ≥ 32)
- `evidence_jpeg_quality` (int, 1–95)

On every successful push, `ControlReceiver` rebuilds the affected `PluginManager` instance so the new thresholds/zones take effect without restarting the camera loop.
