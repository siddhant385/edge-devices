# IBVAP Edge Node

The Edge AI application responsible for local frame ingestion, vision processing (via YOLO ONNX models and Roboflow Supervision), and alert dispatch to the central Supabase backend.

## Architecture

*   **Receiver:** Connects to local cameras or RTSP streams (via IP Camera apps) using OpenCV/FFmpeg.
*   **Processor:** Uses ONNX Runtime (`CPUExecutionProvider` or NNAPI) to run inference on aggressive motion-filtered frames. Integrates Supervision for slicing and NMS.
*   **Plugins:** Pluggable event architecture (e.g., Object Detection, Tracking, Virtual Border, Intrusion).
*   **Sender:** Dispatches JSONL alerts and JPEG evidence to Supabase asynchronously with local queueing.

## Quick Start (Standard Linux/Raspberry Pi)

1.  **Install dependencies:**
    ```bash
    uv venv
    source .venv/bin/activate
    uv pip install -r requirements.txt
    ```

2.  **Configuration:**
    ```bash
    cp .env.example .env
    # Edit .env with your Supabase URL, API Keys, and Camera details
    ```

3.  **Run the node:**
    ```bash
    python main.py
    ```

## Termux (Android) Setup Guide

You can run this edge node on an old Android phone! Modern Snapdragon CPUs process YOLOv11n very efficiently.

### 1. Prepare Termux
Install **Termux** and **Termux:API** from F-Droid (do not use the Google Play versions).

Open Termux and run:
```bash
pkg update && pkg upgrade
pkg install python python-numpy libjpeg-turbo libpng build-essential cmake
pkg install termux-api
```

### 2. Install Python Dependencies
On ARM Android, some Python vision packages need specific compilation. We recommend using the `tur-repo` (Termux User Repository) for OpenCV:

```bash
pkg install tur-repo
pkg install python-opencv
pip install supervision onnxruntime aiofiles supabase python-dotenv
```

### 3. Connect the Phone's Camera
Termux cannot natively read `/dev/video0` easily. The best approach is to loop back the camera via network:
1. Install an app like **IP Webcam** from the Google Play Store on your phone.
2. Open IP Webcam and click "Start server" (it usually hosts on `127.0.0.1:8080`).
3. In your `.env` file within Termux, set:
   ```env
   CAMERA_SOURCE=rtsp://127.0.0.1:8080/h264_pcm.sdp
   # or the HTTP endpoint provided by IP Webcam
   ```

### 4. NPU Acceleration (Optional)
If your Android has a dedicated NPU (like a Pixel or Galaxy S-series), you can significantly boost FPS and save battery by changing the provider in `core/processor.py` from `["CPUExecutionProvider"]` to:
```python
providers=["NnapiExecutionProvider", "CPUExecutionProvider"]
```

### 5. Running
```bash
python main.py
```