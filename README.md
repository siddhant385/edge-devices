# IBVAP Edge Client

CPU-only edge pipeline for RTSP surveillance cameras and local webcams. It samples frames, runs an ordered plug-in pipeline using ONNX Runtime's `CPUExecutionProvider` and Roboflow Supervision, and POSTs alert batches to a central API. Failed deliveries are stored in a bounded local JSONL outbox and retried before new alerts.

## Documentation
- [Termux Android Guide](docs/TERMUX_GUIDE.md) - **NEW:** Learn how to run this node on an Android smartphone!
- [Edge Architecture Overview](edge.md)
- [Central Server Architecture](server.md)

## Setup

1. Place a COCO-compatible YOLO Nano ONNX detection model at `models/yolo26n.onnx` (YOLOv11n recommended for better edge efficiency).
2. Copy `.env.example` to `.env`. Set `CAMERA_SOURCE=0` for the inbuilt webcam, or use an RTSP URL. Add your Supabase credentials.
3. Install dependencies with `uv sync` or `pip install -r requirements.txt`.
4. Start the client with `python main.py`.

The default `object_detection` plug-in filters to COCO person (`0`). Tune frame sampling and thresholds in `.env` for the target device.

## Feature Plugins

Set `ENABLED_PLUGINS` to an ordered comma-separated list. Each plug-in receives the same frame; later built-ins receive the `sv.Detections` output of earlier ones.

| Plugin | Purpose | Configuration |
| --- | --- | --- |
| `object_detection` | CPU ONNX object detection | `MODEL_PATH`, detection thresholds, `TARGET_CLASS_IDS` |
| `object_tracking` | Adds persistent Supervision ByteTrack IDs | Place after `object_detection` |
| `intrusion_detection` | Emits objects inside a Supervision polygon zone | Place after detection/tracking and set `INTRUSION_ZONE_POLYGON` |
| `evidence_capture` | Attaches one JPEG to a preceding detection event | Place after the event named by `EVIDENCE_SOURCE_FEATURE` |

For example, enable detection, tracking, and intrusion alerts with:

```dotenv
ENABLED_PLUGINS=object_detection,object_tracking,intrusion_detection
INTRUSION_ZONE_POLYGON=[[100,100],[1180,100],[1180,620],[100,620]]
EVIDENCE_SOURCE_FEATURE=intrusion_detection
EVIDENCE_MAX_WIDTH=1280
EVIDENCE_JPEG_QUALITY=75
```

External plug-ins are regular installed Python classes configured as `package.module:ClassName`. Their constructor receives `Settings` and `PluginServices`, and they must define a `name` plus `process(context) -> list[FeatureEvent]`. This lets face recognition and licence-plate ONNX models ship independently while reusing the receiver, outbox, sender, annotations, and shared `sv.Detections` state.

The built-ins live in the root-level `plugins/` package. `core/` contains reusable infrastructure only: RTSP reception, CPU ONNX inference, and resilient alert delivery. Custom plugins receive `Settings` and `PluginServices`; the latter exposes the single shared ONNX processor and its inference lock.

## Multiple Cameras

For one camera, continue using `CAMERA_ID` and `CAMERA_SOURCE`. To run several cameras, use a `CAMERAS` JSON array instead; it takes precedence over those single-camera values.

```dotenv
CAMERAS=[{"id":"north-gate","source":"rtsp://192.168.31.175:8554/"},{"id":"south-gate","source":"rtsp://192.168.31.176:8554/"}]
SHOW_PREVIEW=false
```

Each camera runs in its own worker with its own plugin instances. This keeps tracking IDs and zone state isolated per camera. The ONNX model is loaded once and inference is locked, bounding CPU and memory use on edge hardware; alerts retain the originating camera ID.

## Evidence Uploads

Add `evidence_capture` after the feature that should trigger a server-side face or ANPR check. It JPEG-encodes only frames containing detections for `EVIDENCE_SOURCE_FEATURE`, downscales them to `EVIDENCE_MAX_WIDTH`, and adds the JPEG as base64 to the relevant alert record. This retains the existing durable JSONL retry behavior when connectivity drops.

The alert API must accept `detections[].evidence` when present:

```json
{
  "content_type": "image/jpeg",
  "jpeg_base64": "..."
}
```

During testing the detected class names and confidence scores are logged to the terminal. If the configured API is unavailable, alert payloads are retained at `data/outbox.jsonl` for a future retry.

Set `SHOW_PREVIEW=true` for desktop testing to show Supervision-drawn bounding boxes and labels. Press `Q` or `Esc` in the preview window to stop. Leave it disabled on a headless edge device.

Set `ENABLE_SENDING=false` for a local visual test without posting alerts to the configured API.

## Alert Format

```json
{
  "device_id": "edge-001",
  "camera_id": "border-camera-01",
  "timestamp": "2026-08-25T12:00:00+00:00",
  "detections": [
    {"feature": "object_detection", "class_id": 0, "class_name": "person", "confidence": 0.93, "bbox_xyxy": [24.1, 80.0, 160.4, 422.9]}
  ]
}
```
