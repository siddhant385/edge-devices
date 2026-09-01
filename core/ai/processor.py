"""CPU ONNX inference and Supervision detection processing."""

from __future__ import annotations

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
        self._target_class_ids = target_class_ids or frozenset({0, 2, 7}) # Default: person, car, truck

        # ONNX CPU Optimization
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(model_path), 
            sess_options=sess_options,
            providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        
        # Motion detection back-subtractor to save CPU
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(history=50, varThreshold=25, detectShadows=False)

    def has_motion(self, frame: np.ndarray) -> bool:
        """Cheap pre-filter: check if enough pixels have changed to bother running YOLO."""
        small = cv2.resize(frame, (160, 120))  # Aggressive downscaling
        fg_mask = self._bg_subtractor.apply(small)
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
        image, scale, pad_x, pad_y = self._letterbox(frame)
        input_tensor = image[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        
        output = self._session.run(None, {self._input_name: input_tensor})[0]
        detections = self._to_detections(output, frame.shape[1], frame.shape[0], scale, pad_x, pad_y)
        
        if len(detections) == 0:
            return detections
            
        # Filter by confidence and class
        detections = detections[detections.confidence >= confidence_threshold]
        if target_class_ids:
            detections = detections[np.isin(detections.class_id, list(target_class_ids))]
            
        return detections.with_nms(threshold=nms_threshold, class_agnostic=False)

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        height, width = frame.shape[:2]
        scale = min(self._size / width, self._size / height)
        resized_width, resized_height = round(width * scale), round(height * scale)
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self._size, self._size, 3), 114, dtype=np.uint8)
        pad_x = (self._size - resized_width) // 2
        pad_y = (self._size - resized_height) // 2
        canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width] = resized
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
