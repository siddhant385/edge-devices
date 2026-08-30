# Resilience and Error Handling

Border deployments suffer from frequent network outages, power fluctuations, and temporary camera disconnects. The edge pipeline is built to handle these autonomously without crashing or losing critical event data.

## 1. Offline Alert Queue (`core.sender.Sender`)

If the central server (`API_URL`) is unreachable (HTTP timeout, DNS failure, 5xx error), the sender degrades gracefully.

**Durable Storage:**
*   Alert payloads are immediately appended to a local JSON Lines file (`QUEUE_PATH`, default: `data/outbox.jsonl`).
*   This file is durable across sudden power loss (though pending OS buffers may be lost).
*   Storage is capped at `QUEUE_MAX_RECORDS` to prevent filling the SD card/eMMC. If full, the oldest records are dropped (FIFO).

**Burst Replay:**
*   On every tick, the sender checks the network.
*   If online, it sequentially drains the `outbox.jsonl` file, `POST`ing older alerts.
*   The server must rely on the payload's original `timestamp`, *not* the receipt time, for accurate event logging.

## 2. Camera Reconnection (`core.receiver.Receiver`)

RTSP streams over wireless PtP links often stutter or drop.

**Isolation:**
*   Each camera runs in its own thread. If `gate-1-cam` drops, `gate-2-cam` continues processing flawlessly.
*   The main thread continues to spin. If no frames are available for a camera, it simply skips processing that camera for the current loop.

**Retry Logic:**
*   If `cv2.VideoCapture.read()` returns `False` or raises an exception, the thread releases the resource.
*   It sleeps for `RECONNECT_DELAY_SECONDS`.
*   It attempts to re-initialize the connection. This loop repeats indefinitely until the stream recovers.

## 3. SSE Retry & Recovery

The Server-Sent Events control connection (`CONTROL_URL`) is susceptible to idle timeouts or network drops.

**Auto-Reconnect:**
*   The SSE background thread catches `requests.exceptions.RequestException`.
*   It logs the disconnect, sleeps for `CONTROL_RECONNECT_SECONDS`, and opens a new stream.
*   The edge pipeline always runs locally, so the main processing loop is unaffected by SSE connection loss. It simply continues using the last known configuration settings.

## 4. Hardware Monitoring (Heartbeat)

If `HEARTBEAT_URL` is configured, the system proactively reports its state to the server. This is critical for detecting silent failures (e.g., thermal throttling before a hard crash, or an SD card filling up).

The heartbeat thread reports:
*   CPU load (`psutil.cpu_percent`)
*   Memory usage (`psutil.virtual_memory().percent`)
*   Temperature (`/sys/class/thermal/thermal_zone0/temp` where supported)
*   Queue Depth (Lines in `outbox.jsonl`)
*   Active Cameras (Cameras currently returning valid frames).