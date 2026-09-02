#!/bin/bash
# IBVAP Edge Node - Termux (Android) Install Script
# Run this inside Termux: bash install_termux.sh

set -e

echo "=== IBVAP Edge Node - Termux Setup ==="
echo ""

# Check we're on Termux
if [ -z "$TERMUX_VERSION" ]; then
    echo "ERROR: This script must be run inside Termux on Android."
    exit 1
fi

# 1. Update packages
echo "[1/7] Updating Termux packages..."
pkg update -y
pkg upgrade -y

# 2. Install system dependencies
echo "[2/7] Installing system dependencies..."
pkg install -y python python-numpy termux-api ffmpeg

# 3. Upgrade pip
echo "[3/7] Upgrading pip..."
pip install --upgrade pip setuptools wheel

# 4. Install OpenCV (headless to avoid FFmpeg dep issues)
echo "[4/7] Installing OpenCV (headless)..."
pip install opencv-python-headless

# 5. Install Python dependencies
echo "[5/7] Installing Python dependencies..."
pip install numpy scipy supervision onnxruntime aiofiles supabase python-dotenv httpx pillow trackers

# 6. Create .env from example if missing
echo "[6/7] Checking configuration..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "Created .env from .env.example - EDIT IT BEFORE RUNNING"
    else
        echo "WARNING: No .env.example found. Create .env manually."
    fi
else
    echo ".env already exists, skipping."
fi

# 7. Check for model file
echo "[7/7] Checking model file..."
if [ ! -f "models/yolov8n.onnx" ]; then
    echo "WARNING: models/yolov8n.onnx not found."
    echo "Download it with:"
    echo "  mkdir -p models"
    echo "  curl -L -o models/yolov8n.onnx https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Supabase URL, API keys, and camera source"
echo "  2. (Optional) Download model: bash -c 'mkdir -p models && curl -L -o models/yolov8n.onnx https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx'"
echo "  3. Run: python main.py"
echo ""
echo "For phone camera, install 'IP Webcam' app and set CAMERA_SOURCE=http://127.0.0.1:8080/video"
