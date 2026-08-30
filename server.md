# IBVAP Central Server — Architecture & Specification

## 1. System Overview

The IBVAP Central Server is the command-and-control hub for a distributed network of edge AI surveillance devices deployed in border areas. Each edge device runs the IBVAP edge client, which connects to up to 5 IP cameras, performs local object detection via ONNX, and pushes structured alert payloads to this server. The server receives alerts, runs secondary AI processing (face recognition, vehicle ANPR), stores everything in a database, pushes real-time configuration to edge devices via SSE, and serves a web frontend for operators.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CENTRAL SERVER                              │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │ Alert    │  │ SSE      │  │ AI       │  │ Frontend           │  │
│  │ Ingestion│  │ Config   │  │ Modules  │  │ (Next.js)          │  │
│  │ API      │  │ Pusher   │  │ Face/ANPR│  │                    │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬───────────┘  │
│       │              │             │                  │              │
│       └──────────────┴─────────────┴──────────────────┘              │
│                              │                                       │
│                     ┌────────┴────────┐                              │
│                     │   Supabase      │                              │
│                     │   PostgreSQL    │                              │
│                     │   + Storage     │                              │
│                     │   + Realtime    │                              │
│                     └─────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘
         ▲               │
         │ POST alerts   │ SSE settings
         │               ▼
   ┌──────────┐    ┌──────────┐    ┌──────────┐
   │ Edge     │    │ Edge     │    │ Edge     │
   │ Device 1 │    │ Device 2 │    │ Device N │
   │ (1-5 cam)│    │ (1-5 cam)│    │ (1-5 cam)│
   └──────────┘    └──────────┘    └──────────┘
