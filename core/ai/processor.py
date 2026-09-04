"""CPU ONNX inference and Supervision detection processing."""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import supervision as sv
from supervision.detection.tools.inference_slicer import InferenceSlicer

COCO_CLASS_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis",
    "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog",
    "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)


class OnnxProcessor:
    """Run a standard YOLO detection ONNX model using CPUExecutionProvider only."""

    def __init__(
        self,
        model_path: Path,
        inference_size: int = 640,
        confidence_threshold: float = 0.3,
        nms_threshold: float = 0.5,
        target_class_ids: frozenset[int] | None = None,
    ) -> None:
        if not model_path.is_file():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")

        self._size = inference_size
        self._confidence_threshold = confidence_threshold
        self._nms_threshold = nms_threshold
        self._target_class_ids = target_class_ids or frozenset({0, 2, 7})  # Default: person, car, truck

        # ONNX CPU Optimization
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Use all available cores. Without this ORT defaults to 1 thread on
        # ARM, leaving cores idle. 30-50% YOLO speedup on a Pi 4.
        sess_options.intra_op_num_threads = max(1, os.cpu_count() or 1)
        sess_options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name

        # Pre-allocated buffers. Reused on every inference to keep the
        # preprocessing path allocation-free.
        self._letterbox_cache: dict[tuple[int, int], tuple[float, int, int, np.ndarray]] = {}
        self._rgb_buffer: np.ndarray = np.empty((self._size, self._size, 3), dtype=np.uint8)
        self._float_buffer: np.ndarray = np.empty((1, 3, self._size, self._size), dtype=np.float32)

    def has_motion(self, frame: np.ndarray) -> bool:
        """Cheap pre-filter: check if enough pixels have changed to bother running YOLO.

        Note: this is no longer called by the object_detection plugin (which
        removed its motion filter due to MOG2 failure modes on reconnect).
        Kept here in case future code wants to use it. If you re-enable the
        motion filter, reset the MOG2 state on camera reconnect.
        """
        cv2.resize(frame, (160, 120), dst=self._motion_buffer, interpolation=cv2.INTER_AREA)
        fg_mask = self._bg_subtractor.apply(self._motion_buffer)
        motion_pixels = cv2.countNonZero(fg_mask)
        return motion_pixels > (160 * 120 * 0.005)

    def detect(self, frame: np.ndarray) -> sv.Detections:
        """Run detection on the frame."""
        return self._detect_raw(frame, self._confidence_threshold, self._nms_threshold, self._target_class_ids)

    def _detect_raw(
        self,
        frame: np.ndarray,
        confidence_threshold: float,
        nms_threshold: float,
        target_class_ids: frozenset[int],
    ) -> sv.Detections:
        canvas, scale, pad_x, pad_y = self._letterbox(frame)
        # BGR -> RGB into the pre-allocated buffer, then divide into float32
        # in place. Zero allocations on the hot path.
        cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB, dst=self._rgb_buffer)
        # HWC uint8 RGB -> CHW float32 in [0, 1]. transpose creates a view
        # (no copy); divide into the pre-allocated float buffer.
        np.divide(
            self._rgb_buffer.transpose(2, 0, 1).astype(np.float32, copy=False),
            255.0,
            out=self._float_buffer[0],
        )

        output = self._session.run(None, {self._input_name: self._float_buffer})[0]
        detections = self._to_detections(output, frame.shape[1], frame.shape[0], scale, pad_x, pad_y)

        if len(detections) == 0:
            return detections

        # Filter by confidence and class
        detections = detections[detections.confidence >= confidence_threshold]
        if target_class_ids:
            detections = detections[np.isin(detections.class_id, list(target_class_ids))]

        return detections.with_nms(threshold=nms_threshold, class_agnostic=False)

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        """Resize + pad the frame into a square canvas. The canvas is
        memoized per (height, width) so repeat calls reuse the same buffer.
        """
        height, width = frame.shape[:2]
        key = (height, width)
        cached = self._letterbox_cache.get(key)
        if cached is not None:
            scale, pad_x, pad_y, canvas = cached
        else:
            scale = min(self._size / width, self._size / height)
            resized_width, resized_height = round(width * scale), round(height * scale)
            pad_x = (self._size - resized_width) // 2
            pad_y = (self._size - resized_height) // 2
            canvas = np.full((self._size, self._size, 3), 114, dtype=np.uint8)
            self._letterbox_cache[key] = (scale, pad_x, pad_y, canvas)
        # Re-paint: the canvas contents from the previous frame would still
        # be there if we skip this, so write gray + the resized region.
        canvas.fill(114)
        resized_width = round(width * scale)
        resized_height = round(height * scale)
        cv2.resize(
            frame,
            (resized_width, resized_height),
            dst=canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width],
            interpolation=cv2.INTER_LINEAR,
        )
        return canvas, scale, pad_x, pad_y

    def _to_detections(
        self, output: np.ndarray, width: int, height: int, scale: float, pad_x: int, pad_y: int
    ) -> sv.Detections:
        rows = np.squeeze(output, axis=0)

        if rows.ndim != 2:
            raise ValueError(f"Unsupported ONNX output shape: {output.shape}")

        # Handle YOLOv8/v11 transposed output
        if rows.shape[0] in (84, 85) or rows.shape[0] < rows.shape[1]:
            rows = rows.T

        if rows.shape[1] < 6:
            raise ValueError(f"Unsupported ONNX output shape: {output.shape}")

        boxes = rows[:, :4].astype(np.float32)
        boxes_are_xyxy = rows.shape[1] == 6

        if rows.shape[1] == 6:
            confidence = rows[:, 4].astype(np.float32)
            class_id = rows[:, 5].astype(int)
        else:
            class_scores = rows[:, 4:]
            class_id = class_scores.argmax(axis=1)
            confidence = class_scores[np.arange(len(rows)), class_id].astype(np.float32)

        if boxes_are_xyxy:
            xyxy = boxes
        else:
            # Convert cx, cy, w, h to x1, y1, x2, y2
            xyxy = np.column_stack((
                boxes[:, 0] - boxes[:, 2] / 2,
                boxes[:, 1] - boxes[:, 3] / 2,
                boxes[:, 0] + boxes[:, 2] / 2,
                boxes[:, 1] + boxes[:, 3] / 2,
            ))

        # Rescale coordinates to original image size
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / scale
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / scale
        xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, width)
        xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, height)

        class_names = np.array([COCO_CLASS_NAMES[index] if index < len(COCO_CLASS_NAMES) else str(index) for index in class_id])

        return sv.Detections(
            xyxy=xyxy,
            confidence=confidence,
            class_id=class_id,
            data={"class_name": class_names},
        )
