"""Manager for local camera JSON definitions and cloud syncing."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.camera_settings import CameraSettings

logger = logging.getLogger(__name__)

class CameraManager:
    def __init__(self, supabase: AsyncSupabaseClient, device_id: str):
        self.supabase = supabase
        self.device_id = device_id
        self.cameras_dir = Path("cameras")
        self.cameras_dir.mkdir(exist_ok=True)
        self._cameras: dict[str, CameraSettings] = {}

    def load_local_cameras(self) -> dict[str, CameraSettings]:
        self._cameras.clear()
        for json_file in self.cameras_dir.glob("*.json"):
            try:
                with open(json_file, "r") as f:
                    data = json.load(f)

                if "source" not in data:
                    logger.warning("Camera config %s missing 'source'. Skipping.", json_file.name)
                    continue

                if "id" not in data or not data["id"]:
                    data["id"] = str(uuid.uuid4())
                    with open(json_file, "w") as f:
                        json.dump(data, f, indent=4)
                    logger.info("Generated new UUID for camera in %s", json_file.name)
                
                settings = CameraSettings.from_dict(data)
                self._cameras[settings.camera_id] = settings
            except Exception as e:
                logger.error("Failed to load camera config %s: %s", json_file.name, e)
        return self._cameras

    async def sync_with_cloud(self) -> None:
        """
        Ensure all local cameras exist in the cloud and sync settings.
        Replaces local string slugs with true Postgres UUIDs where necessary.
        """
        if not self._cameras:
            logger.info("No local cameras found to sync.")
            return

        # First, resolve the actual UUID of the device from the devices table
        device_resp = await self.supabase.table("devices").select("id").eq("device_id", self.device_id).execute()
        if not device_resp.data:
            logger.error("Device '%s' not found in devices table. Cannot register cameras.", self.device_id)
            return
            
        device_uuid = device_resp.data[0]["id"]
        
        # We will build a fresh dictionary mapped by the true Postgres UUID
        updated_cameras: dict[str, CameraSettings] = {}

        for old_cam_id, settings in self._cameras.items():
            # 1. Is this already a valid UUID? Let's check the database.
            # We search BOTH the primary key `id` and the text `camera_id` column just in case.
            resp = await self.supabase.table("cameras").select("id").eq("camera_id", old_cam_id).execute()
            
            true_uuid = None
            if not resp.data:
                # Could not find it by string slug. Register new camera.
                logger.info("Registering new camera %s to cloud.", old_cam_id)
                insert_resp = await self.supabase.table("cameras").insert({
                    "device_id": device_uuid,
                    "camera_id": old_cam_id, # We store the original slug here temporarily to satisfy unique constraint
                    "source_url": settings.source,
                    "name": f"Camera {old_cam_id[:8]}"
                }).execute()
                true_uuid = insert_resp.data[0]["id"]
                
                # Immediately update the database so 'camera_id' matches 'id'
                await self.supabase.table("cameras").update({
                    "camera_id": true_uuid
                }).eq("id", true_uuid).execute()
            else:
                true_uuid = resp.data[0]["id"]
                
            # If the ID changed from slug to UUID, update the object and delete the old JSON file
            if old_cam_id != true_uuid:
                logger.info("Upgrading local camera ID from slug '%s' to true UUID '%s'", old_cam_id, true_uuid)
                old_filepath = self.cameras_dir / f"{old_cam_id}.json"
                if old_filepath.exists():
                    old_filepath.unlink()
                
                # Rebuild settings with the true UUID
                settings_dict = settings.to_dict()
                settings_dict["id"] = true_uuid
                settings = CameraSettings.from_dict(settings_dict)

            updated_cameras[true_uuid] = settings
            
            # Upsert camera settings using the True UUID
            settings_resp = await self.supabase.table("camera_settings").select("settings").eq("camera_id", true_uuid).execute()
            if not settings_resp.data:
                logger.info("Pushing local settings for camera %s to cloud.", true_uuid)
                await self.supabase.table("camera_settings").insert({
                    "camera_id": true_uuid,
                    "settings": settings.to_dict()
                }).execute()
            else:
                # Cloud has settings, overwrite local object
                logger.info("Loaded cloud settings for camera %s.", true_uuid)
                cloud_data = settings_resp.data[0]["settings"]
                if "source" not in cloud_data:
                    cloud_data["source"] = settings.source
                # Field ownership rule: physical fields (source, lat, lon,
                # location) are set at install time on the edge. The cloud
                # may store them for display, but it must not push them
                # back. Only TUNING_FIELDS flow from cloud to edge.
                from config.camera_settings import TUNING_FIELDS
                cloud_tuning = {k: v for k, v in cloud_data.items() if k in TUNING_FIELDS}
                local_dict = settings.to_dict()
                merged = {**local_dict, **cloud_tuning, "id": true_uuid}
                settings = CameraSettings.from_dict(merged)
                updated_cameras[true_uuid] = settings
                
            # Always save to ensure local reflects True UUID and latest settings
            self._save_local(settings)
                
        self._cameras = updated_cameras

    def _save_local(self, settings: CameraSettings) -> None:
        """Save settings back to local JSON."""
        # Find file or create new
        filepath = self.cameras_dir / f"{settings.camera_id}.json"
        with open(filepath, "w") as f:
            json.dump(settings.to_dict(), f, indent=4)

    def get_settings(self, camera_id: str) -> CameraSettings | None:
        return self._cameras.get(camera_id)

    def update_settings(self, camera_id: str, new_settings: CameraSettings) -> None:
        self._cameras[camera_id] = new_settings
        self._save_local(new_settings)