```

---

## 2. Why Supabase

| Requirement | Supabase Feature |
|---|---|
| Structured alert storage with relational queries | PostgreSQL with full SQL, indexes, partitioning |
| Evidence image storage (JPEG frames) | Supabase Storage (S3-compatible, signed URLs) |
| Real-time dashboard updates | Supabase Realtime (Postgres changes → WebSocket) |
| Authentication for operators and API keys | Supabase Auth (JWT, row-level security) |
| Edge functions for lightweight processing | Supabase Edge Functions (Deno) |
| Self-hostable for air-gapped deployments | Fully open-source stack |

Supabase is the right choice here. The alternative would be a self-managed PostgreSQL + MinIO + custom WebSocket server, which is essentially rebuilding what Supabase provides out of the box. For a border surveillance system that may need air-gapped deployment, Supabase's self-hosting capability is critical.

---

## 3. Server Tech Stack

| Component | Technology | Rationale |
|---|---|---|
| API Framework | **FastAPI** (Python) | Async, SSE native support, shares Python AI ecosystem |
| Database | **Supabase PostgreSQL** | Relational + realtime + auth + storage |
| Image Storage | **Supabase Storage** | S3-compatible, signed URLs, lifecycle policies |
| AI Processing | **ONNX Runtime** (CPU) or GPU if available | Face recognition, ANPR models |
| Task Queue | **PostgreSQL LISTEN/NOTIFY** or **Redis + Celery** | Async AI processing pipeline |
| Frontend | **Next.js 15** + **Tailwind CSS** + **shadcn/ui** | SSR, real-time subscriptions via Supabase client |
| Realtime | **Supabase Realtime** | Postgres changes pushed to frontend via WebSocket |
| Deployment | **Docker Compose** | Single-command deployment, self-hostable |

---

## 4. Database Schema

### 4.1 `devices`

Registered edge devices.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key, auto-generated |
| `device_id` | `text UNIQUE NOT NULL` | Matches edge `DEVICE_ID` (e.g., `edge-001`) |
| `auth_user_id` | `uuid REFERENCES auth.users(id)` | The Supabase Auth User ID for RLS / Edge login |
| `name` | `text` | Human-readable label |
| `location` | `text` | Deployment site description |
| `coordinates` | `point` | GPS lat/lon of the device |
| `api_key_hash` | `text` | DEPRECATED: bcrypt hash of the device's API key |
| `is_online` | `boolean DEFAULT false` | Updated by Supabase Realtime / Postgres Cron |
| `last_seen_at` | `timestamptz` | Last heartbeat timestamp. Checked by cron for offline detection. |
| `created_at` | `timestamptz DEFAULT now()` | |
| `settings_version` | `text DEFAULT ''` | Current pushed config version |

### 4.2 `cameras`

Cameras attached to each device.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `device_id` | `uuid REFERENCES devices(id)` | |
| `camera_id` | `text NOT NULL` | Matches edge `camera_id` field in alerts |
| `name` | `text` | Human-readable label |
| `source_url` | `text` | RTSP URL (stored encrypted or redacted) |
| `is_online` | `boolean DEFAULT false` | Indicates if the camera stream is currently active |
| `created_at` | `timestamptz DEFAULT now()` | |
| UNIQUE | `(device_id, camera_id)` | |

### 4.3 `alerts`

Every alert payload from every edge device.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `device_id` | `uuid REFERENCES devices(id)` | |
| `camera_id` | `uuid REFERENCES cameras(id)` | Resolved from device + camera_id string |
| `timestamp` | `timestamptz NOT NULL` | From the edge alert payload |
| `received_at` | `timestamptz DEFAULT now()` | Server receive time |
| `detection_count` | `int` | Number of detections in this alert |
| `has_evidence` | `boolean DEFAULT false` | Quick filter flag |
| `evidence_path` | `text` | Supabase Storage path to the JPEG |
| `raw_payload` | `jsonb` | Complete original alert JSON |
| `processed` | `boolean DEFAULT false` | Whether AI modules have processed it |
| `severity` | `alert_severity` | Enum: `info`, `warning`, `critical` (default `info`) |
| `status` | `alert_status` | Enum: `unacknowledged`, `investigating`, `resolved`, `false_positive` (default `unacknowledged`) |
| `operator_id` | `uuid REFERENCES auth.users(id)` | User who handled/updated the alert |
| `acknowledged_at` | `timestamptz` | When status changed to investigating or resolved |
| `resolved_at` | `timestamptz` | When status changed to resolved or false_positive |

**Database Triggers on Alerts:**
- **Auto-Severity Escalation**: An `AFTER INSERT` trigger on `detections` automatically escalates the parent alert `severity` to `warning` if an intrusion zone detection is found, or `critical` if a virtual border cross is found. AI Workers can further escalate this to `critical` upon a Watchlist Match.
- **Auto-Timestamps**: A `BEFORE UPDATE` trigger on `alerts` automatically records `acknowledged_at` and `resolved_at` based on changes to the `status` enum, and writes to `audit_log` with the provided `operator_id`.

### 4.4 `detections`

Individual detected objects, one row per bounding box.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `alert_id` | `uuid REFERENCES alerts(id)` | |
| `feature` | `text NOT NULL` | Plugin name: `object_detection`, `intrusion_detection`, `virtual_border` |
| `class_id` | `int` | COCO class index |
| `class_name` | `text` | COCO label |
| `confidence` | `real` | 0.0–1.0 |
| `bbox_xyxy` | `real[4]` | `[x1, y1, x2, y2]` |
| `tracker_id` | `int` | Nullable — present only if tracking was active |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.5 `face_results`

Server-side face recognition results.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `alert_id` | `uuid REFERENCES alerts(id)` | |
| `detection_id` | `uuid REFERENCES detections(id)` | The person detection that was cropped |
| `face_embedding` | `vector(512)` | pgvector embedding for similarity search |
| `matched_identity_id` | `uuid REFERENCES known_faces(id)` | Nullable — null if unknown |
| `similarity_score` | `real` | Cosine similarity to matched face |
| `face_crop_path` | `text` | Storage path to cropped face image |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.6 `known_faces`

Watchlist of known persons.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `name` | `text NOT NULL` | |
| `description` | `text` | |
| `face_embedding` | `vector(512)` | Reference embedding (pgvector) |
| `reference_image_path` | `text` | Storage path |
| `threat_level` | `text` | `low`, `medium`, `high`, `critical` |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.7 `anpr_results`

Server-side ANPR (license plate) results.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `alert_id` | `uuid REFERENCES alerts(id)` | |
| `detection_id` | `uuid REFERENCES detections(id)` | The vehicle detection that was cropped |
| `plate_text` | `text` | Recognized plate number |
| `plate_confidence` | `real` | OCR confidence |
| `plate_crop_path` | `text` | Storage path to cropped plate image |
| `is_flagged` | `boolean DEFAULT false` | Matches a watchlist plate |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.8 `watchlist_plates`

Known/flagged vehicle plates.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `plate_text` | `text UNIQUE NOT NULL` | Normalized plate number |
| `description` | `text` | Reason for flagging |
| `threat_level` | `text` | `low`, `medium`, `high`, `critical` |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.9 `device_settings`

Server-managed runtime configuration per device.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `device_id` | `uuid REFERENCES devices(id) UNIQUE` | One active config per device |
| `version` | `text NOT NULL` | Unique version string (e.g., UUID or timestamp hash) |
| `settings` | `jsonb NOT NULL` | The settings object pushed via SSE |
| `created_by` | `uuid REFERENCES auth.users(id)` | Operator who made the change |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.10 `audit_log`

Every configuration change, login, and manual action.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `user_id` | `uuid REFERENCES auth.users(id)` | |
| `action` | `text NOT NULL` | `settings_updated`, `device_registered`, `face_added`, etc. |
| `target_type` | `text` | `device`, `camera`, `known_face`, `watchlist_plate` |
| `target_id` | `uuid` | |
| `details` | `jsonb` | Before/after or context |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.11 `system_events`

Tracks hardware health and connection status separate from AI vision alerts.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key |
| `device_id` | `uuid REFERENCES devices(id)` | Associated device |
| `camera_id` | `uuid REFERENCES cameras(id)` | Associated camera (nullable if device-level event) |
| `event_type` | `text` | e.g., `device_offline`, `camera_offline` |
| `severity` | `alert_severity` | Enum: `info`, `warning`, `critical` (default `info`) |
| `status` | `alert_status` | Enum: `unacknowledged`, `investigating`, `resolved`, `false_positive` |
| `operator_id` | `uuid REFERENCES auth.users(id)` | User who acknowledged/resolved |
| `acknowledged_at` | `timestamptz` | When operator acknowledged |
| `resolved_at` | `timestamptz` | When issue was fixed |
| `created_at` | `timestamptz DEFAULT now()` | |

**Database Triggers on Hardware Health:**
- **Auto-Alert Creation**: `AFTER UPDATE` triggers on `devices` and `cameras` monitor the `is_online` column. If it flips from `true` to `false`, a new `critical` severity `system_events` record is instantly generated.
- **Auto-Resolution**: If the `is_online` status flips from `false` back to `true`, the triggers locate any outstanding `unacknowledged` or `investigating` offline events for that hardware and automatically update their status to `resolved`. This prevents manual cleanup when intermittent network drops fix themselves.

```sql
CREATE INDEX idx_alerts_device_time ON alerts (device_id, timestamp DESC);
CREATE INDEX idx_alerts_unprocessed ON alerts (processed) WHERE processed = false;
CREATE INDEX idx_detections_alert ON detections (alert_id);
CREATE INDEX idx_detections_feature ON detections (feature, created_at DESC);
CREATE INDEX idx_face_results_identity ON face_results (matched_identity_id);
CREATE INDEX idx_anpr_plate ON anpr_results (plate_text);
CREATE INDEX idx_known_faces_embedding ON known_faces USING ivfflat (face_embedding vector_cosine_ops);
```

### Partitioning

The `alerts` and `detections` tables should be partitioned by month on `timestamp`/`created_at` to maintain query performance as the system scales (border surveillance generates continuous data 24/7).

---

## 5. API Endpoints

### 5.1 Edge Device APIs

#### `POST /api/v1/detections` — Alert Ingestion

Receives alert payloads from edge devices.

**Request:**
```
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

