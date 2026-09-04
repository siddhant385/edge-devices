"""Tests for core/ai/processor.py.

The OnnxProcessor runs a real ONNX model, so we don't exercise the model
forward pass. These tests cover the parts that are testable in isolation:
- Session thread config (#12)
- Letterbox cache per resolution (#1)
- Buffer reuse on the preprocessing path (#8)
- has_motion buffer reuse
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np


def _fake_model_path() -> Path:
    """Create a temp file that exists (is_file() passes) but won't be parsed."""
    f = tempfile.NamedTemporaryFile(suffix=".onnx", delete=False)
    f.write(b"fake")
    f.close()
    return Path(f.name)


def _make_proc():
    with patch("onnxruntime.InferenceSession") as SessionCls:
        SessionCls.return_value.get_inputs.return_value = [MagicMock(name="images")]
        from core.ai.processor import OnnxProcessor
        return OnnxProcessor(model_path=_fake_model_path())


class TestSessionThreadConfig(unittest.TestCase):
    """#12: intra_op_num_threads should equal os.cpu_count()."""

    def test_intra_op_threads_set_to_cpu_count(self):
        with patch("onnxruntime.InferenceSession") as SessionCls:
            SessionCls.return_value.get_inputs.return_value = [MagicMock(name="images")]
            from core.ai.processor import OnnxProcessor
            OnnxProcessor(model_path=_fake_model_path())
            self.assertEqual(SessionCls.call_count, 1)
            opts = SessionCls.call_args.kwargs["sess_options"]
            import os
            self.assertEqual(opts.intra_op_num_threads, max(1, os.cpu_count() or 1))

    def test_inter_op_threads_set_to_one(self):
        with patch("onnxruntime.InferenceSession") as SessionCls:
            SessionCls.return_value.get_inputs.return_value = [MagicMock(name="images")]
            from core.ai.processor import OnnxProcessor
            OnnxProcessor(model_path=_fake_model_path())
            opts = SessionCls.call_args.kwargs["sess_options"]
            self.assertEqual(opts.inter_op_num_threads, 1)


class TestLetterboxCache(unittest.TestCase):
    """#1: the letterbox cache should memoize per (height, width)."""

    def test_same_resolution_reuses_canvas(self):
        proc = _make_proc()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        canvas1, scale1, pad_x1, pad_y1 = proc._letterbox(frame)
        canvas2, scale2, pad_x2, pad_y2 = proc._letterbox(frame)
        self.assertIs(canvas1, canvas2)
        self.assertEqual(scale1, scale2)
        self.assertEqual(pad_x1, pad_x2)
        self.assertEqual(pad_y1, pad_y2)

    def test_different_resolutions_get_different_caches(self):
        proc = _make_proc()
        f1 = np.zeros((480, 640, 3), dtype=np.uint8)
        f2 = np.zeros((720, 1280, 3), dtype=np.uint8)
        c1, _, _, _ = proc._letterbox(f1)
        c2, _, _, _ = proc._letterbox(f2)
        self.assertIsNot(c1, c2)
        self.assertEqual(len(proc._letterbox_cache), 2)

    def test_repeated_call_overwrites_gray_pad_correctly(self):
        proc = _make_proc()
        f1 = np.full((100, 100, 3), 0, dtype=np.uint8)
        c1, _, pad_x1, pad_y1 = proc._letterbox(f1)
        if pad_y1 > 0:
            c1[0:pad_y1, :, :] = 255
        f2 = np.zeros((100, 100, 3), dtype=np.uint8)
        c2, _, _, _ = proc._letterbox(f2)
        if pad_y1 > 0:
            self.assertTrue(np.all(c2[0:pad_y1, :, :] == 114))


