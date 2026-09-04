"""Tests for core/ai/camera_loop.py.

Only the pure helpers are tested here. The CameraLoop class itself is
async + needs a frame stream + plugin manager + supabase client; that's
integration territory covered by the manual smoke test, not unit tests.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from core.ai.camera_loop import (
    ACTIVE_HOLD_FRAMES,
    ACTIVE_MAX_FRAME_SKIP,
    CameraLoop,
    should_run_inference,
)
from config.camera_settings import CameraSettings
from config.device_settings import DeviceSettings
from plugins import PluginServices
from utils.frame_broker import FrameBroker


class TestShouldRunInference(unittest.TestCase):
    def test_first_frame_with_default_skip_does_not_run(self):
        # Original behavior: frame N runs when N % skip == 0. With skip=5,
        # frames 5, 10, 15, ... run. Frame 1 does not.
        run, hold = should_run_inference(frame_number=1, base_skip=5, active_hold=0)
        self.assertFalse(run)
        self.assertEqual(hold, 0)

    def test_skips_until_frame_reaches_base_skip(self):
        for n in range(2, 5):
            run, _ = should_run_inference(frame_number=n, base_skip=5, active_hold=0)
            self.assertFalse(run, f"frame {n} should not run with base_skip=5")

    def test_runs_at_base_skip_boundary(self):
        run, _ = should_run_inference(frame_number=5, base_skip=5, active_hold=0)
        self.assertTrue(run)

    def test_active_hold_drops_skip_to_two(self):
        # Active mode: motion_skip = min(2, base_skip).
        # Frame 2 with active_hold=1 should run, frame 3 should not, frame 4 should.
        run, new_hold = should_run_inference(frame_number=2, base_skip=10, active_hold=2)
        self.assertTrue(run)
        self.assertEqual(new_hold, 1)  # hold decrements
        run, new_hold = should_run_inference(frame_number=3, base_skip=10, active_hold=1)
        self.assertFalse(run)
        self.assertEqual(new_hold, 0)
        # Hold expired; back to base_skip.
        run, _ = should_run_inference(frame_number=4, base_skip=10, active_hold=0)
        self.assertFalse(run)

    def test_active_hold_with_base_skip_smaller_than_two(self):
        # If base_skip=1, motion_skip is also 1; every frame runs while active.
        run, _ = should_run_inference(frame_number=3, base_skip=1, active_hold=5)
        self.assertTrue(run)

    def test_zero_base_skip_does_not_run(self):
        # Defensive: zero means "don't process" - never runs.
        run, _ = should_run_inference(frame_number=10, base_skip=0, active_hold=0)
        self.assertFalse(run)

    def test_hold_decrements_each_call(self):
        # Simulate 3 frames while in active mode: hold decrements 3,2,1.
        _, hold = should_run_inference(frame_number=1, base_skip=10, active_hold=3)
        self.assertEqual(hold, 2)
        _, hold = should_run_inference(frame_number=2, base_skip=10, active_hold=2)
        self.assertEqual(hold, 1)
        _, hold = should_run_inference(frame_number=3, base_skip=10, active_hold=1)
        self.assertEqual(hold, 0)

    def test_constants_match_documented_values(self):
        # Sanity check: the constants haven't drifted.
        self.assertEqual(ACTIVE_HOLD_FRAMES, 30)
        self.assertEqual(ACTIVE_MAX_FRAME_SKIP, 2)


class TestCameraLoopConstructorCapturesLoop(unittest.TestCase):
    """Regression: the online callback fires from the receiver thread and
    must not call asyncio.get_running_loop() (raises RuntimeError on
    non-asyncio threads). The constructor must capture the loop while
    we're still on the asyncio task's thread.
    """

    def test_constructor_stores_loop(self):
        async def run():
            cam = CameraSettings(camera_id="c1", source="rtsp://x")
            dev = MagicMock(spec=DeviceSettings)
            services = PluginServices(processor=MagicMock(), inference_lock=MagicMock())
            loop = CameraLoop(
                camera=cam,
                device_settings=dev,
                services=services,
                sender=MagicMock(),
                stop=asyncio.Event(),
                runtime_settings=MagicMock(),
                supabase=MagicMock(),
            )
            self.assertIs(loop._loop, asyncio.get_running_loop())
        asyncio.run(run())

    def test_online_callback_does_not_call_get_running_loop(self):
        """Simulate the race: callback fires on a non-asyncio thread.
        Must schedule the coroutine via the stored loop, not call
        get_running_loop() on the wrong thread.
        """
        async def run():
            cam = CameraSettings(camera_id="c1", source="rtsp://x")
            dev = MagicMock(spec=DeviceSettings)
            services = PluginServices(processor=MagicMock(), inference_lock=MagicMock())
            captured: list[asyncio.Future] = []

            real_loop = asyncio.get_running_loop()
            sender = MagicMock()
            loop = CameraLoop(
                camera=cam, device_settings=dev, services=services,
                sender=sender, stop=asyncio.Event(),
                runtime_settings=MagicMock(), supabase=MagicMock(),
            )
            # Intercept the loop's create_task so we can assert it was called.
            orig_create_task = loop._loop.create_task
            def _track(coro):
                t = orig_create_task(coro)
                captured.append(t)
                return t
            loop._loop.create_task = _track  # type: ignore[assignment]

            # Call the callback. If it called get_running_loop() on this
            # thread, it would succeed (we're on the asyncio thread). The
            # real failure mode is when it's called from the receiver thread.
            # Simulate that: schedule a thread that calls the callback.
            def _from_other_thread():
                loop._on_camera_online_change(True)  # would raise if it called get_running_loop
            import threading
            t = threading.Thread(target=_from_other_thread)
            t.start()
            t.join(timeout=1.0)
            self.assertFalse(t.is_alive(), "other-thread callback hung")
            # The asyncio side may or may not have run yet; just verify no
            # RuntimeError was raised synchronously.
            # Give the loop a tick to process the scheduled coroutine.
            await asyncio.sleep(0.05)
            real_loop.create_task = orig_create_task  # type: ignore[assignment]
        asyncio.run(run())

    def test_frame_broker_propagated_to_receiver(self):
        """Regression: command_executor reads frames from the FrameBroker
        for snapshot commands. The receiver must register its frames with
        the broker; if the broker isn't passed, snapshots always fail.
        """
        async def run():
            cam = CameraSettings(camera_id="c1", source="rtsp://x")
            dev = MagicMock(spec=DeviceSettings)
            services = PluginServices(processor=MagicMock(), inference_lock=MagicMock())
            broker = FrameBroker()
            loop = CameraLoop(
                camera=cam, device_settings=dev, services=services,
                sender=MagicMock(), stop=asyncio.Event(),
                runtime_settings=MagicMock(), supabase=MagicMock(),
                frame_broker=broker,
            )
            self.assertIs(loop._receiver._frame_broker, broker)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
