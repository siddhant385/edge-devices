"""Low-latency local-camera and RTSP frame ingestion."""

from __future__ import annotations

import logging
import time
import queue
import threading
from collections.abc import Iterator

import cv2
import numpy as np


class CameraReceiver:
    """Reconnect to a local camera or RTSP source and yield decoded frames asynchronously."""

    def __init__(self, camera_source: str, reconnect_delay_seconds: float) -> None:
        self._camera_source = camera_source
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._capture: cv2.VideoCapture | None = None
        
        self._frame_queue = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._on_online_change = None

    def set_online_callback(self, callback: callable) -> None:
        """Register a callback to be notified when camera connection state changes."""
        self._on_online_change = callback

    def _reader_loop(self) -> None:
        """Background thread that continuously reads frames to keep the RTSP buffer empty."""
        while not self._stop_event.is_set():
            if self._capture is None:
                if not self._open():
                    time.sleep(self._reconnect_delay_seconds)
                    continue

            assert self._capture is not None
            ok, frame = self._capture.read()
            if ok and frame is not None:
                # If queue is full, drop the oldest frame to maintain realtime
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                
                try:
                    self._frame_queue.put_nowait(frame)
                except queue.Full:
                    pass
            else:
                logging.warning("Camera frame read failed; reconnecting")
                self._close_capture()
                time.sleep(self._reconnect_delay_seconds)

    def frames(self) -> Iterator[np.ndarray]:
        """Yield the latest frame from the background queue."""
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        
        while not self._stop_event.is_set():
            try:
                # Get the most recent frame
                frame = self._frame_queue.get(timeout=1.0)
                yield frame
            except queue.Empty:
                continue

    def _close_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
            if self._on_online_change:
                self._on_online_change(False)

    def close(self) -> None:
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        self._close_capture()

    def _open(self) -> bool:
        self._close_capture()
        if self._camera_source.isdecimal():
            capture = cv2.VideoCapture(int(self._camera_source))
        else:
            import os
            # Use TCP for RTSP streams to prevent UDP packet loss which causes FFMPEG to stall
            # Reduced timeout from 5000000 (5 sec) to 2000000 (2 sec) to recover from dead streams faster
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|timeout;2000000"
            capture = cv2.VideoCapture(self._camera_source, cv2.CAP_FFMPEG)
            
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Fast decode, discard corrupted frames instead of waiting
        capture.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        # Optimize decoding for low latency on edge
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if not capture.isOpened():
            logging.warning("Unable to open camera source: %s", self._camera_source)
            capture.release()
            return False
            
        self._capture = capture
        logging.info("Connected to camera source: %s", self._camera_source)
        if self._on_online_change:
            self._on_online_change(True)
        return True
