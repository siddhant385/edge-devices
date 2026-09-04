"""Boot the edge pipeline: Supabase auth, services, camera tasks.

Extracted from the old ``async_main`` so the boot sequence reads top-to-bottom
in one place. ``Pipeline.create()`` returns a fully-wired object; ``run()``
starts the long-lived subscribers, spawns the camera tasks, and waits for
shutdown. ``close()`` tears everything down in reverse order.

The class hides the wiring details from ``main.py`` so the entry point
becomes three lines: build, run, close.
"""

from __future__ import annotations

import asyncio
import logging
from threading import Lock

import httpx
from supabase._async.client import AsyncClient as AsyncSupabaseClient
from supabase._async.client import create_client as create_async_client
from supabase.client import ClientOptions
from supabase_auth import AsyncMemoryStorage

from config.camera_settings import CameraSettings
from config.device_settings import DeviceSettings
from core.ai.camera_loop import CameraLoop, guarded
from core.ai.processor import OnnxProcessor
from core.cloud.ack_listener import AcknowledgementListener
from core.cloud.ack_tracker import AcknowledgementTracker
from core.cloud.camera_manager import CameraManager
from core.cloud.command_executor import CommandExecutor
from core.cloud.control_receiver import ControlReceiver, RuntimeSettingsStore
from core.cloud.metadata_reporter import (report_camera_coordinates,
                                          report_device_metadata)
from core.cloud.presence import PresencePublisher
from core.cloud.sender import AlertSender
from plugins import PluginServices
from utils.frame_broker import FrameBroker

logger = logging.getLogger(__name__)


class Pipeline:
    """Long-lived runtime for the edge device."""

    def __init__(
        self,
        settings: DeviceSettings,
        supabase: AsyncSupabaseClient,
        device_uuid: str,
        processor: OnnxProcessor,
        services: PluginServices,
        camera_manager: CameraManager,
        runtime_settings: RuntimeSettingsStore,
        control_receiver: ControlReceiver,
        presence: PresencePublisher,
        ack_tracker: AcknowledgementTracker,
        ack_listener: AcknowledgementListener,
        sender: AlertSender,
        command_executor: CommandExecutor,
        frame_broker: FrameBroker,
        cameras: dict[str, CameraSettings],
    ) -> None:
        self._settings = settings
        self._supabase = supabase
        self._device_uuid = device_uuid
        self._processor = processor
        self._services = services
        self._camera_manager = camera_manager
        self._runtime_settings = runtime_settings
        self._control_receiver = control_receiver
        self._presence = presence
        self._ack_tracker = ack_tracker
        self._ack_listener = ack_listener
        self._sender = sender
        self._command_executor = command_executor
        self._frame_broker = frame_broker
        self._cameras = cameras
        self._stop: asyncio.Event | None = None

    @classmethod
    async def create(cls, settings: DeviceSettings) -> "Pipeline":
        """Sign in, push metadata, load cameras, wire all services.

        Order matters and is documented inline.
        """
        supabase = await _create_supabase_client(settings)
        device_uuid = await _sign_in_and_resolve(supabase, settings)
        await report_device_metadata(supabase, settings.device_id, settings.location)

        camera_manager = CameraManager(supabase, settings.device_id)
        camera_manager.load_local_cameras()
        await camera_manager.sync_with_cloud()
        cameras = camera_manager._cameras  # noqa: SLF001 - post-sync, keys are true UUIDs

        for cam in cameras.values():
            if cam.latitude is not None and cam.longitude is not None:
                await report_camera_coordinates(
                    supabase, cam.camera_id, cam.latitude, cam.longitude, cam.location
                )

        processor = _build_processor(settings, cameras)
        services = PluginServices(processor=processor, inference_lock=Lock())
        runtime_settings = RuntimeSettingsStore(settings, camera_manager)
        control_receiver = ControlReceiver(supabase, settings, runtime_settings, camera_manager)
        presence = PresencePublisher(supabase, device_uuid, settings.device_id)
        ack_tracker = AcknowledgementTracker(ttl_seconds=300.0)
        ack_listener = AcknowledgementListener(supabase, device_uuid, ack_tracker)
        sender = AlertSender(
            supabase,
            settings.queue_path,
            settings.queue_max_records,
            device_uuid=device_uuid,
            device_id_str=settings.device_id,
            ack_tracker=ack_tracker,
        )
        frame_broker = FrameBroker()
        command_executor = CommandExecutor(supabase, settings, frame_broker)

        return cls(
            settings=settings,
            supabase=supabase,
            device_uuid=device_uuid,
            processor=processor,
            services=services,
            camera_manager=camera_manager,
            runtime_settings=runtime_settings,
            control_receiver=control_receiver,
            presence=presence,
            ack_tracker=ack_tracker,
            ack_listener=ack_listener,
            sender=sender,
            command_executor=command_executor,
            frame_broker=frame_broker,
            cameras=cameras,
        )

    async def run(self) -> None:
        """Start all long-lived services, run camera loops, wait for shutdown."""
        self._stop = asyncio.Event()
        try:
            await self._control_receiver.start()
            await self._presence.start()
            await self._command_executor.start()
            await self._ack_listener.start()
            if self._settings.enable_sending:
                await self._sender.start()

            camera_tasks = [
                asyncio.create_task(
                    guarded(
                        CameraLoop(
                            camera=camera,
                            device_settings=self._settings,
                            services=self._services,
                            sender=self._sender,
                            stop=self._stop,
                            runtime_settings=self._runtime_settings,
                            supabase=self._supabase,
                            frame_broker=self._frame_broker,
                        ).run()
                    ),
                    name=f"camera-{camera.camera_id}",
                )
                for camera in self._cameras.values()
            ]
            logger.info("Started %d camera loop(s)", len(camera_tasks))
            await self._stop.wait()
            for task in camera_tasks:
                task.cancel()
            for task in camera_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        except KeyboardInterrupt:
            logger.info("Stopping edge pipeline")
            if self._stop is not None:
                self._stop.set()

    async def close(self) -> None:
        """Tear down every long-lived service. Order is reverse of start."""
        await self._ack_listener.close()
        await self._presence.close()
        await self._control_receiver.close()
        await self._command_executor.close()
        await self._sender.close()