**Body** (exactly as the edge sends it):
```json
{
  "device_id": "edge-001",
  "camera_id": "border-camera-01",
  "timestamp": "2026-08-27T10:30:00.123456+00:00",
  "detections": [
    {
      "feature": "object_detection",
      "class_id": 0,
      "class_name": "person",
      "confidence": 0.8712,
      "bbox_xyxy": [100.0, 200.5, 300.2, 400.0],
      "tracker_id": 42,
      "evidence": {
        "content_type": "image/jpeg",
        "jpeg_base64": "/9j/4AAQ..."
      }
    }
  ]
}
```

**Server Processing Pipeline:**

1. **Authenticate** — Verify `Authorization: Bearer` token against `devices.api_key_hash`.
2. **Validate** — Check required fields, resolve `device_id` string → `devices.id`, auto-register `camera_id` if new.
3. **Extract evidence** — If any detection has `evidence.jpeg_base64`:
   - Decode base64 → raw JPEG bytes.
   - Upload to Supabase Storage at `evidence/{device_id}/{camera_id}/{date}/{alert_id}.jpg`.
   - Store the storage path in `alerts.evidence_path`.
   - Strip `evidence` from the JSON before storing in `raw_payload` (avoid double-storage).
4. **Insert** — Write to `alerts` table + individual rows in `detections` table.
5. **Queue AI** — If detections contain `class_id=0` (person) or `class_id=2,3,5,7` (vehicles), enqueue for face/ANPR processing.
6. **Respond** — Return `201 Created` with `{"alert_id": "<uuid>"}`. Any non-2xx triggers edge-side outbox queuing.

