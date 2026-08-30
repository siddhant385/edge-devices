# Edge-to-Supabase Direct Integration Plan

## Overview
This document outlines the architectural shift to have IBVAP Edge devices communicate directly with Supabase, eliminating the FastAPI middleware. This approach leverages Supabase's native REST APIs, Realtime WebSockets, and Storage capabilities to simplify deployment, reduce latency, and lower server costs.

---

## 1. Security & Authentication Strategy (Replacing Bcrypt)

Currently, the central server uses a static API key hashed with `bcrypt`. Since Edge devices will now talk directly to Supabase, we must adapt to Supabase's native authentication model (PostgREST + Row Level Security).

### The New Auth Flow (Supabase Auth for Devices)
We cannot securely pass a plaintext API key to Supabase PostgREST if it is hashed with bcrypt in the database. Instead, **Edge Devices will become First-Class Supabase Users**.

1. **Device Provisioning (Next.js Dashboard):**
   - When an operator clicks "Register Edge Device", the Next.js app uses the Supabase Admin API to create a new "User" in Supabase Auth.
   - Email: `edge-001@devices.ibvap.internal` (Virtual email)
   - Password: A strong, randomly generated 32-character string.
   - The Next.js UI displays this password **once** (just like the old API key).

2. **Edge Device Configuration:**
   - The edge device `.env` is updated:
     ```env
     SUPABASE_URL=https://<your-project>.supabase.co
     SUPABASE_ANON_KEY=<your-anon-key>
     DEVICE_EMAIL=edge-001@devices.ibvap.internal
     DEVICE_PASSWORD=<the-generated-password>
     ```

3. **Edge Startup Routine:**
   - On boot, the edge device calls `supabase.auth.sign_in_with_password()`.
   - Supabase returns a JWT (Access Token) valid for 1 hour, and a Refresh Token.
   - A background thread automatically uses the Refresh Token every 45 minutes to keep the session alive indefinitely.
   - Every request to Supabase Storage or Database uses this JWT.

4. **Row Level Security (RLS):**
   - We implement Postgres RLS policies:
     ```sql
     CREATE POLICY "Devices can insert own alerts" ON alerts
     FOR INSERT WITH CHECK (auth.uid() IN (SELECT auth_user_id FROM devices WHERE id = alerts.device_id));
     ```
   - This ensures a compromised edge device cannot read the watchlist or alter data belonging to other border posts.

---

## 2. Refactoring Edge Components

The `IBVAP-edge` repository will require modifications to three core network components. We will use the official `supabase-py` SDK or raw `aiohttp`/`requests` targeting Supabase endpoints.

### A. `sender.py` (Alert & Evidence Delivery)
**Current:** Base64-encodes images, wraps them in a massive JSON payload, and `POST`s to FastAPI.
**New Flow:**
1. **Upload Image:** If evidence exists, upload the raw JPEG bytes directly to Supabase Storage:
   ```python
   # PUT /storage/v1/object/evidence/{device_id}/{camera_id}/{date}/{uuid}.jpg
   res = supabase.storage.from_("evidence").upload(path, raw_jpeg_bytes)
   ```
2. **Insert Alert:** `POST` the JSON metadata (without base64) directly to the `alerts` table:
   ```python
   # POST /rest/v1/alerts
   supabase.table("alerts").insert({
       "device_id": device_id,
       "evidence_path": path, # from step 1
       "detection_count": len(detections)
   }).execute()
   ```

### B. `control_receiver.py` (Configuration Sync)
**Current:** Maintains a long-lived HTTP SSE connection to FastAPI (`EventSourceResponse`).
**New Flow:**
Utilize **Supabase Realtime** (WebSockets connected directly to Postgres).
1. Edge device subscribes to the `device_settings` table, filtered by its own `device_id`:
   ```python
   channel = supabase.channel(f"settings-{device_id}")
   channel.on(
       "postgres_changes",
       event="UPDATE",
       schema="public",
       table="device_settings",
       filter=f"device_id=eq.{device_id}",
       callback=handle_new_settings
   ).subscribe()
   ```
2. When the Next.js dashboard updates the virtual fence, Supabase instantly pushes the JSON payload over the WebSocket to the edge device. 
3. *Benefit:* Supabase Realtime handles auto-reconnection natively, dropping the need for our custom reconnect loops.

### C. `heartbeat.py` (Health Monitoring)
**Current:** `POST`s to FastAPI which runs an `UPDATE` query.
**New Flow:**
Direct `UPDATE` (or `UPSERT` into a dedicated health table) via PostgREST:
```python
supabase.table("devices").update({
    "is_online": True,
    "last_seen_at": "now()",
    "metrics": { "cpu": cpu_percent, "mem": mem_percent }
}).eq("id", device_id).execute()
```

---

## 3. Handling AI Inference (The Headless Worker)

Since FastAPI is gone, the ONNX Face/ANPR models need a new home.

1. **The Worker:** We extract the AI logic from the old FastAPI code into a standalone Python script (`ai_worker.py`).
2. **The Trigger:** The worker listens to Supabase Realtime for `INSERT` events on the `alerts` table where `class_id` indicates a person or vehicle.
3. **The Execution:** 
   - Downloads the `evidence.jpg` from Supabase Storage.
   - Runs the ONNX model (ArcFace/LPRNet).
   - Writes the resulting embeddings to the `face_results` or `known_faces` tables via Supabase DB API.
4. **Deployment:** This worker can run anywhere—on a cheap Linux VPS, a dedicated GPU server, or even inside a Docker container on ModelScope. It does not expose any web ports.

---

## Summary of Benefits
* **Cost:** Eliminated the need to host a public-facing Python web server.
* **Bandwidth:** Stopped sending 500KB Base64 strings inside JSON payloads.
* **Resilience:** Supabase Realtime WebSockets are far more robust than HTTP SSE for configuration syncing.
* **Security:** Transitioned from a custom bcrypt solution to industry-standard JWTs and Postgres Row Level Security.
