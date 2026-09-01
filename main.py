"""Run the edge AI surveillance pipeline."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import cv2
import supervision as sv
from supabase._async.client import AsyncClient as AsyncSupabaseClient
from supabase._async.client import create_client as create_async_client
from supabase.client import ClientOptions

from config.camera_settings import CameraSettings
from config.device_settings import DeviceSettings
from core.ai.processor import OnnxProcessor
from core.ai.receiver import CameraReceiver
from core.cloud.camera_manager import CameraManager
from core.cloud.command_executor import CommandExecutor
from core.cloud.control_receiver import ControlReceiver, RuntimeSettingsStore
from core.cloud.metadata_reporter import (report_camera_stream_metadata,
                                          report_device_metadata)
from core.cloud.sender import AlertSender
from plugins import PluginManager, PluginServices
from utils.frame_broker import FrameBroker


async def run_camera_async(
    camera: CameraSettings,
    device_settings: DeviceSettings,
    services: PluginServices,
    sender: AlertSender,
    stop: asyncio.Event,
    runtime_settings: RuntimeSettingsStore,
    supabase: AsyncSupabaseClient,
    frame_broker: FrameBroker,
) -> None:
    """Run one camera with independent stateful plugins such as tracking."""
    receiver = CameraReceiver(
        camera.source,
        device_settings.reconnect_delay_seconds,
        camera.camera_id,
        frame_broker,
    )

    main_loop = asyncio.get_running_loop()

    def on_camera_online_change(is_online: bool):
        async def _update_status():
            try:
                await supabase.table("cameras").update({"is_online": is_online}).eq(
                    "camera_id", camera.camera_id
                ).execute()
                if is_online and receiver._capture:
                    await report_camera_stream_metadata(
                        supabase, camera.camera_id, receiver._capture
                    )
            except Exception as e:
                logging.error(
                    "Failed to update online status for camera %s: %s",
                    camera.camera_id,
                    e,
                )

        # Safely schedule the async task from the background thread onto the main event loop
        main_loop.call_soon_threadsafe(lambda: main_loop.create_task(_update_status()))

    receiver.set_online_callback(on_camera_online_change)
    receiver.start()

    active_settings, generation = runtime_settings.snapshot_camera(camera.camera_id)
    if not active_settings:
        active_settings = camera

    base_frame_skip = active_settings.process_every_n_frames
    plugins = PluginManager(active_settings, services)

    # We will track if we are in "motion/active" state to drop the frame skip interval
    active_state_frames_left = 0
    motion_skip = min(
        2, base_frame_skip
    )  # Drop down to max 2 frame skip (e.g. process every 2nd frame) when active
    frame_number = 0
    box_annotator = sv.BoxAnnotator()
    label_annotator = sv.LabelAnnotator(
        text_color=sv.Color.WHITE, text_scale=0.5, text_thickness=1, text_padding=10
    )
    # Actually cv2 uses text_font in cv2.putText... wait, the supervision LabelAnnotator doesn't accept font kwargs directly unless we subclass or override.
    # Supervision explicitly uses cv2.FONT_HERSHEY_SIMPLEX internally

    latest_detections = None

    try:
        # Use run_in_executor since receiver.frames() is a blocking generator
        loop = asyncio.get_running_loop()

        while not stop.is_set():
            try:
                # Fetch frame asynchronously to prevent blocking the event loop (which starves Realtime)
                # This delegates the blocking queue.get to a background thread.
                import queue

                frame = await asyncio.to_thread(receiver._frame_queue.get, True, 1.0)
            except queue.Empty:
                continue
            except Exception as e:
                logging.error("Camera %s frame fetch error: %s", camera.camera_id, e)
                continue

            active_settings, next_generation = runtime_settings.snapshot_camera(
                camera.camera_id
            )
            if not active_settings:
                active_settings = camera

            next_gen_val = next_generation
            if next_gen_val != generation:
                plugins = PluginManager(active_settings, services)
                generation = next_gen_val
                base_frame_skip = active_settings.process_every_n_frames
                motion_skip = min(2, base_frame_skip)
                logging.info(
                    "Camera %s loaded runtime configuration generation %d",
                    camera.camera_id,
                    generation,
                )

            frame_number += 1
            current_skip = (
                motion_skip if active_state_frames_left > 0 else base_frame_skip
            )
            run_inference = frame_number % current_skip == 0

            if active_state_frames_left > 0:
                active_state_frames_left -= 1

            if run_inference:
                try:
                    # Run the heavy ONNX detection in background executor
                    detections, raw_events = await loop.run_in_executor(
                        None, plugins.process, frame
                    )
                    latest_detections = detections

                    # 1. SAFELY FILTER EVENTS IN ONE LINE
                    # This automatically handles object_detection, virtual_border, etc.
                    # by checking if the feature is in the active settings.
                    events = [
                        event
                        for event in raw_events
                        if event.feature in active_settings.enabled_plugins
                    ]

                    # If we detected something, keep the "active state" alive for the next 30 frames
                    if len(detections) > 0:
                        active_state_frames_left = 30

                    # Calculate detections based ONLY on the filtered events
                    event_detections = sum(len(event.detections) for event in events)

                    if event_detections or device_settings.send_empty_detections:
                        if "class_name" in detections.data:
                            logging.debug(
                                "Camera %s frame %d: %s",
                                camera.camera_id,
                                frame_number,
                                ", ".join(
                                    f"{name} ({confidence:.0%})"
                                    for name, confidence in zip(
                                        detections.data["class_name"],
                                        detections.confidence,
                                        strict=True,
                                    )
                                )
                                or "no target detections",
                            )
                        else:
                            logging.debug(
                                "Camera %s frame %d: %d detections (tracking initialized)",
                                camera.camera_id,
                                frame_number,
                                len(detections),
                            )

                        if device_settings.enable_sending:
                            for event in events:
                                logging.info(
                                    "Sending event from enabled feature: %s",
                                    event.feature,
                                )

                            await sender.send(
                                events, device_settings.device_id, camera.camera_id
                            )
                except Exception as e:
                    logging.error(
                        "Inference or plugin processing failed for camera %s: %s",
                        camera.camera_id,
                        e,
                    )
            else:
                # Small yield to let other tasks run when skipping frames
                await asyncio.sleep(0.001)

            # ALWAYS update the preview to maintain full FPS video stream
            if device_settings.show_preview:
                try:
                    preview = frame.copy()

                    # Draw the most recent bounding boxes we have
                    if (
                        latest_detections is not None
                        and "class_name" in latest_detections.data
                    ):
                        labels = [
                            f"{name} {confidence:.0%}"
                            for name, confidence in zip(
                                latest_detections.data["class_name"],
                                latest_detections.confidence,
                                strict=True,
                            )
                        ]
                        preview = box_annotator.annotate(
                            scene=preview, detections=latest_detections
                        )
                        preview = label_annotator.annotate(
                            scene=preview, detections=latest_detections, labels=labels
                        )

                    cv2.imshow(
                        f"IBVAP {camera.camera_id} - press Q or Esc to quit", preview
                    )
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                        stop.set()
                        break
                except Exception as e:
                    logging.error("Preview rendering failed: %s", e)

    finally:
        receiver.close()


async def async_main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    device_settings = DeviceSettings.from_environment()

    # Initialize Supabase client
    import httpx
    from supabase_auth import AsyncMemoryStorage

    # Apply a broader timeout to the underlying HTTP client for edge connections
    options = ClientOptions(postgrest_client_timeout=30.0, storage=AsyncMemoryStorage())
    supabase = await create_async_client(
        device_settings.supabase_url, device_settings.api_key or "", options=options
    )

    # Auth loop (Deleted - natively handled by supabase python client)

    # Initial Authentication
    try:
        # Override the http client inside the auth module directly to ensure the timeout applies
        if hasattr(supabase.auth, "_http_client"):
            supabase.auth._http_client = httpx.AsyncClient(timeout=60.0)

        auth_response = await supabase.auth.sign_in_with_password(
            {
                "email": device_settings.device_email,
                "password": device_settings.device_password,
            }
        )
        logging.info("Authenticated with Supabase")
    except Exception as e:
        logging.exception("Failed to authenticate with Supabase on startup:")
        return

    # Supabase Python client auto-refreshes tokens. We no longer need the custom loop.

    # 1. Resolve true Postgres UUID for the device
    user_id = auth_response.user.id
    device_response = (
        await supabase.table("devices")
        .select("id")
        .eq("auth_user_id", user_id)
        .execute()
    )
    if not device_response.data:
        logging.error("No device associated with user: %s", user_id)
        return
    device_uuid = device_response.data[0]["id"]

    await report_device_metadata(supabase, device_settings.device_id)

    camera_manager = CameraManager(supabase, device_settings.device_id)
    camera_manager.load_local_cameras()
    await camera_manager.sync_with_cloud()

    # We must reload because sync_with_cloud updates the keys to be True UUIDs
    cameras = camera_manager._cameras

    # Pick a dummy model settings, or grab from first camera
    inference_size = 640
    confidence_threshold = 0.45
    nms_threshold = 0.5
    target_class_ids = frozenset({0})
    if cameras:
        first_cam = list(cameras.values())[0]
        inference_size = first_cam.inference_size
        confidence_threshold = first_cam.confidence_threshold
        nms_threshold = first_cam.nms_threshold
        target_class_ids = first_cam.target_class_ids

    processor = OnnxProcessor(
        device_settings.model_path,
        inference_size,
        confidence_threshold,
        nms_threshold,
        target_class_ids,
    )
    services = PluginServices(processor=processor, inference_lock=Lock())
    runtime_settings = RuntimeSettingsStore(device_settings, camera_manager)
    control_receiver = ControlReceiver(
        supabase, device_settings, runtime_settings, camera_manager
    )
    sender = AlertSender(
        supabase,
        device_settings.queue_path,
        device_settings.queue_max_records,
        device_uuid=device_uuid,
        device_id_str=device_settings.device_id,
    )
    frame_broker = FrameBroker()
    command_executor = CommandExecutor(supabase, device_settings, frame_broker)

    # We define a dummy queue depth since we're replacing the synchronous one
    def queue_depth_sync():
        return 0

    stop = asyncio.Event()

    try:
        await control_receiver.start()
        await command_executor.start()

        if device_settings.enable_sending:
            await sender.start()

        tasks = [
            asyncio.create_task(
                run_camera_async(
                    camera,
                    device_settings,
                    services,
                    sender,
                    stop,
                    runtime_settings,
                    supabase,
                    frame_broker,
                )
            )
            for camera in cameras.values()
        ]

        await asyncio.gather(*tasks)

    except KeyboardInterrupt:
        logging.info("Stopping edge pipeline")
        stop.set()
    finally:
        await control_receiver.close()
        await command_executor.close()
        await sender.close()
        if device_settings.show_preview:
            cv2.destroyAllWindows()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
