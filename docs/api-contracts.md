# API Contracts

This document outlines the standard JSON payloads exchanged between the edge AI pipeline and the central server.

## 1. Alert Payload (Edge -> Server)

When actionable features are detected, the edge sends an alert payload to `API_URL`.

**Method:** `POST`
**Headers:** `Authorization: Bearer <API_KEY>`, `Content-Type: application/json`

**Example Payload:**

```json
{
  "device_id": "edge-001",
  "camera_id": "gate-1-cam",
  "timestamp": "2023-10-27T10:15:30.123456Z",
  "frame_sequence": 1450,
  "features": [
    {
      "type": "object_detection",
      "data": {
        "class_id": [0, 2],
        "confidence": [0.89, 0.76],
        "xyxy": [
          [100.0, 150.0, 200.0, 300.0],
          [400.0, 200.0, 500.0, 400.0]
        ],
        "tracker_id": null
      },
      "metadata": {},
      "evidence": "/9j/4AAQSkZJRgABAQ..."
    },
    {
      "type": "virtual_border_crossing",
      "data": {
         "class_id": [0],
         "confidence": [0.89],
         "xyxy": [[100.0, 150.0, 200.0, 300.0]],
         "tracker_id": [42]
      },
      "metadata": {"crossed_direction": "in"},
      "evidence": null
    }
  ]
}
```

*   `features[].data`: A dictionary representation of a Roboflow `sv.Detections` object. Arrays align by index.
*   `features[].evidence`: A base64-encoded JPEG image string, or `null`.

## 2. Heartbeat Payload (Edge -> Server)

The edge sends periodic health metrics to `HEARTBEAT_URL` if configured.

**Method:** `POST`
**Headers:** `Authorization: Bearer <API_KEY>`, `Content-Type: application/json`

**Example Payload:**

```json
{
  "device_id": "edge-001",
  "timestamp": "2023-10-27T10:15:30.123456Z",
  "metrics": {
    "cpu_percent": 45.2,
    "memory_percent": 62.1,
    "temperature_c": 58.5,
    "queue_depth": 14,
    "active_cameras": ["gate-1-cam", "gate-2-cam"]
  }
}
```

*   `temperature_c`: Read from `/sys/class/thermal/thermal_zone0/temp` (Linux only). Returns `0.0` if unavailable.

## 3. SSE Protocol Payload (Server -> Edge)

The central server pushes live configuration updates to the edge via the Server-Sent Events endpoint (`CONTROL_URL`).

**Method:** `GET`
**Headers:** `Authorization: Bearer <API_KEY>`, `Accept: text/event-stream`

**Example Event Stream:**

```text
event: settings
data: {"version": "v1.2", "settings": {"confidence_threshold": 0.6, "process_every_n_frames": 10}}
```

*   The edge only processes events named `settings`.
*   The JSON payload in `data` must contain a `settings` object mapping valid remote configuration keys to their new values. Any omitted keys retain their local values.