**Response Codes:**

| Code | Meaning |
|---|---|
| `201` | Alert accepted and stored |
| `400` | Malformed payload |
| `401` | Invalid or missing API key |
| `404` | Unknown device_id (if auto-registration is disabled) |
| `429` | Rate limited |
| `500` | Server error (edge will queue and retry) |

#### `GET /api/v1/control/sse?device_id=<id>` — SSE Configuration Stream

Persistent SSE connection for pushing runtime configuration changes to edge devices.

**Request:**
```
Accept: text/event-stream
Authorization: Bearer <API_KEY>
X-Device-ID: <DEVICE_ID>
```

**Server Behavior:**

1. **Authenticate** — Verify bearer token and device ID.
2. **Mark online** — Set `devices.is_online = true`, update `last_seen_at`.
3. **Send current config** — Immediately push the current `device_settings` as the first SSE event.
4. **Stream changes** — Use PostgreSQL `LISTEN/NOTIFY` on `device_settings` changes. When an operator updates settings via the frontend, the server pushes a new SSE event.
5. **Heartbeat** — Send a comment line (`: heartbeat`) every 30 seconds to keep the connection alive and detect dead connections.
6. **Disconnect** — Mark `devices.is_online = false` when the connection drops.

**SSE Event Format:**
```
: heartbeat

event: settings
data: {"version": "v4-2026-08-27T10:30:00Z", "settings": {"confidence_threshold": 0.6, "enabled_plugins": ["object_detection", "object_tracking", "virtual_border", "evidence_capture"], "virtual_border_line": [[0, 360], [1280, 360]]}}

```

**Controllable Settings (pushed via SSE):**

| Setting | JSON Type | Constraints | Description |
|---|---|---|---|
| `process_every_n_frames` | `int` | ≥ 1 | Frame skip rate |
| `confidence_threshold` | `float` | 0.0–1.0 | Detection confidence filter |
| `nms_threshold` | `float` | 0.0–1.0 | Non-maximum suppression threshold |
| `target_class_ids` | `int[]` | COCO class IDs | Which object classes to detect |
| `send_empty_detections` | `bool` | | Send alerts even with zero detections |
| `enabled_plugins` | `string[]` | Non-empty, built-in names only | Active plugin pipeline |
| `intrusion_zone_polygon` | `[[x,y],...]` | ≥ 3 points | Polygon zone for intrusion detection |
| `virtual_border_line` | `[[x,y],[x,y]]` or `null` | 2 points or null | Line zone for border crossing detection |
| `evidence_source_feature` | `string` | Plugin name | Which plugin event gets JPEG evidence |
| `evidence_max_width` | `int` | ≥ 32 | Max evidence image width in pixels |
| `evidence_jpeg_quality` | `int` | 1–95 | JPEG compression quality |

**Settings the server CANNOT change** (device-owned, local only):
- `device_id`, `camera_id`, `camera_source`, `cameras`
- `api_url`, `api_key`, `control_url`
- `model_path`, `inference_size`
- `reconnect_delay_seconds`, `request_timeout_seconds`
- `queue_path`, `queue_max_records`
- `show_preview`, `enable_sending`
- `control_reconnect_seconds`

#### `POST /api/v1/devices/register` — Device Self-Registration

Optional endpoint for new edge devices to register themselves.

**Body:**
```json
{
  "device_id": "edge-003",
  "name": "North Border Post 3",
  "cameras": [
    {"camera_id": "cam-01", "source": "rtsp://..."}
  ]
}
```

**Response:** `201` with `{"api_key": "<generated-key>"}` (shown once, stored hashed).

#### `POST /api/v1/heartbeat` — Device Health Ping

