# Configuration Reference

The edge AI pipeline uses environment variables (or `.env` files) as its primary configuration source, supplemented by real-time settings pushed from the central server. The configuration applies to everything from camera ingestion to local model inference and HTTP sending.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DEVICE_ID` | `edge-device-unknown` | Unique identifier for this physical edge device. |
| `CAMERA_ID` | `camera-unknown` | Identifier for the single camera when `CAMERAS` is not set. |
| `CAMERA_SOURCE` | (empty string) | RTSP URL or device index (e.g. `0`) for the single camera when `CAMERAS` is not set. |
| `CAMERAS` | (unset) | JSON array overriding single camera config (see CAMERAS JSON). |
| `API_URL` | (required) | Central server endpoint where alert JSON payloads are posted. |
| `API_KEY` | (unset) | Optional bearer token sent in the `Authorization` header. |
| `MODEL_PATH` | `models/yolo26n.onnx` | Path to the `.onnx` inference model file. |
| `QUEUE_PATH` | `data/outbox.jsonl` | Local file used for the durable offline queue. |
| `PROCESS_EVERY_N_FRAMES` | `5` | Skips frames to reduce CPU load (e.g., `5` means process 1 out of 5 frames). |
| `INFERENCE_SIZE` | `640` | Resize resolution dimension for the vision model (must be >= 32). |
| `CONFIDENCE_THRESHOLD` | `0.45` | Minimum confidence score (0.0 to 1.0) to retain a detection. |
| `NMS_THRESHOLD` | `0.5` | Non-maximum suppression IoU threshold (0.0 to 1.0) for overlapping bounding boxes. |
| `TARGET_CLASS_IDS` | `0,2,3,5,7` | Comma-separated list of COCO class IDs (e.g. `0`=person) to detect. |
| `RECONNECT_DELAY_SECONDS` | `3.0` | Delay before attempting to reconnect to a dropped camera stream. |
| `REQUEST_TIMEOUT_SECONDS` | `5.0` | Timeout for `POST`ing alert payloads to the `API_URL`. |
| `QUEUE_MAX_RECORDS` | `1000` | Maximum number of alert JSON lines to store offline before dropping the oldest. |
| `SEND_EMPTY_DETECTIONS` | `false` | If true, sends alert payloads even when no target objects are detected. |
| `SHOW_PREVIEW` | `false` | Displays an annotated OpenCV window (requires desktop environment). Use `false` with multiple cameras. |
| `ENABLE_SENDING` | `true` | If false, processes frames and updates preview but drops all alerts instead of sending them. |
| `ENABLED_PLUGINS` | `object_detection` | Comma-separated list of plugin modules to run in sequence. |
| `INTRUSION_ZONE_POLYGON` | (empty) | JSON array of `[x, y]` points for the `intrusion_detection` plugin zone. |
| `VIRTUAL_BORDER_LINE` | (empty) | JSON array of exactly two `[x, y]` points for the `virtual_border` plugin. |
| `EVIDENCE_SOURCE_FEATURE` | `object_detection` | Target feature event for the `evidence_capture` plugin to attach cropped JPEGs to. |
| `EVIDENCE_MAX_WIDTH` | `1280` | Maximum pixel width for evidence JPEGs. Preserves aspect ratio. |
| `EVIDENCE_JPEG_QUALITY` | `75` | JPEG quality score (1-95) for evidence images. |
| `CONTROL_URL` | (unset) | Central server Server-Sent Events (SSE) endpoint for live setting updates. |
| `CONTROL_RECONNECT_SECONDS` | `5.0` | Delay before retrying an SSE connection after a failure. |
| `HEARTBEAT_URL` | (unset) | Central server endpoint to `POST` device health metrics. |
| `HEARTBEAT_INTERVAL_SECONDS` | `60.0` | Interval between health metric posts. |

## CAMERAS JSON Configuration

When processing multiple cameras concurrently, use the `CAMERAS` environment variable containing a JSON array. Each object requires an `id` and `source` string:

```json
[
  {
    "id": "gate-1-cam",
    "source": "rtsp://192.168.1.100:554/stream"
  },
  {
    "id": "gate-2-cam",
    "source": "rtsp://192.168.1.101:554/stream"
  }
]
```

When `CAMERAS` is defined, `CAMERA_ID` and `CAMERA_SOURCE` are ignored, and `SHOW_PREVIEW` cannot be `true`.

## Remote Settings List

A central server connected via the `CONTROL_URL` SSE endpoint can dynamically push updates that override local configuration for the following specific settings (other local settings like device IDs, paths, or URLs remain unalterable):

*   `process_every_n_frames` (integer)
*   `confidence_threshold` (float)
*   `nms_threshold` (float)
*   `target_class_ids` (array of integers)
*   `send_empty_detections` (boolean)
*   `enabled_plugins` (array of strings, e.g., `["object_detection", "object_tracking"]`)
*   `intrusion_zone_polygon` (array of `[x,y]` coordinates)
*   `evidence_source_feature` (string)
*   `evidence_max_width` (integer)
*   `evidence_jpeg_quality` (integer)
*   `virtual_border_line` (array of two `[x,y]` coordinates or `null`)

The edge validates all incoming payloads and will ignore configuration changes that contain invalid inputs or attempt to update local-only configuration fields.