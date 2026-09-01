"""Thread-safe in-memory frame storage for bridging components."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

class FrameBroker:
    """Stores the most recent frame per camera for immediate access without network I/O."""
    
    def __init__(self) -> None:
        self._frames: dict[str, Any] = {}
        self._lock = Lock()
        
    def set_latest_frame(self, camera_id: str, frame: Any) -> None:
        """Update the latest frame for a camera. Thread-safe."""
        with self._lock:
            # We store a reference. OpenCV numpy arrays are mutable, but we only 
            # read them briefly during compression. In high-contention environments,
            # you might want to frame.copy() here, but that impacts CPU.
            self._frames[camera_id] = frame
            
    def get_latest_frame(self, camera_id: str) -> Any | None:
        """Retrieve the latest frame for a camera. Thread-safe."""
        with self._lock:
            return self._frames.get(camera_id)
