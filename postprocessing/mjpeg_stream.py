#!/usr/bin/env python3
"""Shared MJPEG stream reader utilities for the postprocessing scripts."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import requests


# ---------------------------------------------------------------------------
# Shared frame-rendering helpers (imported by viewer scripts)
# ---------------------------------------------------------------------------

def resize_and_pad(frame: np.ndarray, target_height: int) -> np.ndarray:
    height, width = frame.shape[:2]
    if height <= 0 or width <= 0:
        return frame
    scale = target_height / float(height)
    new_width = max(1, int(width * scale))
    return cv2.resize(frame, (new_width, target_height), interpolation=cv2.INTER_LINEAR)


def make_placeholder(label: str, size: tuple = (480, 640)) -> np.ndarray:
    height, width = size
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:] = (30, 30, 30)
    cv2.putText(canvas, label, (30, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (230, 230, 230), 2)
    cv2.putText(canvas, "Waiting for stream...", (30, height // 2 + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 180), 2)
    return canvas


def add_header(frame: np.ndarray, title: str, connected: bool, error: "Optional[str]") -> np.ndarray:
    overlay = frame.copy()
    color = (60, 180, 90) if connected else (40, 40, 220)
    cv2.rectangle(overlay, (0, 0), (frame.shape[1], 52), (18, 18, 18), -1)
    frame = cv2.addWeighted(overlay, 0.88, frame, 0.12, 0)
    status = "connected" if connected else "offline"
    cv2.putText(frame, title, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (240, 240, 240), 2)
    cv2.putText(frame, status, (frame.shape[1] - 140, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    if error:
        cv2.putText(frame, error[:60], (16, frame.shape[0] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (120, 180, 255), 1)
    return frame


def build_side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    target_height = max(left.shape[0], right.shape[0])
    left_resized = resize_and_pad(left, target_height)
    right_resized = resize_and_pad(right, target_height)

    if left_resized.shape[0] != right_resized.shape[0]:
        target_height = min(left_resized.shape[0], right_resized.shape[0])
        left_resized = resize_and_pad(left_resized, target_height)
        right_resized = resize_and_pad(right_resized, target_height)

    separator = np.zeros((target_height, 12, 3), dtype=np.uint8)
    separator[:] = (45, 45, 45)
    return np.hstack([left_resized, separator, right_resized])


# ---------------------------------------------------------------------------

@dataclass
class StreamState:
    frame: Optional[np.ndarray] = None
    connected: bool = False
    error: Optional[str] = None


class MjpegStreamReader:
    def __init__(self, source: str, name: str, loop: bool = False):
        self.source = source
        self.name = name
        self.loop = loop
        self.state = StreamState()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _is_network_source(self) -> bool:
        return self.source.startswith("http://") or self.source.startswith("https://")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.state.error = None
                self.state.connected = False
                if self._is_network_source():
                    response = requests.get(
                        self.source,
                        stream=True,
                        timeout=(3, None),
                        headers={"Cache-Control": "no-cache"},
                    )
                    response.raise_for_status()
                    self.state.connected = True

                    buffer = b""
                    for chunk in response.iter_content(chunk_size=4096):
                        if self._stop_event.is_set():
                            break
                        if not chunk:
                            continue

                        buffer += chunk
                        
                        # Look for complete JPEG frames using SOI and EOI markers
                        while True:
                            start = buffer.find(b"\xff\xd8")
                            if start == -1:
                                break
                            
                            end = buffer.find(b"\xff\xd9", start)
                            if end == -1:
                                buffer = buffer[start:]  # discard garbage before SOI so the buffer doesn't grow unboundedly
                                break
                            
                            # Extract the complete JPEG
                            jpg = buffer[start : end + 2]
                            buffer = buffer[end + 2 :]
                            
                            # Try to decode the frame
                            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                            if frame is not None:
                                self.state.frame = frame
                            else:
                                # If decode fails, log but continue
                                pass

                    response.close()
                else:
                    if not os.path.exists(self.source):
                        raise FileNotFoundError(self.source)

                    capture = cv2.VideoCapture(self.source)
                    if not capture.isOpened():
                        raise RuntimeError(f"Unable to open video source: {self.source}")
                    self.state.connected = True

                    while not self._stop_event.is_set():
                        ok, frame = capture.read()
                        if not ok:
                            if self.loop:
                                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                continue
                            self.state.error = "end of file"
                            self.state.connected = False
                            break
                        self.state.frame = frame
                        time.sleep(0.001)

                    capture.release()
            except Exception as exc:
                self.state.error = str(exc)
                self.state.connected = False
                time.sleep(1.0)