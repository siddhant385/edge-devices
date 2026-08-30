"""Run the edge AI surveillance pipeline."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import cv2
import supervision as sv
from supabase._async.client import AsyncClient as AsyncSupabaseClient
from supabase.client import ClientOptions
from supabase._async.client import create_client as create_async_client

from config.settings import CameraSettings, Settings
from core.control_receiver import ControlReceiver, RuntimeSettingsStore
from core.heartbeat import HeartbeatSender
from core.processor import OnnxProcessor
from core.receiver import CameraReceiver
from core.sender import AlertSender
from plugins import PluginManager, PluginServices


async def run_camera_async(
    camera: CameraSettings,
    settings: Settings,
    services: PluginServices,
    sender: AlertSender,
    stop: asyncio.Event,
    runtime_settings: RuntimeSettingsStore,
    supabase: AsyncSupabaseClient,
) -> None:
    """Run one camera with independent stateful plugins such as tracking."""
    receiver = CameraReceiver(camera.source, settings.reconnect_delay_seconds)
    
    main_loop = asyncio.get_running_loop()
    
    def on_camera_online_change(is_online: bool):
        # Fire-and-forget task to update DB safely from a different thread
        main_loop.call_soon_threadsafe(
            lambda: main_loop.create_task(sender.update_camera_status(camera.camera_id, is_online))
        )
        
    receiver.set_online_callback(on_camera_online_change)
    
    active_settings, generation = runtime_settings.snapshot()
    base_frame_skip = active_settings.process_every_n_frames
    plugins = PluginManager(active_settings, services)
    
    # We will track if we are in "motion/active" state to drop the frame skip interval
    active_state_frames_left = 0
    motion_skip = min(2, base_frame_skip) # Drop down to max 2 frame skip (e.g. process every 2nd frame) when active
    frame_number = 0
    box_annotator = sv.BoxAnnotator()
    label_annotator = sv.LabelAnnotator(
        text_color=sv.Color.WHITE,
        text_scale=0.5,
        text_thickness=1,
        text_padding=10
    )
    # Actually cv2 uses text_font in cv2.putText... wait, the supervision LabelAnnotator doesn't accept font kwargs directly unless we subclass or override. 
    # Supervision explicitly uses cv2.FONT_HERSHEY_SIMPLEX internally
    
    latest_detections = None
    
    try:
        # Use run_in_executor since receiver.frames() is a blocking generator
        loop = asyncio.get_running_loop()
        
        for frame in receiver.frames():
            if stop.is_set():
                break
                
            active_settings, next_generation = runtime_settings.snapshot()
            if next_generation != generation:
                plugins = PluginManager(active_settings, services)
                generation = next_generation
                base_frame_skip = active_settings.process_every_n_frames
                motion_skip = min(2, base_frame_skip)
                logging.info("Camera %s loaded runtime configuration generation %d", camera.camera_id, generation)
                
            frame_number += 1
            current_skip = motion_skip if active_state_frames_left > 0 else base_frame_skip
            run_inference = (frame_number % current_skip == 0)
            
            if active_state_frames_left > 0:
                active_state_frames_left -= 1
            
            if run_inference:
                # Run the heavy ONNX detection in background executor
                detections, events = await loop.run_in_executor(None, plugins.process, frame)
                latest_detections = detections
                
                # If we detected something, keep the "active state" alive for the next 30 frames
                if len(detections) > 0:
                    active_state_frames_left = 30
                
                event_detections = sum(len(event.detections) for event in events)
                if event_detections or active_settings.send_empty_detections:
                    if "class_name" in detections.data:
                        logging.info(
                            "Camera %s frame %d: %s",
                            camera.camera_id,
                            frame_number,
                            ", ".join(
                                f"{name} ({confidence:.0%})"
                                for name, confidence in zip(
                                    detections.data["class_name"], detections.confidence, strict=True
                                )
                            ) or "no target detections",
                        )
                    else:
                        logging.info(
                            "Camera %s frame %d: %d detections (tracking initialized)",
                            camera.camera_id,
                            frame_number,
                            len(detections)
                        )
                        
                    if settings.enable_sending:
                        await sender.send(events, settings.device_id, camera.camera_id)
            else:
                # Small yield to let other tasks run when skipping frames
                await asyncio.sleep(0.001)

            # ALWAYS update the preview to maintain full FPS video stream
            if settings.show_preview:
                preview = frame.copy()
                
                # Draw the most recent bounding boxes we have
                if latest_detections is not None and "class_name" in latest_detections.data:
                    labels = [
                        f"{name} {confidence:.0%}"
                        for name, confidence in zip(
                            latest_detections.data["class_name"], latest_detections.confidence, strict=True
                        )
                    ]
                    preview = box_annotator.annotate(scene=preview, detections=latest_detections)
                    preview = label_annotator.annotate(
                        scene=preview, detections=latest_detections, labels=labels
                    )
                    
                cv2.imshow(f"IBVAP {camera.camera_id} - press Q or Esc to quit", preview)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    stop.set()
                    break
                    
    finally:
        receiver.close()


async def async_main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_environment()
    
    # Initialize Supabase client
    import httpx
    from supabase_auth import AsyncMemoryStorage

    # Apply a broader timeout to the underlying HTTP client for edge connections
    options = ClientOptions(postgrest_client_timeout=30.0, storage=AsyncMemoryStorage())
    supabase = await create_async_client(settings.supabase_url, settings.api_key or "", options=options)
    
    # Auth loop
    async def maintain_session():
        while True:
            # Sleep for 45 minutes before refreshing
            await asyncio.sleep(45 * 60)
            try:
                # Need to refresh token, or just sign in again
                await supabase.auth.sign_in_with_password({
                    "email": settings.device_email,
                    "password": settings.device_password
                })
                logging.info("Refreshed Supabase Auth Session")
            except Exception as e:
                logging.error(f"Failed to refresh Supabase Auth: {e}")

    # Initial Authentication
    try:
        # Override the http client inside the auth module directly to ensure the timeout applies
        if hasattr(supabase.auth, "_http_client"):
            supabase.auth._http_client = httpx.AsyncClient(timeout=30.0)
            
        await supabase.auth.sign_in_with_password({
            "email": settings.device_email,
            "password": settings.device_password
        })
        logging.info("Authenticated with Supabase")
    except Exception as e:
        logging.exception("Failed to authenticate with Supabase on startup:")
        return

    asyncio.create_task(maintain_session())
    
    processor = OnnxProcessor(
        settings.model_path,
        settings.inference_size,
        settings.confidence_threshold,
        settings.nms_threshold,
        settings.target_class_ids,
    )
    services = PluginServices(processor=processor, inference_lock=Lock())
    runtime_settings = RuntimeSettingsStore(settings)
    control_receiver = ControlReceiver(supabase, settings, runtime_settings)
    sender = AlertSender(
        supabase,
        settings.queue_path,
        settings.queue_max_records,
    )
    
    # We define a dummy queue depth since we're replacing the synchronous one
    def queue_depth_sync():
        return 0
        
    heartbeat = HeartbeatSender(
        supabase,
        settings,
        cameras_active=len(settings.cameras),
        queue_depth_fn=queue_depth_sync,
    )
    stop = asyncio.Event()
    
    try:
        await control_receiver.start()
        await heartbeat.start()
        
        if settings.enable_sending:
            await sender.start()
            
        tasks = [
            asyncio.create_task(run_camera_async(camera, settings, services, sender, stop, runtime_settings, supabase))
            for camera in settings.cameras
        ]
        
        await asyncio.gather(*tasks)
            
    except KeyboardInterrupt:
        logging.info("Stopping edge pipeline")
        stop.set()
    finally:
        await control_receiver.close()
        await heartbeat.close()
        await sender.close()
        if settings.show_preview:
            cv2.destroyAllWindows()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
