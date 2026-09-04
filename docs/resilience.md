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

## 5. Device Liveness via Realtime Presence

Device liveness is communicated through a single Supabase Realtime **presence** channel rather than REST polling. The edge opens the channel on the same WebSocket it already uses for control, periodically re-issues `track()`, and writes `devices.last_seen_at` itself from the resulting `presence_sync` event. A pg_cron job (see migration `cron_mark_devices_offline_after_90s`) flips `is_online` to `false` if `last_seen_at` is stale.

**Edge side (`core/cloud/presence.py`):**
- On boot, after `sign_in_with_password`, `PresencePublisher.start()` opens channel `device-presence:{device_uuid}` with presence enabled and calls `track({"device_id", "device_uuid", "online_at"})` once.
- A background task re-`track()`s every `REFRESH_SECONDS` (30 s). Each refresh triggers our own `on_presence_sync` callback, which issues a tiny `update(devices).eq(id, uuid).set(last_seen_at=now, is_online=true)`. This is one REST upsert per 30 s — orders of magnitude cheaper than per-frame polling.
- The supabase-py realtime client maintains the WebSocket itself (ping/pong, exponential-backoff reconnect).
- On graceful shutdown, the refresh task is cancelled and `untrack()` is called so the server sees an immediate leave.

**Why the edge writes its own `last_seen_at` instead of a server-side listener:**
Supabase Realtime presence is an in-memory CRDT inside the Realtime cluster. It is not exposed to Postgres, and there is no mechanism to route presence events to an Edge Function or database trigger. The only consumers of presence events are other WebSocket clients on the same channel. The edge is one of those clients, so it sees its own join/sync events and writes the timestamp itself. This is the canonical Supabase pattern for client-side liveness.

**Offline detection (`cron_mark_devices_offline_after_90s`):**
- pg_cron job `mark-devices-offline` runs every 30 s: `update devices set is_online = false where is_online = true and last_seen_at < now() - interval '90 seconds'`.
- 90 s threshold is 3x the 30 s refresh interval — absorbs a single missed refresh and WebSocket jitter without flapping.
- When the WebSocket dies (power loss, kill -9, network drop), the edge stops calling `track()`, `last_seen_at` goes stale, and the next cron tick flips `is_online` to `false`.
