# Agent Role & Context

You are an expert AI developer agent assisting in building an Edge AI Surveillance System for border area monitoring. Your primary goal is to write highly optimized, production-ready Python code that runs efficiently on constrained edge devices (like Raspberry Pi, Orange Pi, or AIoT Edge Boxes) without GPU acceleration.

# The Problem Statement

The system is deployed in sensitive border regions to capture real-time video feeds via IP cameras. It must locally process these frames to detect threats or intrusions (e.g., persons, vehicles) and instantly dispatch structured payloads to a Supabase project. Network connectivity may be unstable, and compute power is strictly limited.

# Tech Stack & Dependencies

| Component | Technology | Directive |
| :--- | :--- | :--- |
| **Video Ingestion** | OpenCV / FFmpeg | Use strictly for connecting to RTSP streams and basic frame reading. |
| **Vision Utilities** | Roboflow Supervision | **Mandatory.** Use for all tracking, filtering, zone/line counting, and annotations. |
| **Object Tracking** | `trackers` package | `ByteTrackTracker` from the external `trackers` package. Do NOT use the deprecated `sv.ByteTrack`. |
| **Inference** | ONNX Runtime | Use `CPUExecutionProvider` for executing vision models. |
| **Backend** | Supabase | PostgREST + Storage + Realtime WebSockets. Direct edge-to-cloud, no FastAPI middleware. |
| **Async** | `asyncio` | Top-level event loop. Blocking work goes through `asyncio.to_thread` or `run_in_executor`. |

# Core Development Directives

* **Supervision-First Approach.** Reference `https://supervision.roboflow.com/latest/llms.txt` for the latest syntax. Always use `supervision` for handling bounding boxes, detections (`sv.Detections`), line crossing, and polygon zones instead of writing raw OpenCV logic.
* **Lightweight Models Only.** Never suggest full PyTorch or TensorFlow implementations. Always default to Nano or Pico models (e.g. `yolov8n`, `yolov11n`) exported to `.onnx`.
* **Modular Architecture.** Maintain strict separation across the three layers: the **Receiver** (frame extraction), the **Processor** (inference + Roboflow Supervision), and the **Sender** (Supabase REST + Storage).
* **Resource Optimization.** Proactively implement frame skipping (`process_every_n_frames`), frame resizing (`inference_size`), and robust error handling to prevent CPU thermal throttling and memory leaks.
* **Resilient Communication.** The sender must account for network drops via the bounded `data/outbox.jsonl` queue and JWT-based auth that auto-refreshes.
* **Shared ONNX Session.** One `OnnxProcessor` per device, serialized by `PluginServices.inference_lock`. Do not instantiate multiple ONNX sessions per camera.

# Architecture

```
ibvap-edge/
├── main.py                       # asyncio entry point: orchestration, per-camera tasks, sender.
├── config/
│   ├── device_settings.py        # .env-driven: Supabase URL, device credentials, model path.
│   └── camera_settings.py        # Per-camera JSON in cameras/*.json + remote overrides via Realtime.
├── core/
│   ├── ai/
│   │   ├── receiver.py           # CameraReceiver: RTSP/HTTP decode in a background thread.
│   │   └── processor.py          # OnnxProcessor: letterbox + ONNX CPU inference + NMS.
│   └── cloud/
│       ├── sender.py             # AlertSender: outbox queue + Supabase REST + Storage uploads.
│       ├── control_receiver.py   # Supabase Realtime subscription for device_settings updates.
│       ├── command_executor.py   # Processes device_commands (e.g. snapshot capture).
│       ├── camera_manager.py     # Syncs cameras/*.json with the cameras table.
│       └── metadata_reporter.py  # Reads OpenCV stream metadata and pushes to cameras.stream_info.
├── plugins/
│   ├── base.py                   # VisionPlugin Protocol, FrameContext, FeatureEvent, PluginServices.
│   ├── manager.py                # PluginManager: ordered execution, dependency injection, preview annotation.
│   ├── object_detection.py       # Runs ONNX inference into FrameContext.detections.
│   ├── object_tracking.py        # Wraps trackers.ByteTrackTracker.
│   ├── intrusion.py              # sv.PolygonZone; emits only in-zone detections.
│   ├── virtual_border.py         # sv.LineZone; emits detections that crossed the line.
│   └── evidence_capture.py       # JPEG-encodes one frame per spatial event.
├── cameras/                      # One JSON file per camera. Synced to Supabase on boot.
├── models/                       # ONNX model weights.
├── data/outbox.jsonl             # Durable queue for unsent alerts.
├── docs/                         # architecture, configuration, plugins, resilience, threading, etc.
├── Dockerfile                    # Production container image.
├── docker-compose.yml            # One-command deployment.
└── install_termux.sh             # Termux (Android) one-shot installer.
```

# Plugin Execution Order

`object_detection → object_tracking → virtual_border → intrusion_detection → evidence_capture`

`PluginManager` silently auto-loads `object_detection` and `object_tracking` whenever a spatial plugin (`virtual_border` or `intrusion_detection`) is enabled. Custom plugins are loadable via `module:ClassName` import paths.
