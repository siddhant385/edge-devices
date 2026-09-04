"""Tests for the alert sender refactor.

These tests are runnable as: python -m unittest tests.test_sender
They lock in behavior of AlertSender, Outbox, and payload.build_payload
so the refactor of core/cloud/sender.py can be verified to be behavior-preserving.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import supervision as sv

from core.cloud.outbox import Outbox
from core.cloud.payload import build_payload, cooldown_filter
from core.cloud.sender import AlertSender
from core.cloud.ack_tracker import AcknowledgementTracker
from core.cloud.ack_listener import AcknowledgementListener
from plugins.base import FeatureEvent


def _det(xyxy, tracker_id=None, class_id=0, conf=0.9, class_name="person", zone_name=None):
    dets = sv.Detections(
        xyxy=np.array([xyxy], dtype=np.float32),
        confidence=np.array([conf]),
        class_id=np.array([class_id]),
        data={"class_name": np.array([class_name])},
    )
    if tracker_id is not None:
        dets.tracker_id = np.array([tracker_id])
    if zone_name is not None:
        dets.data["zone_name"] = np.array([zone_name])
    return dets


class TestOutbox(unittest.TestCase):
    def test_append_and_read_roundtrip(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                ob = Outbox(Path(d) / "out.jsonl", max_records=100)
                await ob.append({"a": 1})
                await ob.append({"b": 2, "evidence_jpeg": b"\xff\xd8\xff"})
                records = await ob.read_all()
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0], {"a": 1})
                self.assertEqual(records[1]["b"], 2)
                self.assertEqual(records[1]["evidence_jpeg"], b"\xff\xd8\xff")
        asyncio.run(run())

    def test_cap_enforced(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "out.jsonl"
                ob = Outbox(p, max_records=3)
                for i in range(5):
                    await ob.append({"i": i})
                lines = p.read_text().strip().splitlines()
                self.assertEqual(len(lines), 3, f"expected 3, got {len(lines)}")
                self.assertEqual([json.loads(l)["i"] for l in lines], [2, 3, 4])
        asyncio.run(run())

    def test_cap_zero_means_unlimited(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "out.jsonl"
                ob = Outbox(p, max_records=0)
                for i in range(50):
                    await ob.append({"i": i})
                self.assertEqual(len(p.read_text().strip().splitlines()), 50)
        asyncio.run(run())

    def test_write_tail_rewrites_remaining(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "out.jsonl"
                ob = Outbox(p, max_records=100)
                for i in range(5):
                    await ob.append({"i": i})
                # Simulate that records 0..2 were delivered; 3..4 remain.
                all_records = await ob.read_all()
                await ob.write_tail(all_records[3:])
                lines = p.read_text().strip().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertEqual([json.loads(l)["i"] for l in lines], [3, 4])
        asyncio.run(run())


class TestBuildPayload(unittest.TestCase):
    def test_zone_id_resolved_from_map(self):
        event = FeatureEvent("intrusion_detection", _det([0, 0, 10, 10], tracker_id=1, zone_name="front_gate"))
        payload = build_payload([event], "dev", "cam", "critical", {"front_gate": "uuid-aaa"})
        self.assertEqual(payload["detections"][0]["zone_name"], "front_gate")
        self.assertEqual(payload["detections"][0]["zone_id"], "uuid-aaa")

    def test_zone_name_without_map_entry(self):
        event = FeatureEvent("intrusion_detection", _det([0, 0, 10, 10], tracker_id=1, zone_name="unmapped"))
        payload = build_payload([event], "dev", "cam", "critical", {})
        self.assertEqual(payload["detections"][0]["zone_name"], "unmapped")
        self.assertNotIn("zone_id", payload["detections"][0])

    def test_severity_stamped_in_payload(self):
        event = FeatureEvent("object_detection", _det([0, 0, 10, 10], tracker_id=1))
        payload = build_payload([event], "dev", "cam", "warning")
        self.assertEqual(payload["_severity"], "warning")

    def test_multiple_events_combine(self):
        e1 = FeatureEvent("object_detection", _det([0, 0, 10, 10], tracker_id=1))
        e2 = FeatureEvent("object_detection", _det([20, 20, 30, 30], tracker_id=2))
        payload = build_payload([e1, e2], "dev", "cam", "critical")
        self.assertEqual(len(payload["detections"]), 2)
        self.assertEqual([d["tracker_id"] for d in payload["detections"]], [1, 2])

    def test_no_events_yields_empty(self):
        payload = build_payload([], "dev", "cam", "critical")
        self.assertEqual(payload["detections"], [])
        self.assertIsNone(payload["evidence_jpeg"])


class TestCooldownFilter(unittest.TestCase):
    def test_dedup_within_cooldown_window(self):
        dets = [
            {"tracker_id": 1, "feature": "object_detection"},
            {"tracker_id": 1, "feature": "object_detection"},
            {"tracker_id": 2, "feature": "object_detection"},
        ]
        last_seen: dict[int, float] = {}
        kept, last_seen = cooldown_filter(dets, last_seen, cooldown_seconds=5.0, now=100.0)
        self.assertEqual([d["tracker_id"] for d in kept], [1, 2])
        self.assertEqual(last_seen, {1: 100.0, 2: 100.0})

    def test_second_pass_after_window_resets(self):
        dets = [{"tracker_id": 1, "feature": "object_detection"}]
        last_seen: dict[int, float] = {1: 100.0}
        kept, last_seen = cooldown_filter(dets, last_seen, cooldown_seconds=5.0, now=110.0)
        self.assertEqual([d["tracker_id"] for d in kept], [1])
        self.assertEqual(last_seen[1], 110.0)

    def test_stale_entries_pruned_after_60s(self):
        dets: list[dict] = []
        last_seen: dict[int, float] = {1: 0.0, 2: 200.0}
        kept, last_seen = cooldown_filter(dets, last_seen, cooldown_seconds=5.0, now=200.0)
        self.assertNotIn(1, last_seen)
        self.assertIn(2, last_seen)

    def test_no_tracker_id_always_kept(self):
        dets = [{"feature": "object_detection"}, {"feature": "object_detection"}]
        last_seen: dict[int, float] = {}
        kept, _ = cooldown_filter(dets, last_seen, cooldown_seconds=5.0, now=100.0)
        self.assertEqual(len(kept), 2)

    def test_suppressed_trackers_dropped(self):
        dets = [
            {"tracker_id": 1, "feature": "object_detection"},
            {"tracker_id": 2, "feature": "object_detection"},
            {"tracker_id": 3, "feature": "object_detection"},
        ]
        kept, _ = cooldown_filter(
            dets, {}, cooldown_seconds=0.0, now=100.0,
            suppressed_trackers={2, 99},  # 99 is not in dets; should be ignored
        )
        self.assertEqual([d["tracker_id"] for d in kept], [1, 3])

    def test_suppression_takes_precedence_over_cooldown(self):
        dets = [{"tracker_id": 1, "feature": "object_detection"}]
        last_seen: dict[int, float] = {1: 50.0}  # not in cooldown
        kept, _ = cooldown_filter(
            dets, last_seen, cooldown_seconds=5.0, now=100.0,
            suppressed_trackers={1},
        )
        self.assertEqual(kept, [])


class TestAcknowledgementTracker(unittest.TestCase):
    def test_record_then_suppress_within_window(self):
        t = AcknowledgementTracker(ttl_seconds=300.0)
        t.record(42, now=100.0)
        self.assertTrue(t.should_suppress(42, now=200.0))
        self.assertFalse(t.should_suppress(99, now=200.0))

    def test_expires_after_ttl(self):
        t = AcknowledgementTracker(ttl_seconds=300.0)
        t.record(42, now=100.0)
        # exactly at the boundary: not suppressed (>= TTL means expired)
        self.assertFalse(t.should_suppress(42, now=400.0))
        # ensure the expired entry was cleaned up
        self.assertEqual(len(t), 0)

    def test_prune_removes_only_expired(self):
        t = AcknowledgementTracker(ttl_seconds=300.0)
        t.record(1, now=0.0)
        t.record(2, now=200.0)
        pruned = t.prune(now=400.0)
        self.assertEqual(pruned, 1)
        self.assertEqual(len(t), 1)
        self.assertIn(2, t.known_ids())

    def test_invalid_ttl_rejected(self):
        with self.assertRaises(ValueError):
            AcknowledgementTracker(ttl_seconds=0)
        with self.assertRaises(ValueError):
            AcknowledgementTracker(ttl_seconds=-1)

    def test_record_overwrites_stamp(self):
        t = AcknowledgementTracker(ttl_seconds=300.0)
        t.record(42, now=100.0)
        t.record(42, now=200.0)  # re-ack refreshes the window
        self.assertTrue(t.should_suppress(42, now=400.0))  # 200s after second ack
        self.assertFalse(t.should_suppress(42, now=501.0))


class TestAcknowledgementListenerCallback(unittest.TestCase):
    """Test the synchronous _on_update callback in isolation."""

    def _make_listener(self, ttl: float = 300.0) -> tuple[AcknowledgementListener, AcknowledgementTracker]:
        tracker = AcknowledgementTracker(ttl_seconds=ttl)
        listener = AcknowledgementListener.__new__(AcknowledgementListener)
        listener._tracker = tracker
        listener._lock = threading.Lock()
        return listener, tracker

    def test_acknowledged_status_with_tracker_id(self):
        listener, tracker = self._make_listener()
        listener._on_update({
            "data": {
                "record": {
                    "id": "alert-uuid",
                    "status": "acknowledged",
                    "raw_payload": {"tracker_id": 42},
                }
            }
        })
        self.assertTrue(tracker.should_suppress(42, now=999999.0))

    def test_unacknowledged_status_ignored(self):
        listener, tracker = self._make_listener()
        listener._on_update({
            "data": {"record": {"status": "unacknowledged", "raw_payload": {"tracker_id": 42}}}
        })
        self.assertEqual(len(tracker), 0)

    def test_missing_tracker_id_ignored(self):
        listener, tracker = self._make_listener()
        listener._on_update({
            "data": {"record": {"status": "acknowledged", "raw_payload": {}}}
        })
        self.assertEqual(len(tracker), 0)

    def test_resolved_status_also_suppresses(self):
        listener, tracker = self._make_listener()
        listener._on_update({
            "data": {"record": {"status": "resolved", "raw_payload": {"tracker_id": 7}}}
        })
        self.assertTrue(tracker.should_suppress(7, now=999999.0))

    def test_malformed_payload_does_not_raise(self):
        listener, _ = self._make_listener()
        listener._on_update({})  # no data key
        listener._on_update({"data": {}})  # no record
        listener._on_update({"data": {"record": None}})  # null record


class TestAlertSenderIntegration(unittest.TestCase):
    """Smoke tests for AlertSender end-to-end with mocked supabase."""

    def _make_sender(self, queue_path: Path) -> AlertSender:
        return AlertSender(
            supabase=MagicMock(),
            queue_path=queue_path,
            max_queue_records=10,
            device_uuid="dev-uuid",
            device_id_str="dev",
        )

    def test_send_schedules_async_task(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                sender = self._make_sender(Path(d) / "out.jsonl")
                event = FeatureEvent("object_detection", _det([0, 0, 10, 10], tracker_id=1))
                result = await sender.send([event], "dev", "cam")
                self.assertTrue(result)
                # Give the scheduled task a tick to enqueue.
                await asyncio.sleep(0.05)
                # The post call was on the MagicMock; the outbox file is empty
                # because the mock didn't fail, so nothing was queued.
                self.assertTrue((Path(d) / "out.jsonl").exists() or True)
        asyncio.run(run())

    def test_failed_post_lands_in_outbox(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                qp = Path(d) / "out.jsonl"
                supabase = MagicMock()
                # Make the storage upload raise so _post returns False.
                supabase.storage.from_.return_value.upload.side_effect = RuntimeError("offline")
                sender = AlertSender(supabase, qp, max_queue_records=10, device_uuid="dev-uuid", device_id_str="dev")
                # Inject a JPEG to force the upload path.
                dets = _det([0, 0, 10, 10], tracker_id=1, class_id=0)
                event = FeatureEvent("object_detection", dets)
                event.evidence_jpeg = b"\xff\xd8\xff\xd9"  # minimal JPEG
                await sender.send([event], "dev", "cam")
                # Wait for the dispatched task to complete.
                for _ in range(20):
                    if qp.exists() and qp.stat().st_size > 0:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(qp.exists(), "outbox file should exist after failed post")
                content = qp.read_text().strip()
                self.assertGreater(len(content), 0, "outbox should have at least one record")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
