# Deployment

This guide covers deploying the edge AI pipeline on a constrained device: Raspberry Pi, Orange Pi, NVIDIA Jetson, AIoT Edge Box, x86 VPS, or any Linux host with Python 3.12+. A Termux (Android phone) recipe is in [docs/TERMUX_GUIDE.md](TERMUX_GUIDE.md).

## Prerequisites

- Python 3.12 or higher.
- `uv` for fast dependency management (recommended).
- A pre-trained ONNX model (e.g. `yolov8n.onnx` placed at `models/yolo26n.onnx`).
- A Supabase project with the schema in `server.md`.

## 1. Setup Environment

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone <repository_url> ibvap-edge
cd ibvap-edge

# Create a virtual environment and install dependencies
uv sync
```

## 2. Configuration

```bash
cp .env.example .env
nano .env
```

Required variables for production:

- `DEVICE_ID` — unique slug for this device.
- `SUPABASE_URL` — your Supabase project URL.
- `API_KEY` — Supabase anon key.
- `DEVICE_EMAIL`, `DEVICE_PASSWORD` — credentials issued at device registration.
- `SHOW_PREVIEW=false` — must be false on a headless deployment.

Then add cameras:

```bash
mkdir -p cameras
cat > cameras/gate-1.json <<'EOF'
{
  "id": "gate-1",
  "source": "rtsp://user:pass@192.168.1.100:8554/stream",
  "enabled_plugins": ["virtual_border", "evidence_capture"],
  "virtual_border_line": [[0.4, 0.5], [0.6, 0.5]]
}
EOF
```

And the model:

```bash
mkdir -p models
cp /path/to/yolov8n.onnx models/yolo26n.onnx
```

On first run, `CameraManager` uploads the camera configs to Supabase. The operator can then edit them in the dashboard and settings will hot-reload via Realtime.

## 3. Run Locally

```bash
uv run python main.py
```

## 4. Run with Docker

A multi-stage `Dockerfile` is included. It builds on `python:3.12-slim`, installs OpenCV's system deps (libgl, libswscale, libswresample, etc.), and runs as a non-root user.

```bash
docker build -t ibvap-edge .
docker compose up -d
docker compose logs -f
```

Mount the camera configs and model as read-only volumes; mount `data` and `logs` for persistence. The compose file uses `network_mode: host` for low-latency RTSP and Realtime WebSocket connectivity — change to a bridged network if you don't need that.

Resource caps in `docker-compose.yml` default to 2 CPU and 1 GB RAM, suitable for two-camera deployments. Adjust for your hardware.

## 5. Run as a Systemd Service

For Linux hosts running directly on metal (no Docker):

```ini
# /etc/systemd/system/edge-ai.service
[Unit]
Description=IBVAP Edge AI Surveillance Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ibvap
WorkingDirectory=/home/ibvap/ibvap-edge
ExecStart=/home/ibvap/ibvap-edge/.venv/bin/python main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now edge-ai.service
sudo journalctl -u edge-ai.service -f
```

## 6. Run on Termux (Android)

See [docs/TERMUX_GUIDE.md](TERMUX_GUIDE.md). The one-shot installer is `install_termux.sh` at the project root.

## Hardware Considerations

- **CPU**: Defaults to `CPUExecutionProvider`. Adjust `process_every_n_frames` and `inference_size` to prevent thermal throttling on Raspberry Pi-class devices.
- **Memory**: 512 MB is sufficient for one camera. 1 GB handles two cameras comfortably. Each `LineZone`/`PolygonZone` adds ~10 KB; tracker state grows with object count but is capped by tracker eviction.
- **Storage**: `data/outbox.jsonl` writes frequently during network outages. Use high-endurance SD cards or eMMC.
- **Network**: Cellular fallback works but expect 5–30s latency on Supabase REST calls; tune `REQUEST_TIMEOUT_SECONDS` accordingly.