class TestBufferReuse(unittest.TestCase):
    """#8: preprocessing buffers should be reused, not re-allocated."""

    def test_rgb_buffer_is_preallocated(self):
        proc = _make_proc()
        self.assertEqual(proc._rgb_buffer.shape, (640, 640, 3))
        self.assertEqual(proc._rgb_buffer.dtype, np.uint8)

    def test_float_buffer_is_preallocated(self):
        proc = _make_proc()
        self.assertEqual(proc._float_buffer.shape, (1, 3, 640, 640))
        self.assertEqual(proc._float_buffer.dtype, np.float32)

    def test_motion_buffer_is_preallocated(self):
        proc = _make_proc()
        self.assertEqual(proc._motion_buffer.shape, (120, 160))
        self.assertEqual(proc._motion_buffer.dtype, np.uint8)

    def test_letterbox_does_not_reallocate_on_cache_hit(self):
        proc = _make_proc()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        proc._letterbox(frame)  # first call: allocates
        with patch("numpy.full") as np_full:
            proc._letterbox(frame)  # second call: cache hit
            np_full.assert_not_called()


if __name__ == "__main__":
    unittest.main()

    def test_same_resolution_reuses_canvas(self):
        proc = self._make_proc()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        canvas1, scale1, pad_x1, pad_y1 = proc._letterbox(frame)
        canvas2, scale2, pad_x2, pad_y2 = proc._letterbox(frame)
        # Same canvas object (no re-allocation).
        self.assertIs(canvas1, canvas2)
        self.assertEqual(scale1, scale2)
        self.assertEqual(pad_x1, pad_x2)
        self.assertEqual(pad_y1, pad_y2)

    def test_different_resolutions_get_different_caches(self):
        proc = self._make_proc()
        f1 = np.zeros((480, 640, 3), dtype=np.uint8)
        f2 = np.zeros((720, 1280, 3), dtype=np.uint8)
        c1, _, _, _ = proc._letterbox(f1)
        c2, _, _, _ = proc._letterbox(f2)
        self.assertIsNot(c1, c2)
        # Cache should hold both.
        self.assertEqual(len(proc._letterbox_cache), 2)

    def test_repeated_call_overwrites_gray_pad_correctly(self):
        """If the previous frame was smaller than the next, the pad area
        must be re-painted gray. The bug would be leftover content from
        a previous call bleeding into the pad.
        """
        proc = self._make_proc()
        # First frame: small, lots of gray pad.
        f1 = np.full((100, 100, 3), 0, dtype=np.uint8)
        c1, _, pad_x1, pad_y1 = proc._letterbox(f1)
        # Scribble into the gray pad region of the cached canvas.
        if pad_y1 > 0:
            c1[0:pad_y1, :, :] = 255  # mark pad as white
        # Second frame: same size, but its actual content is solid black.
        f2 = np.zeros((100, 100, 3), dtype=np.uint8)
        c2, _, _, _ = proc._letterbox(f2)
        # Pad must be back to gray (114), not white.
        if pad_y1 > 0:
            self.assertTrue(np.all(c2[0:pad_y1, :, :] == 114))


class TestBufferReuse(unittest.TestCase):
    """#8: preprocessing buffers should be reused, not re-allocated."""

    def test_rgb_buffer_is_preallocated(self):
        proc = _make_proc()
        self.assertEqual(proc._rgb_buffer.shape, (640, 640, 3))
        self.assertEqual(proc._rgb_buffer.dtype, np.uint8)

    def test_float_buffer_is_preallocated(self):
        proc = _make_proc()
        self.assertEqual(proc._float_buffer.shape, (1, 3, 640, 640))
        self.assertEqual(proc._float_buffer.dtype, np.float32)

    def test_motion_state_was_removed(self):
        """The motion buffer and MOG2 subtractor were removed when the
        motion filter was dropped from the object_detection plugin.
        MOG2 had a stuck-background failure mode after camera reconnects
        that caused real motion to be missed.
        """
        proc = _make_proc()
        self.assertFalse(hasattr(proc, "_motion_buffer"))
        self.assertFalse(hasattr(proc, "_bg_subtractor"))

    def test_letterbox_does_not_reallocate_on_cache_hit(self):
        proc = _make_proc()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        proc._letterbox(frame)
        with patch("numpy.full") as np_full:
            proc._letterbox(frame)
            np_full.assert_not_called()


if __name__ == "__main__":
    unittest.main()