Edge devices should send periodic heartbeats (every 60s) with system metrics.

**Body:**
```json
{
  "device_id": "edge-001",
  "cpu_percent": 62.5,
  "memory_percent": 44.2,
  "temperature_celsius": 58.3,
  "uptime_seconds": 86400,
  "queue_depth": 12,
  "cameras_active": 3
}
```

This data populates the dashboard health panel.

### 5.2 Frontend APIs

#### Devices & Cameras
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/devices` | List all devices with online status |
| `GET` | `/api/v1/devices/:id` | Device detail + cameras + current settings |
| `PUT` | `/api/v1/devices/:id/settings` | Update runtime settings (triggers SSE push) |
| `DELETE` | `/api/v1/devices/:id` | Decommission a device |

#### Alerts & Detections
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/alerts` | Paginated alert list with filters (device, camera, feature, time range, class) |
| `GET` | `/api/v1/alerts/:id` | Single alert with all detections, evidence URL, face/ANPR results |
| `GET` | `/api/v1/alerts/:id/evidence` | Redirect to signed Supabase Storage URL for the evidence JPEG |

#### AI Results
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/faces/matches` | Recent face recognition matches |
| `GET` | `/api/v1/faces/unknown` | Unmatched face crops for manual review |
| `POST` | `/api/v1/faces/known` | Add a person to the watchlist (upload reference photo) |
| `GET` | `/api/v1/anpr/results` | Recent plate recognitions |
| `POST` | `/api/v1/anpr/watchlist` | Add a plate to the watchlist |
| `GET` | `/api/v1/anpr/watchlist` | List flagged plates |

#### Dashboard & Analytics
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/dashboard/stats` | Aggregate counts (alerts/hour, detections by class, active devices) |
| `GET` | `/api/v1/dashboard/timeline` | Time-series data for charts |
| `GET` | `/api/v1/audit-log` | Paginated audit log |
| `GET` | `/api/v1/system-events` | Feed of hardware health alerts (offline cameras/devices) |

---

## 6. AI Processing Modules (Server-Side)

### 6.1 Face Recognition Pipeline

```
Alert received (class_id=0, person)
  → Crop bounding box from evidence JPEG
  → Run face detection model (RetinaFace/SCRFD, ONNX)
  → If face found:
      → Extract embedding (ArcFace, 512-dim, ONNX)
      → Search known_faces using pgvector cosine similarity
      → If similarity > 0.65: matched → write face_results with identity
      → If similarity < 0.65: unknown → write face_results, flag for review
      → Save cropped face to Storage
```

**Models (all ONNX, CPU-compatible):**
- Face detection: `scrfd_2.5g.onnx` (~3MB)
- Face embedding: `arcface_r50.onnx` (~166MB) or `mobilefacenet.onnx` (~5MB)

**Why server-side, not edge:** Face recognition requires a reference database and embedding search. The edge device has no access to the watchlist and insufficient memory for embedding models alongside YOLO.

### 6.2 ANPR Pipeline

```
Alert received (class_id=2,3,5,7 — car/motorcycle/bus/truck)
  → Crop vehicle bounding box from evidence JPEG
  → Run plate detection model (ONNX)
  → If plate found:
      → Run OCR model (ONNX, e.g., PaddleOCR or LPRNet)
      → Normalize plate text (strip spaces, uppercase)
      → Check against watchlist_plates
      → Write anpr_results, flag if matched
      → Save cropped plate to Storage
```

**Models (all ONNX):**
- Plate detection: `yolov8n-plate.onnx` (~6MB)
- Plate OCR: `lprnet.onnx` (~2MB) or PaddleOCR ONNX

### 6.3 Processing Architecture

AI processing should be **async and decoupled** from alert ingestion to avoid blocking the API:

```
POST /api/v1/detections
  → Insert alert + detections (fast, <50ms)
  → Return 201
  → Enqueue task: process_alert(alert_id)

Worker picks up task:
  → Download evidence JPEG from Storage
  → Run face pipeline for person detections
  → Run ANPR pipeline for vehicle detections
  → Write results to face_results / anpr_results
  → Mark alert.processed = true
  → Supabase Realtime notifies frontend
```

For the task queue, two options:
1. **Simple (recommended initially):** PostgreSQL `LISTEN/NOTIFY` + a Python worker process polling `alerts WHERE processed = false`.
2. **Scalable (later):** Redis + Celery workers, with horizontal scaling for GPU nodes.

