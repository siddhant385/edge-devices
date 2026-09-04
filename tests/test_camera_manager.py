"""Tests for the field-ownership rule in sync_with_cloud.

Regression: the camera's source URL (and other physical fields) was being
overwritten by the cloud's stale value on every boot. The fix is a
structural rule: when syncing settings, only TUNING_FIELDS flow from cloud
to edge. PHYSICAL_FIELDS always come from the local JSON.
"""

from __future__ import annotations

import unittest

from config.camera_settings import (
    PHYSICAL_FIELDS,
    TUNING_FIELDS,
    CameraSettings,
)


def _merge(local: CameraSettings, cloud_data: dict) -> CameraSettings:
    """Replicates the merge in camera_manager.sync_with_cloud.

    The actual code inlines this in sync_with_cloud; we duplicate the
    rule here so we can test it without a Supabase client.
    """
    local_dict = local.to_dict()
    cloud_tuning = {k: v for k, v in cloud_data.items() if k in TUNING_FIELDS}
    merged = {**local_dict, **cloud_tuning, "id": local.camera_id}
    return CameraSettings.from_dict(merged)


class TestFieldOwnership(unittest.TestCase):
    """The structural rule that physical fields never flow from cloud to edge."""

    def test_physical_and_tuning_are_disjoint(self):
        overlap = PHYSICAL_FIELDS & TUNING_FIELDS
        self.assertEqual(
            overlap, set(),
            f"physical and tuning fields must not overlap: {overlap}",
        )

    def test_source_in_physical(self):
        self.assertIn("source", PHYSICAL_FIELDS)

    def test_lat_lon_location_in_physical(self):
        for f in ("latitude", "longitude", "location"):
            self.assertIn(f, PHYSICAL_FIELDS, f"{f} must be physical")

    def test_confidence_in_tuning(self):
        self.assertIn("confidence_threshold", TUNING_FIELDS)
        self.assertIn("zones", TUNING_FIELDS)
        self.assertIn("cooldown_seconds", TUNING_FIELDS)


class TestMergeKeepsLocalPhysical(unittest.TestCase):
    """Cloud-stale physical fields must not overwrite the local value."""

    def test_cloud_source_does_not_overwrite_local(self):
        local = CameraSettings(
            camera_id="c1", source="rtsp://192.168.31.175:8554/mystream",
        )
        cloud = {
            "id": "c1",
            "source": "rtsp://:8554/mystream",  # stale/bad cloud value
        }
        merged = _merge(local, cloud)
        self.assertEqual(merged.source, "rtsp://192.168.31.175:8554/mystream")

    def test_cloud_lat_lon_does_not_overwrite_local(self):
        local = CameraSettings(
            camera_id="c1", source="rtsp://x",
            latitude=30.85, longitude=72.345, location="Post A",
        )
        cloud = {
            "latitude": 0.0,  # bad: 0,0 is in the ocean
            "longitude": 0.0,
            "location": "stale wrong value",
        }
        merged = _merge(local, cloud)
        self.assertEqual(merged.latitude, 30.85)
        self.assertEqual(merged.longitude, 72.345)
        self.assertEqual(merged.location, "Post A")

    def test_cloud_tuning_does_overwrite_local(self):
        """The opposite side of the rule: tuning fields from the cloud win."""
        local = CameraSettings(
            camera_id="c1", source="rtsp://x",
            confidence_threshold=0.45, cooldown_seconds=5.0,
        )
        cloud = {
            "confidence_threshold": 0.65,  # operator wants stricter
            "cooldown_seconds": 10.0,
        }
        merged = _merge(local, cloud)
        self.assertEqual(merged.confidence_threshold, 0.65)
        self.assertEqual(merged.cooldown_seconds, 10.0)

    def test_merged_keeps_local_id_even_when_cloud_has_one(self):
        local = CameraSettings(camera_id="uuid-from-edge", source="rtsp://x")
        cloud = {"id": "uuid-from-cloud"}
        merged = _merge(local, cloud)
        # The camera_id is the edge's authoritative identity, not the cloud's
        # representation of it. The local value wins.
        self.assertEqual(merged.camera_id, "uuid-from-edge")

    def test_empty_cloud_data_returns_local_unchanged(self):
        local = CameraSettings(
            camera_id="c1", source="rtsp://x",
            confidence_threshold=0.5, latitude=10.0,
        )
        merged = _merge(local, {})
        self.assertEqual(merged.source, "rtsp://x")
        self.assertEqual(merged.confidence_threshold, 0.5)
        self.assertEqual(merged.latitude, 10.0)


if __name__ == "__main__":
    unittest.main()
