# IBVAP Central Server — Architecture & Specification

## 1. System Overview

The IBVAP Central Server is the command-and-control hub for a distributed network of edge AI surveillance devices deployed in border areas. Each edge device runs the IBVAP edge client (`ibvap-edge` repo), connects to one or more IP cameras, performs local object detection via ONNX, and pushes structured alert payloads directly to this Supabase project. The AI worker subscribes to detection inserts, runs face recognition and vehicle ANPR on the JPEG evidence, and writes results back to the database. The frontend is a Next.js dashboard that subscribes to Supabase Realtime for live operator updates.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CENTRAL SERVER                              │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │ AI       │  │ Next.js  │  │ Edge     │  │ Realtime           │  │
│  │ Worker   │  │ Dashboard│  │ Functions│  │ (PostgREST WS)     │  │
│  │ (Face/   │  │ (Tailwind│  │ (Deno)   │  │                    │  │
│  │ ANPR)    │  │ shadcn)  │  │          │  │                    │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬───────────┘  │
│       │              │             │                  │              │
│       └──────────────┴─────────────┴──────────────────┘              │
│                              │                                       │
│                     ┌────────┴────────┐                              │
│                     │   Supabase      │                              │
│                     │   PostgreSQL    │                              │
│                     │   + Storage     │                              │
│                     │   + Realtime    │                              │
│                     │   + Auth        │                              │
│                     └─────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘
          ▲               │
          │ REST + Storage│ Realtime (settings push)
          │ + Realtime    ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ Edge     │    │ Edge     │    │ Edge     │
    │ Device 1 │    │ Device 2 │    │ Device N │
    └──────────┘    └──────────┘    └──────────┘
