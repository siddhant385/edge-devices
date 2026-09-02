# IBVAP Edge Client

CPU-only RTSP-to-Supabase edge AI pipeline for surveillance cameras. It samples frames, runs an ordered plug-in pipeline using ONNX Runtime's `CPUExecutionProvider`, Roboflow `supervision`, and the `trackers` ByteTrack implementation, then dispatches alerts and JPEG evidence directly to a Supabase project (PostgREST, Storage, and Realtime). Failed deliveries are retained in a bounded local JSONL outbox and retried on the next tick.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration Reference](docs/configuration.md)
- [Plugins](docs/plugins.md)
- [Resilience](docs/resilience.md)
- [Threading and Concurrency](docs/threading.md)
- [API Contracts](docs/api-contracts.md)
- [Deployment](docs/deployment.md)
- [Termux (Android) Guide](docs/TERMUX_GUIDE.md)
- [Central Server Schema](server.md)
- [Edge-to-Supabase Direct Integration Plan](edge.md)

## Quick Start

### Local (uv)

```bash
git clone <repo> ibvap-edge && cd ibvap-edge
uv sync
cp .env.example .env             # fill in Supabase URL/keys/device credentials
mkdir -p cameras models
cp <your-yolov8n.onnx> models/yolo26n.onnx
echo '{"id":"cam-1","source":"rtsp://user:pass@host/stream"}' > cameras/cam-1.json
uv run python main.py
```

### Docker

```bash
docker build -t ibvap-edge .
docker compose up -d
docker compose logs -f
```

See [docs/deployment.md](docs/deployment.md) for full instructions including Docker, systemd, and Termux.

## Cameras

Each camera has its own JSON file under `cameras/`. The filename does not have to match the `id`; the loader scans `cameras/*.json` at startup and registers whatever it finds. Camera configs are also synced to Supabase on first run so the dashboard can render them.

```json
{
  "id": "496b6c20-5fa2-4932-a934-463da216d706",
  "source": "rtsp://192.168.31.175:8554/mystream",
  "process_every_n_frames": 5,
  "inference_size": 640,
  "confidence_threshold": 0.45,
  "nms_threshold": 0.5,
  "target_class_ids": [0],
  "enabled_plugins": ["virtual_border", "evidence_capture"],
  "intrusion_zone_polygon": [],
  "virtual_border_line": [[0.38, 0.57], [0.71, 0.36]],
  "evidence_source_feature": "object_detection",
  "evidence_max_width": 1280,
  "evidence_jpeg_quality": 75
}
```

- `virtual_border_line` and `intrusion_zone_polygon` accept **normalized** `[0.0, 1.0]` coordinates. They are scaled to the camera's actual frame size on first frame.
- `enabled_plugins` accepts any combination of the built-ins below. Spatial plugins (`virtual_border`, `intrusion_detection`) auto-load `object_detection` and `object_tracking` as dependencies.

## Built-in Plugins

| Plugin | Purpose | Config |
| --- | --- | --- |
| `object_detection` | ONNX CPU inference | `inference_size`, `confidence_threshold`, `nms_threshold`, `target_class_ids` |
| `object_tracking` | Persistent IDs via ByteTrack | (no config; auto-loaded with spatial plugins) |
| `intrusion_detection` | `sv.PolygonZone` filtering | `intrusion_zone_polygon` |
| `virtual_border` | `sv.LineZone` crossings | `virtual_border_line` |
| `evidence_capture` | Attaches one JPEG per crossing event | `evidence_source_feature`, `evidence_max_width`, `evidence_jpeg_quality` |

External plugins are loadable via `module:ClassName` import paths. Custom plugins receive `CameraSettings` and `PluginServices` and must implement `name`, `process(context)`, and optionally `annotate_preview(scene)` to draw on the OpenCV preview window.

## Alert Pipeline

On a spatial event (`virtual_border` or `intrusion_detection`) with evidence:

1. `EvidenceCapturePlugin` JPEG-encodes the frame (downscaled to `evidence_max_width`) and stamps it onto the `FeatureEvent`.
2. `AlertSender` (`core/cloud/sender.py`) uploads the JPEG to the `evidence` Storage bucket at `<device_id>/<camera_id>/<YYYY-MM-DD>/<uuid>.jpg`.
3. Detection rows are inserted into the `detections` table. The first row in a batch carries `evidence_path`; subsequent rows reference the same path implicitly via `tracker_id`.
4. Critical-severity rows are inserted into the `alerts` table with `severity='critical'`, `status='unacknowledged'`, and a FK to the originating `detection_id`.

Detection rows for non-spatial features (`object_detection`, `object_tracking`) are also inserted but do not auto-create alerts.

## Preview

Set `SHOW_PREVIEW=true` to display annotated frames in an OpenCV window. The line/polygon overlays are drawn by each plugin's `annotate_preview` hook through `PluginManager.annotate_preview`. Press `Q` or `Esc` to stop.

Set `ENABLE_SENDING=false` for a local visual test that never POSTs to Supabase.

## Requirements

- Python 3.12+
- ONNX-compatible model (default path: `models/yolo26n.onnx`)
- `uv` for dependency management
- Supabase project (URL + anon/publishable key + device credentials)
- A reachable camera (RTSP URL, HTTP MJPEG, or `0` for local webcam)

See [docs/configuration.md](docs/configuration.md) for every environment variable and [docs/deployment.md](docs/deployment.md) for Docker, systemd, and Termux instructions.
