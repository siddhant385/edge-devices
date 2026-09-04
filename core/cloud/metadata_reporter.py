"""Hardware and Stream Metadata Reporting."""

from __future__ import annotations

import logging
import platform

try:
    import psutil
except ImportError:
    psutil = None

try:
    import cv2
except ImportError:
    cv2 = None

from supabase._async.client import AsyncClient as AsyncSupabaseClient

logger = logging.getLogger(__name__)

async def report_device_metadata(
    supabase: AsyncSupabaseClient, device_id: str, location: str | None = None
) -> None:
    """Gather hardware info and update the devices table."""
    try:
        info = {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
        }

        if psutil:
            info["cpu_cores"] = psutil.cpu_count(logical=True)
            info["ram_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 2)

        update: dict = {"device_info": info}
        if location:
            update["location"] = location

        await supabase.table("devices").update(update).eq("device_id", device_id).execute()
        logger.info("Updated device hardware metadata for %s", device_id)
    except Exception as e:
        logger.error("Failed to report device metadata: %s", e)

async def report_camera_stream_metadata(supabase: AsyncSupabaseClient, camera_id: str, cap: cv2.VideoCapture | None) -> None:
    """Extract stream details from OpenCV and update the cameras table."""
    if not cap or not cv2:
        return

    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        codec = int(cap.get(cv2.CAP_PROP_FOURCC))

        info = {
            "native_width": width,
            "native_height": height,
            "native_fps": fps,
            "codec": codec,
        }

        await supabase.table("cameras").update({
            "stream_info": info
        }).eq("camera_id", camera_id).execute()
        logger.info("Updated stream metadata for camera %s", camera_id)
    except Exception as e:
        logger.error("Failed to report camera metadata for %s: %s", camera_id, e)


async def report_camera_coordinates(
    supabase: AsyncSupabaseClient,
    camera_id: str,
    latitude: float,
    longitude: float,
    location: str | None = None,
) -> None:
    """Push GPS coordinates to cameras.coordinates so the trigger back-fills
    detections.camera_coords and alerts.camera_coords.

    PostgREST casts a 'lon,lat' string to the point type automatically.
    Longitude FIRST, then latitude. Optional `location` sets the human-readable
    text column (e.g. "North Post Alpha, Sector 7G") in the same write.
    """
    try:
        update: dict = {"coordinates": f"{longitude},{latitude}"}
        if location:
            update["location"] = location
        await (
            supabase.table("cameras")
            .update(update)
            .eq("camera_id", camera_id)
            .execute()
        )
        logger.info(
            "Updated coordinates for camera %s: (%s, %s) location=%s",
            camera_id, latitude, longitude, location,
        )
    except Exception as e:
        logger.error("Failed to report coordinates for camera %s: %s", camera_id, e)
