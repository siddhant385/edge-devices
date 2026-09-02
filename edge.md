# Edge-to-Supabase Direct Integration

## Overview

IBVAP edge devices communicate directly with Supabase: PostgREST for inserts, Supabase Storage for JPEG evidence, and Supabase Realtime (WebSockets) for live configuration updates. There is no intermediate FastAPI layer on the server side.

---

## 1. Authentication

The edge is provisioned as a first-class Supabase Auth user. The provisioning flow lives in the Next.js dashboard, not on the edge:

1. Operator clicks "Register Edge Device" in the dashboard.
2. Dashboard calls `supabase.auth.admin.create_user()` with:
   - `email`: `edge-001@devices.ibvap.internal` (virtual email)
   - `password`: a strong randomly-generated string, shown to the operator once
3. Dashboard inserts a row into the `devices` table linking `auth_user_id` to a `device_id` slug.
4. Operator writes `DEVICE_EMAIL` and `DEVICE_PASSWORD` into the edge's `.env`.

On boot, the edge calls `supabase.auth.sign_in_with_password()` and stores the JWT + refresh token. The supabase-py async client auto-refreshes before expiry.

Row Level Security on `detections`, `alerts`, and `devices` is keyed off `auth.uid()` — a compromised device cannot read or write other devices' data.

---

## 2. Sender (`core/cloud/sender.py`)

### Per-event flow

1. **JPEG upload** to Supabase Storage (`evidence` bucket):
   ```python
   supabase.storage.from_("evidence").upload(
       f"{device_id}/{camera_id}/{date}/{uuid}.jpg",
       evidence_jpeg_bytes,
       {"content-type": "image/jpeg"},
   )
   ```
2. **Detection row insert** into `detections` via PostgREST. Each bounding box is one row.
3. **Alert row insert** into `alerts` for spatial events with attached evidence:
   ```python
   supabase.table("alerts").insert({
       "device_id": device_uuid,
       "camera_id": camera_uuid,
       "detection_id": detection_uuid,
       "evidence_path": path,
       "severity": "critical",
       "status": "unacknowledged",
       "raw_payload": {...},
   }).execute()
   ```

### Offline handling

If any step fails (timeout, 5xx, network drop), the entire event is appended to `data/outbox.jsonl` and retried on the next tick. Capped at `QUEUE_MAX_RECORDS` (default 1000) with FIFO eviction.

---

## 3. Control Receiver (`core/cloud/control_receiver.py`)

The edge subscribes to Postgres changes on `device_settings`:

```python
channel = supabase.channel(f"settings-{device_id}")
channel.on(
    "postgres_changes",
    event="UPDATE",
    schema="public",
    table="device_settings",
    filter=f"device_id=eq.{device_id}",
    callback=handle_new_settings,
).subscribe()
```

When the dashboard updates `device_settings.settings`, Supabase pushes the new JSONB over the WebSocket. `handle_new_settings` validates the keys against `REMOTE_SETTING_NAMES` and calls `apply_remote_camera_settings` on each camera. The change takes effect on the next `run_camera_async` tick when `PluginManager` is rebuilt.

Supabase Realtime handles reconnection natively with exponential backoff — no custom retry loop is needed.

---

## 4. Camera Manager (`core/cloud/camera_manager.py`)

`cameras/*.json` is the source of truth on disk. On boot, `CameraManager.load_local_cameras()` reads every file and registers the cameras in Supabase:

- If a `camera_id` is missing in Supabase, insert a row.
- If the local source URL differs from the cloud `source_url`, update the cloud row.
- Push the per-camera settings (`camera_settings` table) so the dashboard can render the current polygon / line / enabled plugins.

This means cameras added offline (by editing JSON on the device) show up in the dashboard automatically on the next reconnect.

---

## 5. Metadata Reporter (`core/cloud/metadata_reporter.py`)

When a camera connects (`CameraReceiver._on_online_change(True)`), the reporter queries OpenCV for the stream's resolution and FPS, then updates the `cameras` row:

```python
supabase.table("cameras").update({
    "stream_info": {"width": w, "height": h, "fps": fps, "codec": codec},
    "is_online": True,
}).eq("id", camera_id).execute()
```

This is what the dashboard uses to render the preview tile sizes correctly without probing each camera.

---

## 6. AI Worker (Server-side, separate repo)

The face/ANPR ONNX models live in a separate worker process (`ai_worker.py` in the server repo). It listens to Supabase Realtime for `INSERT` events on `detections` where `class_id` indicates a person or vehicle, downloads the JPEG from Storage, runs inference, and writes `face_results` / `anpr_results` rows.

---

## Benefits of This Design

- **Cost**: No public-facing Python web server to host.
- **Bandwidth**: JPEGs go to Storage directly, not as base64 inside JSON.
- **Resilience**: Supabase Realtime replaces custom SSE with a managed WebSocket layer.
- **Security**: JWT + RLS replaces a custom bcrypt API-key scheme.
