# Agent Role & Context
You are an expert AI developer agent assisting in building an Edge AI Surveillance System for border area monitoring. Your primary goal is to write highly optimized, production-ready Python code that runs efficiently on constrained edge devices (like Raspberry Pi or AIoT Edge Boxes) without GPU acceleration.

# The Problem Statement
The system is deployed in sensitive border regions to capture real-time video feeds via IP cameras. It must locally process these frames to detect threats or intrusions (e.g., persons, vehicles) and instantly dispatch structured JSON payloads to a central server. Network connectivity may be unstable, and compute power is strictly limited.

# Tech Stack & Dependencies

| Component | Technology | Directive |
| :--- | :--- | :--- |
| **Video Ingestion** | OpenCV / FFmpeg | Use strictly for connecting to RTSP streams and basic frame reading. |
| **Vision Utilities** | Roboflow Supervision | **Mandatory.** Use for all tracking, filtering, zone/line counting, and annotations. |
| **Inference** | ONNX Runtime | Use `CPUExecutionProvider` for executing vision models. |

# Core Development Directives

*   **Supervision-First Approach:** You must reference `https://supervision.roboflow.com/latest/llms.txt` for the latest syntax. Always use `supervision` for handling bounding boxes, detections (`sv.Detections`), line crossing, and polygon zones instead of writing raw OpenCV logic.
*   **Lightweight Models Only:** Never suggest full PyTorch or TensorFlow implementations. Always default to Nano or Pico models (like YOLOv8n or MobileNet) exported to the `.onnx` format to ensure high FPS on edge CPUs.
*   **Modular Architecture:** Maintain the strict separation of concerns across the three core components: the **Receiver** (frame extraction), the **Processor** (inference), and the **Sender** (API communication). 
*   **Resource Optimization:** Proactively implement frame skipping (e.g., processing every 5th frame), frame resizing, and robust error handling to prevent CPU thermal throttling and memory leaks.
*   **Resilient Communication:** The sender module must account for network drops typical in border areas, utilizing timeout handling and payload queuing where necessary.

# Architecture
edge-ai-client/
├── core/                   # The main logic engines
│   ├── __init__.py
│   ├── receiver.py         # 1. Connects to RTSP/IP Camera and extracts frames
│   ├── processor.py        # 2. Runs ONNX inference and uses Roboflow Supervision
│   └── sender.py           # 3. Formats detections to JSON and pushes to API/WebSocket
│
├── models/                 # Directory to store local lightweight model weights
│   └── yolov8n.onnx        # ONNX format for fast CPU execution
│
├── config/                 # Configuration and environment setup
│   ├── __init__.py
│   └── settings.py         # Loads variables (e.g., Camera ID, API Endpoints)
│
├── .env                    # Device ID, API keys, Server URL (DO NOT COMMIT)
├── .gitignore
├── requirements.txt        # opencv-python, onnxruntime, supervision, requests, etc.
├── AGENTS.md               # AI instructions for Cursor/Copilot
└── main.py                 # The entry point that ties Receiver, Processor, and Sender together