async def _create_supabase_client(settings: DeviceSettings) -> AsyncSupabaseClient:
    options = ClientOptions(postgrest_client_timeout=30.0, storage=AsyncMemoryStorage())
    client = await create_async_client(
        settings.supabase_url, settings.api_key or "", options=options
    )
    if hasattr(client.auth, "_http_client"):
        client.auth._http_client = httpx.AsyncClient(timeout=60.0)
    return client


async def _sign_in_and_resolve(
    supabase: AsyncSupabaseClient, settings: DeviceSettings
) -> str:
    try:
        response = await supabase.auth.sign_in_with_password(
            {"email": settings.device_email, "password": settings.device_password}
        )
    except Exception:
        logger.exception("Failed to authenticate with Supabase on startup:")
        raise
    logger.info("Authenticated with Supabase")
    user_id = response.user.id
    device_response = (
        await supabase.table("devices").select("id").eq("auth_user_id", user_id).execute()
    )
    if not device_response.data:
        raise RuntimeError(f"No device row associated with user {user_id}")
    return device_response.data[0]["id"]


def _build_processor(
    settings: DeviceSettings, cameras: dict[str, CameraSettings]
) -> OnnxProcessor:
    """Use the first camera's settings to size the shared processor.

    All cameras sharing one processor must agree on inference_size and
    target_class_ids; per-camera thresholds are honored by the plugin layer.
    """
    first = next(iter(cameras.values()), None) if cameras else None
    return OnnxProcessor(
        settings.model_path,
        inference_size=first.inference_size if first else 640,
        confidence_threshold=first.confidence_threshold if first else 0.45,
        nms_threshold=first.nms_threshold if first else 0.5,
        target_class_ids=first.target_class_ids if first else frozenset({0}),
    )