```

---

## 2. Why Supabase

| Requirement | Supabase Feature |
|---|---|
| Structured alert storage with relational queries | PostgreSQL with full SQL, indexes, partitioning |
| Evidence image storage (JPEG frames) | Supabase Storage (S3-compatible, signed URLs) |
| Real-time dashboard updates | Supabase Realtime (Postgres changes → WebSocket) |
| Authentication for operators and devices | Supabase Auth (JWT, row-level security) |
| Edge functions for lightweight processing | Supabase Edge Functions (Deno) |
| Self-hostable for air-gapped deployments | Fully open-source stack |

For border deployments that may need air-gapped operation, Supabase's self-hosting story is critical.

---

## 3. Tech Stack

| Component | Technology | Rationale |
|---|---|---|
| Database | **Supabase PostgreSQL** | Relational + realtime + auth + storage |
| Image Storage | **Supabase Storage** | S3-compatible, signed URLs, lifecycle policies |
| AI Worker | **Python + ONNX Runtime** | Face recognition (ArcFace), ANPR models |
| Frontend | **Next.js 15** + **Tailwind** + **shadcn/ui** | SSR, Supabase JS client for realtime |
| Realtime | **Supabase Realtime** | Postgres changes pushed to frontend and edge |
| Deployment | **Docker Compose** | Single-command deployment, self-hostable |

---

## 4. Database Schema

### 4.1 `devices`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | Auto-generated |
| `device_id` | `text UNIQUE` | Matches `DEVICE_ID` env (e.g. `edge-001`) |
| `auth_user_id` | `uuid REFERENCES auth.users(id)` | RLS key |
| `name` | `text` | Human label |
| `is_online` | `bool DEFAULT false` | Heartbeat flag |
| `last_seen_at` | `timestamptz` | Last heartbeat |
| `device_info` | `jsonb` | Free-form metadata |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.2 `cameras`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | |
| `device_id` | `uuid REFERENCES devices(id)` | |
| `camera_id` | `text UNIQUE` | Matches the JSON file's `id` |
| `name` | `text` | |
| `source_url` | `text` | RTSP URL (stored as-is) |
| `is_online` | `bool DEFAULT false` | Stream status |
| `location` | `text` | |
| `coordinates` | `point` | Geo lat/lon |
| `stream_info` | `jsonb` | Width/height/fps/codec from edge |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.3 `detections`

One row per bounding box emitted by the edge.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | |
| `device_id` | `uuid REFERENCES devices(id)` | |
| `camera_id` | `uuid REFERENCES cameras(id)` | |
| `feature` | `text NOT NULL` | `object_detection`, `intrusion_detection`, `virtual_border`, `evidence_capture` |
| `class_id` | `int` | COCO class index |
| `class_name` | `text` | COCO label |
| `confidence` | `real` | 0.0–1.0 |
| `bbox_xyxy` | `real[4]` | `[x1, y1, x2, y2]` |
| `tracker_id` | `int NULL` | Persistent ID from ByteTrack |
| `evidence_path` | `text NULL` | Storage path to JPEG; only the first row in a batch has this populated |
| `timestamp` | `timestamptz DEFAULT now()` | Edge-side event time |
| `created_at` | `timestamptz DEFAULT now()` | DB receive time |

A trigger `tr_queue_new_detection` queues each new row for the AI worker to process.

### 4.4 `alerts`

High-severity events for the operator dashboard.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | |
| `device_id` | `uuid REFERENCES devices(id)` | |
| `camera_id` | `uuid REFERENCES cameras(id)` | |
| `detection_id` | `uuid REFERENCES detections(id)` | The originating detection row |
| `timestamp` | `timestamptz` | Edge-side event time |
| `received_at` | `timestamptz DEFAULT now()` | |
| `evidence_path` | `text` | Storage path (always set for critical alerts) |
| `has_evidence` | `bool DEFAULT false` | |
| `severity` | `alert_severity` | `info` / `warning` / `critical` |
| `status` | `alert_status` | `unacknowledged` / `investigating` / `resolved` / `false_positive` / `acknowledged` |
| `raw_payload` | `jsonb` | Full detection context |
| `operator_id` | `uuid REFERENCES auth.users(id)` | |
| `acknowledged_at` | `timestamptz` | |
| `resolved_at` | `timestamptz` | |
| `processed` | `bool DEFAULT false` | AI worker has consumed |

### 4.5 `device_settings`

| Column | Type | Notes |
|---|---|---|
| `device_id` | `text UNIQUE` | One row per device |
| `settings` | `jsonb` | The pushed settings object |
| `version` | `text` | Unique per push (UUID or hash) |
| `updated_at` | `timestamptz DEFAULT now()` | |

### 4.6 `camera_settings`

| Column | Type | Notes |
|---|---|---|
| `camera_id` | `text UNIQUE` | One row per camera |
| `settings` | `jsonb` | Mirrors the on-disk JSON |
| `version` | `text` | |
| `updated_at` | `timestamptz DEFAULT now()` | |

### 4.7 `device_commands`

Server-pushed commands (e.g. snapshot capture).

| Column | Type | Notes |
|---|---|---|
| `device_id` | `text` | |
| `camera_id` | `text NULL` | |
| `command` | `text` | `snapshot`, ... |
| `payload` | `jsonb` | |
| `status` | `text DEFAULT 'pending'` | `pending`, `completed`, `failed` |
| `result` | `jsonb` | Populated by the edge |
| `created_at`, `updated_at` | `timestamptz` | |

### 4.8 `face_results`

Server-side face recognition output.

| Column | Type | Notes |
|---|---|---|
| `alert_id` | `uuid REFERENCES alerts(id)` | |
| `detection_id` | `uuid REFERENCES detections(id)` | |
| `matched_identity_id` | `uuid REFERENCES known_faces(id)` | |
| `face_embedding` | `vector(512)` | pgvector |
| `similarity_score` | `real` | |
| `face_crop_path` | `text` | |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.9 `anpr_results`

Server-side ANPR output.

| Column | Type | Notes |
|---|---|---|
| `alert_id`, `detection_id` | `uuid` | |
| `plate_text` | `text` | |
| `plate_confidence` | `real` | |
| `plate_crop_path` | `text` | |
| `is_flagged` | `bool DEFAULT false` | |
| `created_at` | `timestamptz DEFAULT now()` | |

### 4.10 `known_faces`

Watchlist.

| Column | Type | Notes |
|---|---|---|
| `name` | `text NOT NULL` | |
| `face_embedding` | `vector(512)` | |
| `reference_image_path` | `text` | |
| `threat_level` | `text` | `low`, `medium`, `high`, `critical` |
| `created_at` | `timestamptz DEFAULT now()` | |

---

## 5. Row Level Security

Each table has RLS enabled. The policies:

- `devices`: an operator (authenticated user) can SELECT/UPDATE all rows; an edge can only UPDATE its own row's `is_online` and `last_seen_at`.
- `detections`, `alerts`: INSERT requires `auth.uid()` to match the `auth_user_id` of the row's `device_id`. SELECT is allowed for all authenticated operators.
- `device_settings`, `camera_settings`: SELECT for all authenticated operators; INSERT/UPDATE restricted to operators with the dashboard role.
- `device_commands`: an edge SELECTs its own commands; an operator INSERTs.
- `face_results`, `anpr_results`, `known_faces`: read-all for authenticated operators; write restricted to the AI worker's service role.

---

## 6. Realtime Channels

- `detections`: AI worker subscribes to `INSERT` events with `class_id IN (0, 2, 5, 7)` (people and vehicles).
- `device_settings`: edge subscribes to `UPDATE` events filtered by `device_id`.
- `alerts`: dashboard subscribes to all events for the live feed.
- `cameras`: dashboard subscribes for online/offline status badges.

---

## 7. Storage

- **Bucket**: `evidence` (private).
- **Path**: `<device_id>/<camera_id>/<YYYY-MM-DD>/<uuid>.jpg`.
- **Access**: signed URLs via the `storage.from_("evidence").create_signed_url(path, ttl)` API for the dashboard preview.
- **Lifecycle**: optional cron to delete entries older than N days (TBD by operator policy).

---

## 8. Edge → Server Version Matrix

| Edge version | Required server schema version |
|---|---|
| ≥ 0.1.0 | All tables listed above with `severity`/`status` enums on `alerts` |
