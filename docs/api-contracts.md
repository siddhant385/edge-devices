# API Contracts

This document describes the JSON payloads exchanged between the edge AI pipeline and the central Supabase project. Authentication is JWT-based (issued via `supabase.auth.sign_in_with_password`); RLS policies on each table enforce authorization.

## 1. Detection Insert (Edge → Supabase)

When any enabled plugin emits detections, `AlertSender` (`core/cloud/sender.py`) inserts one row per bounding box into the `detections` table via PostgREST.

**Table:** `public.detections`
**Method:** `POST /rest/v1/detections?select=id,...`
**Headers:** `Authorization: Bearer <JWT>`, `apikey: <anon_key>`, `Content-Type: application/json`, `Prefer: return=representation`

**Row shape (one per detection):**

```json
{
  "device_id": "<uuid>",
  "camera_id": "<uuid>",
  "timestamp": "2026-09-03T01:53:54.144+00:00",
  "feature": "virtual_border",
  "class_id": 0,
  "class_name": "person",
  "confidence": 0.8743,
  "bbox_xyxy": [320.5, 110.2, 410.0, 480.7],
  "tracker_id": 5,
  "evidence_path": "edge-001/<cam-uuid>/2026-09-03/<uuid>.jpg"
}
```

Only the first detection row in a batch carries `evidence_path`; subsequent rows in the same event reference the same path implicitly via `tracker_id` lookup.

## 2. Evidence Upload (Edge → Supabase Storage)

JPEG blobs go straight to the `evidence` bucket without base64 wrapping.

**Bucket:** `evidence`
**Method:** `POST /storage/v1/object/evidence/<path>`
**Headers:** `Authorization: Bearer <JWT>`, `Content-Type: image/jpeg`

**Path format:** `<device_id>/<camera_id>/<YYYY-MM-DD>/<uuid>.jpg`

The JPEG is downscaled to `evidence_max_width` (default 1280) with `INTER_AREA` and encoded at `evidence_jpeg_quality` (default 75) by `EvidenceCapturePlugin._encode` (`plugins/evidence_capture.py`).

## 3. Alert Insert (Edge → Supabase)

For spatial features (`virtual_border`, `intrusion_detection`) with attached evidence, the sender also inserts a row into `alerts`.

**Table:** `public.alerts`
**Method:** `POST /rest/v1/alerts`
**Headers:** same as detections

```json
{
  "device_id": "<uuid>",
  "camera_id": "<uuid>",
  "timestamp": "2026-09-03T01:53:54.144+00:00",
  "detection_id": "<uuid of detections row>",
  "evidence_path": "edge-001/<cam-uuid>/2026-09-03/<uuid>.jpg",
  "has_evidence": true,
  "severity": "critical",
  "status": "unacknowledged",
  "raw_payload": {
    "feature": "virtual_border",
    "class_name": "person",
    "confidence": 0.8743,
    "tracker_id": 5,
    "bbox_xyxy": [320.5, 110.2, 410.0, 480.7]
  }
}
```

Severity is `critical` for any spatial event with evidence; the AI worker can escalate further on watchlist matches.

## 4. Real-time Configuration Push (Server → Edge)

The server updates `public.device_settings` for this device. A Supabase Realtime subscription on the edge (`ControlReceiver`) receives the new row, validates the keys against `REMOTE_SETTING_NAMES`, and applies them via `apply_remote_camera_settings`. The change is propagated to the live `PluginManager` on the next camera loop tick.

**Channel:** Postgres changes on `public.device_settings` filtered by `device_id=eq.<device_text_id>`.

**Push payload (server side, for reference):**

```json
{
  "version": "v1.2",
  "settings": {
    "confidence_threshold": 0.6,
    "process_every_n_frames": 10,
    "virtual_border_line": [[0.4, 0.5], [0.6, 0.5]]
  }
}
```

Any keys outside the allow-list are silently ignored.

## 5. Commands (Server → Edge)

The server inserts rows into `public.device_commands` with `command='snapshot'` or similar. `CommandExecutor` (`core/cloud/command_executor.py`) processes them and writes back to `result`.

## 6. Heartbeat

`is_online` and `last_seen_at` on the `devices` row are updated periodically. There is no separate heartbeat table.
