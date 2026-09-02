# Resilience and Error Handling

Border deployments suffer from frequent network outages, power fluctuations, and temporary camera disconnects. The edge pipeline handles these autonomously without crashing or losing critical event data.

## 1. Offline Alert Queue (`core/cloud/sender.py`)

If Supabase is unreachable (timeout, DNS failure, 5xx), `AlertSender` degrades gracefully.

**Durable Storage:**
- Alert payloads are appended to `data/outbox.jsonl` (path configurable via `QUEUE_PATH`).
- The file is fsync'd after each write to survive sudden power loss.
- Storage is capped at `QUEUE_MAX_RECORDS` (default 1000). If full, the oldest records are dropped (FIFO) and a warning is logged.

**Burst Replay:**
- On each send tick, the sender checks network availability.
- If online, it drains the outbox, `POST`ing older alerts first.
- The Supabase row's `timestamp` reflects the original event time, not the receipt time.

**Per-tracker debounce (sender-side):**
- `AlertSender` keeps a 5 s cooldown map keyed by `tracker_id` to prevent storms when multiple plugins emit for the same object in the same frame.

**Per-tracker evidence debounce (plugin-side):**
- `EvidenceCapturePlugin` keeps a 30 s cooldown keyed by `tracker_id`, with a 3600 px² minimum bbox. This ensures one JPEG per object per 30 s rather than one per frame.

## 2. Camera Reconnection (`core/ai/receiver.py`)

RTSP streams over wireless PtP links often stutter or drop.

**Isolation:**
- Each camera runs `CameraReceiver` in its own background `threading.Thread`.
- `main.py` runs one `asyncio` task per camera; the asyncio task pulls from the receiver's `queue.Queue` via `asyncio.to_thread`. A dropped camera only affects its own queue.
- The main process keeps running and serving other cameras.

**Retry Logic:**
- If `cv2.VideoCapture.read()` returns `False` or raises, the thread releases the capture object, sleeps for `RECONNECT_DELAY_SECONDS` (default 3 s), and re-opens.
- `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;udp|timeout;2000000` is set globally to use UDP with a 2 s timeout for RTSP — TCP fallback is automatic in OpenCV's FFmpeg backend if UDP fails.

## 3. Supabase Realtime Reconnect (`core/cloud/control_receiver.py`)

The Realtime WebSocket subscription carries server-pushed setting changes. It is the modern equivalent of the old SSE listener.

**Auto-Reconnect:**
- The supabase-py async client handles WebSocket reconnection internally with exponential backoff.
- If the subscription drops, `ControlReceiver.start` retries after `CONTROL_RECONNECT_SECONDS` (default 5 s).
- The pipeline always runs locally; settings simply stay frozen at the last known value until the next successful push.

## 4. Authentication and Token Refresh

- On boot, `main.py` calls `supabase.auth.sign_in_with_password(DEVICE_EMAIL, DEVICE_PASSWORD)`.
- The returned JWT is attached to every Supabase REST and Storage request.
- The supabase-py async client auto-refreshes the JWT before expiry; no manual token rotation is required.
- If authentication fails, the pipeline aborts with a clear error — there is no anonymous fallback because RLS would block all writes anyway.

## 5. Hardware Monitoring

Device liveness is communicated by updating the `devices.is_online` and `devices.last_seen_at` columns on a periodic tick. There is no separate heartbeat table; the operator dashboard reads `is_online` directly.

- `HEARTBEAT_INTERVAL_SECONDS` controls the cadence (default 60 s, minimum 10 s).
- A row older than the threshold is treated as offline by the dashboard's own logic — no Postgres cron is required.
