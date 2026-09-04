"""Per-camera frame loop: receive, process, dispatch, preview.

The class extracts the body of the old ``run_camera_async`` free function in
``main.py``. The seven concerns of the old function are now seven small
methods, and the frame-skip arithmetic is a pure function (``should_run``)
that can be unit-tested without a frame.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import cv2
import numpy as np
import supervision as sv
from supabase._async.client import AsyncClient as AsyncSupabaseClient

from config.camera_settings import CameraSettings
from config.device_settings import DeviceSettings
from core.ai.receiver import CameraReceiver
from core.cloud.metadata_reporter import report_camera_stream_metadata
from core.cloud.sender import AlertSender
from plugins import PluginManager, PluginServices
from core.cloud.control_receiver import RuntimeSettingsStore
from utils.frame_broker import FrameBroker

logger = logging.getLogger(__name__)

# When the previous frame produced any detection, hold the camera in
# "active" mode for this many frames so we don't miss the rest of the burst.
ACTIVE_HOLD_FRAMES = 30
# During active mode, drop the frame skip to at most this many frames
# so we capture fast-moving objects.
ACTIVE_MAX_FRAME_SKIP = 2
# How often the preview window polls for keypress.
PREVIEW_POLL_MS = 1


def should_run_inference(
    frame_number: int,
    base_skip: int,
    active_hold: int,
) -> tuple[bool, int]:
    """Decide whether to run inference this frame and the new active hold.

    Returns (run, new_active_hold). Pure function - testable without a
    receiver, plugin manager, or Supabase client.
    """
    if active_hold > 0:
        new_hold = active_hold - 1
    else:
        new_hold = 0
    effective_skip = min(ACTIVE_MAX_FRAME_SKIP, base_skip) if active_hold > 0 else base_skip
    run = effective_skip > 0 and frame_number % effective_skip == 0
    return run, new_hold


@dataclass
class _FrameState:
    """Per-camera runtime state. Owned by CameraLoop, mutated in place."""
    plugins: PluginManager
    generation: int
    frame_number: int
    base_frame_skip: int
    motion_skip: int
    active_hold: int
    latest_detections: sv.Detections | None


class CameraLoop:
    """One asyncio task per camera. Owns its own stateful plugins."""

    def __init__(
        self,
        camera: CameraSettings,
        device_settings: DeviceSettings,
        services: PluginServices,
        sender: AlertSender,
        stop: asyncio.Event,
        runtime_settings: RuntimeSettingsStore,
        supabase: AsyncSupabaseClient,
        frame_broker: FrameBroker | None = None,
    ) -> None:
        self._camera = camera
        self._device = device_settings
        self._services = services
        self._sender = sender
        self._stop = stop
        self._runtime = runtime_settings
        self._supabase = supabase

        self._receiver = CameraReceiver(
            camera.source,
            device_settings.reconnect_delay_seconds,
            camera.camera_id,
            frame_broker=frame_broker,
        )
        # The online callback fires from the receiver's background thread.
        # We need an asyncio loop reference to call_soon_threadsafe, and
        # `asyncio.get_running_loop()` only works from the thread that owns
        # the loop. Capture it here in the asyncio task's thread before
        # the reader thread spins up.
        self._loop = asyncio.get_event_loop()
        self._box_annotator = sv.BoxAnnotator()
        self._label_annotator = sv.LabelAnnotator(
            text_color=sv.Color.WHITE, text_scale=0.5, text_thickness=1, text_padding=10
        )
        self._state: _FrameState | None = None

    async def run(self) -> None:
        """The camera's lifecycle. One task per camera. Errors are logged, not raised."""
        self._receiver.set_online_callback(self._on_camera_online_change)
        self._receiver.start()
        try:
            self._state = self._initial_state()
            while not self._stop.is_set():
                frame = await self._next_frame()
                if frame is None:
                    continue
                self._maybe_reload_settings()
                await self._handle_frame(frame)
                self._maybe_render_preview(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Camera loop crashed for %s", self._camera.camera_id)
        finally:
            self._receiver.close()

    # --- helpers --------------------------------------------------

    async def _next_frame(self) -> np.ndarray | None:
        try:
            return await self._receiver.next_frame(timeout_seconds=1.0)
        except Exception as error:
            logger.error("Camera %s frame fetch error: %s", self._camera.camera_id, error)
            return None

    def _initial_state(self) -> _FrameState:
        active, generation = self._runtime.snapshot_camera(self._camera.camera_id)
        settings = active or self._camera
        base_skip = settings.process_every_n_frames
        return _FrameState(
            plugins=PluginManager(settings, self._services),
            generation=generation,
            frame_number=0,
            base_frame_skip=base_skip,
            motion_skip=min(ACTIVE_MAX_FRAME_SKIP, base_skip),
            active_hold=0,
            latest_detections=None,
        )

    def _maybe_reload_settings(self) -> None:
        assert self._state is not None
        active, next_generation = self._runtime.snapshot_camera(self._camera.camera_id)
        if not active or next_generation == self._state.generation:
            return
        base_skip = active.process_every_n_frames
        self._state.plugins = PluginManager(active, self._services)
        self._state.generation = next_generation
        self._state.base_frame_skip = base_skip
        self._state.motion_skip = min(ACTIVE_MAX_FRAME_SKIP, base_skip)
        logger.info(
            "Camera %s loaded runtime configuration generation %d",
            self._camera.camera_id, next_generation,
        )

    async def _handle_frame(self, frame: np.ndarray) -> None:
        assert self._state is not None
        self._state.frame_number += 1
        run, new_hold = should_run_inference(
            self._state.frame_number,
            self._state.base_frame_skip,
            self._state.active_hold,
        )
        self._state.active_hold = new_hold
        if not run:
            await asyncio.sleep(0.001)  # let other coroutines breathe
            return
        try:
            loop = asyncio.get_running_loop()
            detections, raw_events = await loop.run_in_executor(
                None, self._state.plugins.process, frame
            )
        except Exception as error:
            logger.error(
                "Inference or plugin processing failed for camera %s: %s",
                self._camera.camera_id, error,
            )
            return
        self._state.latest_detections = detections
        events = [e for e in raw_events if e.feature in self._active_settings().enabled_plugins]
        if len(detections) > 0:
            self._state.active_hold = ACTIVE_HOLD_FRAMES
        self._log_detections(detections, events)
        if sum(len(e.detections) for e in events) or self._device.send_empty_detections:
            if self._device.enable_sending:
                for event in events:
                    logger.info("Sending event from enabled feature: %s", event.feature)
                await self._sender.send(
                    events,
                    self._device.device_id,
                    self._camera.camera_id,
                    cooldown_seconds=self._active_settings().cooldown_seconds,
                    severity=self._active_settings().severity,
                    zone_id_map=dict(self._active_settings().zone_id_map),
                )

    def _log_detections(self, detections: sv.Detections, events: list) -> None:
        if "class_name" in detections.data:
            summary = ", ".join(
                f"{n} ({c:.0%})"
                for n, c in zip(detections.data["class_name"], detections.confidence, strict=True)
            ) or "no target detections"
        else:
            summary = f"{len(detections)} detections (tracking initialized)"
        logger.debug(
            "Camera %s frame %d: %s",
            self._camera.camera_id, self._state.frame_number, summary,
        )

    def _active_settings(self) -> CameraSettings:
        """Return the live settings (from runtime store) for the current generation."""
        active, _ = self._runtime.snapshot_camera(self._camera.camera_id)
        return active or self._camera

    def _maybe_render_preview(self, frame: np.ndarray) -> None:
        if not self._device.show_preview:
            return
        try:
            preview = frame.copy()
            assert self._state is not None
            latest = self._state.latest_detections
            if latest is not None and "class_name" in latest.data:
                labels = [
                    f"{n} {c:.0%}"
                    for n, c in zip(latest.data["class_name"], latest.confidence, strict=True)
                ]
                preview = self._box_annotator.annotate(scene=preview, detections=latest)
                preview = self._label_annotator.annotate(
                    scene=preview, detections=latest, labels=labels
                )
            assert self._state is not None
            cv2.imshow(
                f"IBVAP {self._camera.camera_id} - press Q or Esc to quit",
                self._state.plugins.annotate_preview(preview),
            )
            if cv2.waitKey(PREVIEW_POLL_MS) & 0xFF in (ord("q"), 27):
                self._stop.set()
        except Exception as error:
            logger.error("Preview rendering failed: %s", error)

    def _on_camera_online_change(self, is_online: bool) -> None:
        """Callback fired on the receiver thread; hop to the asyncio loop."""
        async def _update():
            try:
                await self._supabase.table("cameras").update({"is_online": is_online}).eq(
                    "camera_id", self._camera.camera_id
                ).execute()
                if is_online and self._receiver.capture is not None:
                    await report_camera_stream_metadata(
                        self._supabase, self._camera.camera_id, self._receiver.capture
                    )
            except Exception as error:
                logger.error(
                    "Failed to update online status for camera %s: %s",
                    self._camera.camera_id, error,
                )

        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(_update()))


async def guarded(coro) -> None:
    """Run a coroutine; log + swallow exceptions so one camera's death
    doesn't cascade-cancel the others via ``asyncio.gather``.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Guarded task crashed; continuing.")