---

## 7. Evidence Image Handling

### 7.1 Flow: Edge → Server → Storage

```
Edge:
  evidence_capture plugin → JPEG bytes (≤1280px wide, quality 75)
  sender.py → base64-encode → embed in alert JSON payload
  ↓
Server:
  POST /api/v1/detections receives JSON
  → base64-decode evidence.jpeg_base64 → raw bytes
  → Upload to Supabase Storage:
      bucket: "evidence"
      path:   "{device_id}/{camera_id}/{YYYY-MM-DD}/{alert_id}.jpg"
  → Store path in alerts.evidence_path
  → Strip base64 from raw_payload before storing (saves ~33% DB space)
  ↓
Frontend:
  GET /api/v1/alerts/:id/evidence
  → Server generates signed URL (1-hour expiry) → redirect
  → Browser loads image directly from Storage CDN
```

### 7.2 Storage Lifecycle

| Policy | Value | Rationale |
|---|---|---|
| Retention | 90 days default, configurable per deployment | Border regulations vary |
| Max file size | ~500KB (1280px JPEG at quality 75) | Edge already constrains this |
| Bucket policy | Private, signed URLs only | Evidence is sensitive |
| Cleanup | Cron job deletes expired evidence + cascades `evidence_path = NULL` | Prevents unbounded growth |

### 7.3 Evidence for AI Crops

When the face/ANPR pipeline crops sub-regions from evidence images, those crops are stored separately:

```
bucket: "ai-crops"
path:   "faces/{face_result_id}.jpg"
path:   "plates/{anpr_result_id}.jpg"
```

---

## 8. Frontend Architecture

### 8.1 Pages

| Page | Path | Description |
|---|---|---|
| **Dashboard** | `/` | Real-time overview: active devices map, alert rate charts, recent critical events |
| **Devices** | `/devices` | Grid/list of all edge devices with online status, camera count, last seen |
| **Device Detail** | `/devices/:id` | Camera list, settings editor, live alert feed, health metrics |
| **Camera Control** | `/devices/:id/cameras/:cam` | Detection zone/line editor (canvas overlay), plugin toggle, threshold sliders |
| **Alerts** | `/alerts` | Filterable, paginated alert timeline with evidence thumbnails |
| **Alert Detail** | `/alerts/:id` | Full evidence image, all detections annotated, face/ANPR results |
| **Faces** | `/faces` | Known faces watchlist manager, unknown face review queue |
| **Vehicles** | `/vehicles` | ANPR results, watchlist plate manager |
| **Settings** | `/settings` | Global server settings, user management, retention policies |
| **Audit Log** | `/audit` | Complete action history |

### 8.2 Camera Control Panel (Key Feature)

The camera control page is the primary operator interface for configuring edge detection:

```
┌──────────────────────────────────────────────┐
│  Camera: border-camera-01 (edge-001)         │
│  Status: ● Online    FPS: ~2                 │
├──────────────────────────────────────────────┤
│                                              │
│   ┌──────────────────────────────────┐       │
│   │                                  │       │
│   │   [Last evidence frame]         │       │
│   │                                  │       │
│   │   ──── virtual border line ──── │       │
│   │                                  │       │
│   │   ┌─ ─ ─ ─ ─ ─ ─ ┐            │       │
│   │   │ intrusion zone │            │       │
│   │   └─ ─ ─ ─ ─ ─ ─ ┘            │       │
│   │                                  │       │
│   └──────────────────────────────────┘       │
│   [Draw Zone] [Draw Border] [Clear]         │
│                                              │
├──────────────────────────────────────────────┤
│  Plugins                                     │
│  ☑ Object Detection                         │
│  ☑ Object Tracking                          │
│  ☑ Virtual Border        line: [0,360]→[1280,360] │
│  ☐ Intrusion Detection   zone: not set       │
│  ☑ Evidence Capture       source: object_detection │
│                                              │
├──────────────────────────────────────────────┤
│  Detection Settings                          │
│  Confidence:  [====●=====] 0.45              │
│  NMS:         [====●=====] 0.50              │
│  Frame Skip:  [====●=====] 5                 │
│  Classes:     [person] [car] [motorcycle]    │
│                                              │
├──────────────────────────────────────────────┤
│  Evidence Settings                           │
│  Max Width:   [1280] px                      │
│  JPEG Quality:[====●=====] 75                │
│                                              │
│  [Save & Push to Device]                     │
└──────────────────────────────────────────────┘
```

