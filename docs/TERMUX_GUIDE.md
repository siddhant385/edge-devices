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

### 2. Install FFmpeg and OpenCV Dependencies (CRITICAL)
The error you're seeing (`libswresample not found`, `libswscale not found`) means OpenCV's shared FFmpeg libraries aren't installed. Termux packages these separately:

```bash
# Install FFmpeg with its shared libraries (libswscale, libswresample, etc.)
pkg install ffmpeg

# Also install these for complete OpenCV support
pkg install opencv
```

If `opencv` package isn't available in the main repo, you need `tur-repo`:
```bash
pkg install tur-repo
pkg install opencv
```

### 3. Install Python Dependencies
After FFmpeg is in place, install Python packages. Use `pip` (Termux's Python has pip built-in):

```bash
# Install core deps first
pip install --upgrade pip setuptools wheel

# Install packages (opencv-python-headless avoids the FFmpeg dependency entirely)
pip install opencv-python-headless numpy

# Install IBVAP dependencies
pip install supervision onnxruntime aiofiles supabase python-dotenv httpx pillow
```

> **Why `opencv-python-headless`?** It bundles a minimal FFmpeg and doesn't require the system libraries. If you prefer the full OpenCV with display support, use `opencv-python` but ensure `ffmpeg` package is installed first.

### 4. Connect the Phone's Camera
Termux cannot natively read `/dev/video0` easily. The best approach is to loop back the camera via network:
1. Install an app like **IP Webcam** from the Google Play Store on your phone.
2. Open IP Webcam and click "Start server" (it usually hosts on `127.0.0.1:8080`).
3. In your `.env` file within Termux, set:
   ```env
   CAMERA_SOURCE=http://127.0.0.1:8080/video
   # OR for older IP Webcam versions:
   # CAMERA_SOURCE=rtsp://127.0.0.1:8080/h264_pcm.sdp
   ```

### 5. NPU Acceleration (Optional)
If your Android has a dedicated NPU (like a Pixel or Galaxy S-series), you can significantly boost FPS and save battery by changing the provider in `core/ai/processor.py`:

```python
# Change from:
providers=["CPUExecutionProvider"]
# To:
providers=["NnapiExecutionProvider", "CPUExecutionProvider"]
```

### 6. Running
```bash
python main.py
```

### Troubleshooting

**Error: `libswresample.so.3 not found`**
- Run `pkg install ffmpeg` again and verify it installed the shared libraries.
- Or switch to `opencv-python-headless` which doesn't need them.

**Error: `onnxruntime` won't install**
- Pre-built ARM wheels may be missing. Try:
  ```bash
  pip install --only-binary=:all: onnxruntime
  ```
- If that fails, build from source (takes 30+ minutes) or use the GitHub release wheel for aarch64.

**Error: `supervision` import fails**
- Install its dependencies first: `pip install numpy opencv-python-headless scipy`
- Then: `pip install supervision`

**App gets killed when screen turns off**
- Use `termux-wake-lock` to prevent Android from suspending Termux.
- Install Termux:Boot add-on to auto-start on boot.

**Phone overheats and FPS drops**
- Reduce `inference_size` to 320 in your camera config.
- Increase `process_every_n_frames` to 10 or higher.