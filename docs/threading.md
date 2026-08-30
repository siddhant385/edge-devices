# Threading and Concurrency

To handle real-time video without dropping streams or blocking the main processing loop, the edge pipeline utilizes isolated daemon threads and shared synchronization primitives.

## 1. The Main Thread (Inference Loop)

The `main.py` entry point initializes resources and loops indefinitely. It owns:
1.  **The Processor / ONNX Runtime**: Execution provider calls block the main thread.
2.  **Plugin Iteration**: Sequentially processes a `FrameContext`.
3.  **Sender Invocation**: Synchronously calls the sender (which then writes to disk or queues a network request).

The main thread runs as fast as `PROCESS_EVERY_N_FRAMES` and camera FPS permit. It does *not* read raw camera I/O directly.

## 2. Camera Threadpool (`core.receiver.Receiver`)

Video decoding (via `cv2.VideoCapture`) is I/O bound. The `Receiver` spawns one Python `threading.Thread` per configured camera.

**Lifecycle:**
*   Thread opens the RTSP/Webcam stream.
*   Enters a `while True` loop reading `cap.read()`.
*   Writes the latest valid frame to a shared dictionary `self._latest_frames[camera.camera_id]`.
*   Skips or overwrites older frames if the main thread is busy doing inference.
*   Handles timeouts/disconnects independently, attempting to re-open its specific stream after `RECONNECT_DELAY_SECONDS` without disrupting other cameras.

**Daemon Status:**
All camera threads are spawned with `daemon=True`, ensuring they terminate immediately when the main thread exits or crashes.

## 3. SSE Listener Thread (`core.receiver.listen_for_settings`)

When `CONTROL_URL` is configured, a background daemon thread maintains the persistent Server-Sent Events HTTP connection.

**Lifecycle:**
*   Opens a long-lived `requests.get(stream=True)`.
*   Blocks on network I/O waiting for server messages.
*   When a `settings` event arrives, it acquires `_settings_lock`.
*   Updates the shared `Settings` object.
*   If the connection drops, it sleeps for `CONTROL_RECONNECT_SECONDS` and retries.

## 4. Shared Locks and Thread Safety

The architecture minimizes shared state to prevent deadlocks and data races.

*   **`Receiver._frames_lock` (`threading.Lock`)**: Protects concurrent access to the `_latest_frames` dictionary. The camera threads hold it briefly to write; the main thread holds it briefly to read all current frames.
*   **`Receiver._settings_lock` (`threading.Lock`)**: Protects the global `Settings` object. The SSE thread holds it to write updates; the main loop holds it once per iteration to snapshot the current configuration before running the plugin pipeline.