**Interactive Drawing:** Operators draw the intrusion polygon and virtual border line directly on the last evidence frame using a canvas overlay. The coordinates are pixel values relative to the camera's frame resolution. On save, the server:
1. Writes to `device_settings` with a new version.
2. PostgreSQL `NOTIFY` triggers the SSE push to the edge device.
3. The edge rebuilds its `PluginManager` with the new settings within one frame cycle.

### 8.3 Real-Time Updates

The frontend subscribes to Supabase Realtime channels:

| Channel | Event | UI Update |
|---|---|---|
| `alerts` | `INSERT` | New alert appears in dashboard feed, counter increments |
| `devices` | `UPDATE` | Online/offline status changes, last_seen updates |
| `face_results` | `INSERT` | Face match notification (critical if watchlist match) |
| `anpr_results` | `INSERT` | Plate match notification |

---

## 9. Authentication & Security

### 9.1 Edge Device Authentication

| Aspect | Implementation |
|---|---|
| Method | `Authorization: Bearer <API_KEY>` on every request |
| Key storage | Edge: `.env` file (gitignored). Server: bcrypt hash in `devices.api_key_hash` |
| Key rotation | Admin generates new key via frontend → old key invalidated → new key shown once |
| Rate limiting | Per-device: 100 alerts/minute (configurable). Prevents runaway edge from flooding |

### 9.2 Operator Authentication

| Aspect | Implementation |
|---|---|
| Method | Supabase Auth (email/password, optional SSO/SAML) |
| Roles | `admin` (full access), `operator` (view + configure devices), `viewer` (read-only) |
| Row-Level Security | Supabase RLS policies restrict data access by role |
| Sessions | JWT tokens, 1-hour expiry, refresh tokens |

### 9.3 Data Security

- All communication over HTTPS/TLS.
- Evidence images are private — accessed only via time-limited signed URLs.
- RTSP URLs stored encrypted at rest (contain camera credentials).
- Audit log captures every configuration change with before/after diffs.
- No PII in logs — face embeddings are not reversible to images.

---

## 10. Deployment Architecture

### 10.1 Docker Compose Services

```yaml
services:
  api:
    # FastAPI server
    # Handles alert ingestion, SSE, frontend API
    # Ports: 8000

  worker:
    # AI processing worker
    # Consumes unprocessed alerts
    # Runs face/ANPR pipelines
    # Can be scaled horizontally

  frontend:
    # Next.js production build
    # Ports: 3000

  supabase-db:
    # PostgreSQL 15 + pgvector
    # Ports: 5432

  supabase-storage:
    # S3-compatible object storage
    # Ports: 5000

  supabase-realtime:
    # WebSocket server for Postgres changes
    # Ports: 4000

  supabase-auth:
    # Authentication service
    # Ports: 9999

  redis:
    # Optional: task queue backend
    # Ports: 6379
```

### 10.2 Scaling Considerations

| Component | Scaling Strategy |
|---|---|
| Alert ingestion | Horizontal — multiple API instances behind a load balancer |
| AI workers | Horizontal — each worker processes independently from the queue |
| Database | Vertical initially, read replicas for dashboard queries |
| Storage | Supabase Storage auto-scales (S3 backend) |
| SSE connections | Each API instance holds N device connections. With 100 devices, a single instance is sufficient. At 1000+, use sticky sessions or a dedicated SSE service |

### 10.3 Resource Estimates

| Edge Devices | Alerts/min | Storage/day | DB Rows/day | Server CPU |
|---|---|---|---|---|
| 10 (50 cameras) | ~600 | ~18 GB | ~600K | 4 cores |
| 50 (250 cameras) | ~3,000 | ~90 GB | ~3M | 8 cores |
| 100 (500 cameras) | ~6,000 | ~180 GB | ~6M | 16 cores |

*Assumes 1 alert every 5 seconds per active camera, ~500KB evidence per alert.*

---

## 11. Configuration Settings Reference

