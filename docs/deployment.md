# Deployment

This guide covers deploying the edge AI pipeline on a constrained device (e.g., Raspberry Pi, NVIDIA Jetson, or an AIoT Edge Box running Linux).

## Prerequisites

*   Python 3.10 or higher.
*   `uv` for fast dependency management (recommended).
*   A pre-trained ONNX model (e.g., `yolov8n.onnx`).

## 1. Setup Environment

We recommend using `uv` to create virtual environments and manage dependencies. It is significantly faster than standard `pip`.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone <repository_url> edge-ai-client
cd edge-ai-client

# Create a virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## 2. Configuration

Copy the example environment file and configure it for your deployment.

```bash
cp .env.example .env
nano .env
```

Ensure the following variables are correctly set for production:
*   `DEVICE_ID`: Unique identifier (e.g., `edge-border-north-1`).
*   `API_URL`: The central server alert endpoint.
*   `API_KEY`: Authentication token for the central server.
*   `SHOW_PREVIEW=false`: Must be false for headless deployments.

## 3. Running the Pipeline

Execute the main entry point:

```bash
python main.py
```

## 4. Running as a Systemd Service

To ensure the edge client starts automatically on boot and restarts if it crashes, configure it as a `systemd` service.

1.  Create a service file: `sudo nano /etc/systemd/system/edge-ai.service`

```ini
[Unit]
Description=Edge AI Surveillance Client
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/edge-ai-client
ExecStart=/home/pi/edge-ai-client/.venv/bin/python main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

*(Adjust `User`, `WorkingDirectory`, and `ExecStart` paths according to your system).*

2.  Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable edge-ai.service
sudo systemctl start edge-ai.service
```

3.  Check logs:

```bash
sudo journalctl -u edge-ai.service -f
```

## Hardware Considerations

*   **CPU:** The system defaults to using the ONNX `CPUExecutionProvider`. Performance is highly dependent on CPU capabilities. Adjust `PROCESS_EVERY_N_FRAMES` and `INFERENCE_SIZE` to prevent thermal throttling.
*   **Memory:** Monitor memory usage, especially if using large values for `QUEUE_MAX_RECORDS` or processing many concurrent camera streams.
*   **Storage:** The `data` directory (containing `outbox.jsonl`) will experience frequent writes if the network is unstable. Use high-endurance SD cards or eMMC storage for long-term reliability.