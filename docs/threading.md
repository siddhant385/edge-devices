# Threading and Concurrency

The edge pipeline uses a hybrid model: an `asyncio` event loop for orchestration and network I/O, and OS threads for blocking CPU/IO work (RTSP decode, ONNX inference). The split is deliberate — `asyncio` gives us cheap concurrency for many cameras without the cost of one thread per RTSP socket.

## 1. The Asyncio Event Loop (Orchestration)

`asyncio.run(async_main())` runs the top-level event loop. Inside it, the following tasks live:

- One `run_camera_async` task **per camera** — pulls frames, runs plugins, calls the sender.
- `ControlReceiver.start()` — manages the Supabase Realtime WebSocket subscription.
- `CommandExecutor.start()` — polls `device_commands` for snapshot requests.
- `AlertSender.start()` — drains the outbox and sends queued events.
- `metadata_reporter` — reports stream metadata on camera connect.

Per-frame work that would block the loop is delegated via:

- `asyncio.to_thread(receiver._frame_queue.get, True, 1.0)` — pulls the next frame without blocking the loop.
- `loop.run_in_executor(None, plugins.process, frame)` — runs inference + plugin chain on the default thread pool.

The default executor is sized at `min(32, os.cpu_count() + 4)`, which is sufficient for the inference lock contention (only one camera can use ONNX at a time).

## 2. Camera Threads (`core/ai/receiver.py`)

Each `CameraReceiver` spawns **one background `threading.Thread`** that owns a `cv2.VideoCapture` and continuously reads frames. Frames are pushed into a `queue.Queue(maxsize=1)`:

- If the queue is full, the reader drops the oldest frame (`get_nowait`) before pushing the new one. This keeps the queue shallow and the pipeline real-time — we never want a backlog of old frames.
- The thread is daemonized so it dies with the process.

The reader also publishes the latest frame to a `FrameBroker` (`frame_broker.set_latest_frame`) for command-driven snapshots.

## 3. ONNX Inference Lock (`plugins/object_detection.py`)

A single `OnnxProcessor` instance is shared across all cameras. The processor wraps `onnxruntime.InferenceSession`, which is thread-safe to call *but* contended — two cameras calling it simultaneously will serialize at the session level.

To make that serialization explicit and bounded, `PluginServices.inference_lock` (`threading.Lock`) is acquired around every `processor.detect` call. This guarantees:

- Predictable inference latency per camera.
- No surprise memory growth from concurrent session access.
- A natural upper bound on CPU usage (one ONNX call at a time per device).

If you need more throughput, instantiate multiple `OnnxProcessor`s and round-robin cameras across them — but at the cost of RAM (each session is ~100 MB).

## 4. Realtime and Sender Tasks

- `ControlReceiver` runs entirely on the asyncio loop. The supabase-py async client uses an internal aiohttp session and a WebSocket task.
- `AlertSender.send_events` runs on the asyncio loop but pushes each HTTP call through the supabase-py async client, which handles connection pooling and retries internally.
- Outbox reads/writes use `aiofiles` for non-blocking disk I/O on the asyncio loop.

## 5. Synchronization Primitives

| Primitive | Location | Purpose |
|---|---|---|
| `threading.Lock` | `PluginServices.inference_lock` | Serialize ONNX calls across cameras. |
| `queue.Queue` | `CameraReceiver._frame_queue` | Hand frames from reader thread to asyncio task. |
| `asyncio.Lock` (in `RuntimeSettingsStore`) | `main.py` | Protect hot-reload of `CameraSettings`. |
| `asyncio.Event` | `main.py: stop` | Cooperative shutdown signal. |

## 6. Shutdown Sequence

1. `Ctrl-C` triggers `KeyboardInterrupt` in the asyncio loop.
2. `async_main` sets `stop.set()` so all `run_camera_async` tasks exit their loops.
3. `ControlReceiver.close()` unsubscribes from Realtime.
4. `CommandExecutor.close()` cancels its poll task.
5. `AlertSender.close()` flushes any in-flight sends and closes the supabase client.
6. `CameraReceiver.close()` joins its reader threads with a 2 s timeout.
7. The asyncio loop closes; the process exits.

No data is lost if the shutdown happens mid-send — the outbox file fsync'd the payload before the network call, so the next restart will replay it.