### 11.1 Server Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | Supabase PostgreSQL connection string |
| `SUPABASE_URL` | — | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | — | Supabase service role key (server-side only) |
| `SUPABASE_ANON_KEY` | — | Supabase anon key (frontend) |
| `STORAGE_BUCKET_EVIDENCE` | `evidence` | Bucket name for evidence images |
| `STORAGE_BUCKET_CROPS` | `ai-crops` | Bucket name for AI crops |
| `EVIDENCE_RETENTION_DAYS` | `90` | Auto-delete evidence after N days |
| `FACE_MODEL_PATH` | `models/arcface_r50.onnx` | Face embedding model |
| `FACE_DETECTION_MODEL_PATH` | `models/scrfd_2.5g.onnx` | Face detection model |
| `FACE_MATCH_THRESHOLD` | `0.65` | Cosine similarity threshold |
| `ANPR_DETECTION_MODEL_PATH` | `models/yolov8n-plate.onnx` | Plate detection model |
| `ANPR_OCR_MODEL_PATH` | `models/lprnet.onnx` | Plate OCR model |
| `SSE_HEARTBEAT_SECONDS` | `30` | SSE keepalive interval |
| `RATE_LIMIT_PER_DEVICE` | `100` | Max alerts per minute per device |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed frontend origins |
| `LOG_LEVEL` | `INFO` | Logging level |

### 11.2 Default Device Settings (pushed on first connect)

```json
{
  "version": "default-v1",
  "settings": {
    "process_every_n_frames": 5,
    "confidence_threshold": 0.45,
    "nms_threshold": 0.5,
    "target_class_ids": [0, 2, 3, 5, 7],
    "send_empty_detections": false,
    "enabled_plugins": ["object_detection", "evidence_capture"],
    "intrusion_zone_polygon": [],
    "virtual_border_line": null,
    "evidence_source_feature": "object_detection",
    "evidence_max_width": 1280,
    "evidence_jpeg_quality": 75
  }
}
```

---

## 12. Architecture Assessment & Recommendations

### What's Right

1. **Edge-server split is correct.** Heavy detection (YOLO) runs on edge, specialized AI (face/ANPR) runs on server where the watchlist database lives. This minimizes bandwidth — only evidence frames and compact JSON cross the network.

2. **SSE for config push is correct.** Unidirectional server→edge. Lower overhead than WebSockets, simpler than MQTT, auto-reconnects natively. Perfect for the "push settings" use case.

3. **Plugin architecture is correct.** The edge pipeline is composable. The server can remotely enable/disable plugins and adjust their parameters without touching the edge code.

4. **Supabase is a strong choice.** PostgreSQL + pgvector for face embeddings, Realtime for dashboard, Storage for evidence, Auth for operators — all in one self-hostable stack.

### Recommendations & Future Work

5. **Add a snapshot endpoint.** Allow the frontend to request the latest frame from a specific camera on demand: `POST /api/v1/devices/:id/cameras/:cam/snapshot` → SSE pushes a one-time `capture` event to the edge → edge sends back a frame. Useful for verifying camera angles when configuring zones.

6. **Implement evidence deduplication.** With 5 cameras at 2 FPS processing, identical or near-identical scenes produce redundant evidence. Consider perceptual hashing (pHash) on evidence images to avoid storing duplicate frames within a short time window.

7. **Add offline operation mode documentation.** Border areas may lose connectivity for hours. The edge already queues alerts in a JSONL outbox. The server handles burst-replay gracefully because timestamps are preserved by the edge client.

8. **Add bandwidth estimation to the dashboard.** Each edge device pushing evidence at ~500KB per alert × 12 alerts/minute = ~6MB/min = ~8.6 GB/day per device. Operators need visibility into bandwidth consumption per device/camera to make informed decisions about `process_every_n_frames` and `evidence_jpeg_quality`.

1. **Edge-Side Object Tracking (Debouncing)**: Using ByteTrack/DeepSORT to assign tracker IDs. The Edge waits until it has tracking confirmation and a good bounding box, only uploading an evidence frame once every 5 seconds per object to avoid alert spam.
2. **Spatial Filtering (Zones)**: Only uploading evidence when a tracked object intersects with a predefined Virtual Border line or Intrusion Polygon, dropping all general background traffic.
3. **Minimum Pixel Gate**: Ignoring objects with bounding boxes smaller than 60x60 pixels, as they are too small for server-side Face or ANPR recognition.
4. **Dynamic Frame Skipping**: Coasting at low FPS (e.g., 1-2 FPS processing) when the scene is empty, and automatically ramping up processing speed when motion or an object is detected.
