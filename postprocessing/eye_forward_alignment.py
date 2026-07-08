#!/usr/bin/env python3
"""
Eye/forward postprocessing viewer.

This script does the first two tasks of the pipeline:
1. Estimate an eye angle from the eye-facing camera stream.
2. Remap that angle to the forward camera view, taking the glasses-mounted
   camera offset into account.

It only uses the video feeds and simple OpenCV-based pupil localization.
No YOLO or object recognition is included here.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from dotenv import load_dotenv

from mjpeg_stream import MjpegStreamReader, resize_and_pad, make_placeholder, add_header, build_side_by_side

load_dotenv()

_CLAHE_PUPIL = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

# Detections below this score are likely noise or reflections and are dropped
# rather than fed to the smoother (which would pull the gaze circle to a bad position).
_PUPIL_MIN_SCORE = 0.20


@dataclass
class EyeAngles:
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    confidence: float = 0.0


@dataclass
class PupilDetection:
    pupil: Optional[Tuple[int, int, int]]
    threshold: np.ndarray
    contour_count: int = 0
    best_score: float = 0.0


@dataclass
class CalibrationModel:
    affine_x: Tuple[float, float, float] = (1.0, 0.0, 0.0)
    affine_y: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    neutral_center_x_norm: float = 0.5
    neutral_center_y_norm: float = 0.5
    board_size_cm: float = 15.0
    board_distance_cm: float = 30.0
    point_hold_seconds: float = 2.0
    sample_count: int = 0
    point_count: int = 0
    source: str = "default"
    profile: str = "child"
    poly_x: Optional[Tuple[float, float, float, float, float, float]] = None
    poly_y: Optional[Tuple[float, float, float, float, float, float]] = None


@dataclass(frozen=True)
class CalibrationTarget:
    x_cm: float
    y_cm: float
    label: str
    display_x_norm: float
    display_y_norm: float


CALIBRATION_BOARD_SIZE_CM = 15.0
CALIBRATION_BOARD_DISTANCE_CM = 30.0
CALIBRATION_POINT_HOLD_SECONDS = 2.0


def board_point_to_angles_deg(x_cm: float, y_cm: float, distance_cm: float) -> Tuple[float, float]:
    yaw_deg = math.degrees(math.atan2(x_cm, distance_cm))
    pitch_deg = math.degrees(math.atan2(y_cm, distance_cm))
    return yaw_deg, pitch_deg


def build_board_targets(board_size_cm: float = CALIBRATION_BOARD_SIZE_CM) -> list[CalibrationTarget]:
    half = board_size_cm * 0.5
    return [
        CalibrationTarget(0.0, 0.0, "center", 0.50, 0.50),
        CalibrationTarget(-half, half, "top-left", 0.00, 0.00),
        CalibrationTarget(0.0, half, "top-center", 0.50, 0.00),
        CalibrationTarget(half, half, "top-right", 1.00, 0.00),
        CalibrationTarget(-half, 0.0, "left-center", 0.00, 0.50),
        CalibrationTarget(half, 0.0, "right-center", 1.00, 0.50),
        CalibrationTarget(-half, -half, "bottom-left", 0.00, 1.00),
        CalibrationTarget(0.0, -half, "bottom-center", 0.50, 1.00),
        CalibrationTarget(half, -half, "bottom-right", 1.00, 1.00),
    ]


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    eye_max_yaw_deg: float
    eye_max_pitch_deg: float
    forward_hfov_deg: float
    forward_vfov_deg: float
    camera_offset_x_mm: float
    camera_offset_y_mm: float
    camera_offset_z_mm: float
    assumed_depth_mm: float
    smoothing_alpha: float
    calibration_hold_seconds: float


DEVICE_PROFILES = {
    "child": DeviceProfile(
        name="child",
        eye_max_yaw_deg=22.0,
        eye_max_pitch_deg=16.0,
        forward_hfov_deg=70.0,
        forward_vfov_deg=50.0,
        camera_offset_x_mm=65.0,
        camera_offset_y_mm=0.0,
        camera_offset_z_mm=25.0,
        assumed_depth_mm=170.0,
        smoothing_alpha=0.15,
        calibration_hold_seconds=2.0,
    ),
    "adult": DeviceProfile(
        name="adult",
        eye_max_yaw_deg=25.0,
        eye_max_pitch_deg=18.0,
        forward_hfov_deg=70.0,
        forward_vfov_deg=50.0,
        camera_offset_x_mm=65.0,
        camera_offset_y_mm=0.0,
        camera_offset_z_mm=25.0,
        assumed_depth_mm=200.0,
        smoothing_alpha=0.15,
        calibration_hold_seconds=2.0,
    ),
}


def get_device_profile(profile_name: str) -> DeviceProfile:
    return DEVICE_PROFILES.get(profile_name, DEVICE_PROFILES["child"])


class ScalarSmoother:
    def __init__(self, alpha: float):
        self.alpha = float(np.clip(alpha, 0.0, 1.0))
        self._value: Optional[float] = None

    @property
    def value(self) -> Optional[float]:
        return self._value

    def update(self, value: float) -> float:
        if self._value is None:
            self._value = value
            return value
        self._value = self.alpha * value + (1.0 - self.alpha) * self._value
        return self._value


class CalibrationSession:
    def __init__(self, point_hold_seconds: float, points: Optional[list[CalibrationTarget]] = None, board_distance_cm: float = CALIBRATION_BOARD_DISTANCE_CM):
        self.point_hold_seconds = max(0.25, float(point_hold_seconds))
        self.board_distance_cm = float(board_distance_cm)
        self.points = points or build_board_targets()
        self.active = False
        self.current_index = 0
        self.point_started_at: float = 0.0
        self.samples: list[Tuple[float, float, float, float]] = []

    def start(self) -> None:
        self.active = True
        self.current_index = 0
        self.point_started_at = time.monotonic()
        self.samples.clear()

    def stop(self) -> None:
        self.active = False

    def current_target(self) -> Optional[CalibrationTarget]:
        if not self.active or self.current_index >= len(self.points):
            return None
        return self.points[self.current_index]

    def add_sample(self, pupil: Optional[Tuple[int, int, int]], frame_shape: Tuple[int, int, int]) -> Optional[CalibrationModel]:
        if not self.active or pupil is None:
            return None

        height, width = frame_shape[:2]
        cx, cy, _ = pupil
        target = self.points[self.current_index]
        target_yaw_deg, target_pitch_deg = board_point_to_angles_deg(target.x_cm, target.y_cm, self.board_distance_cm)
        self.samples.append((cx / max(1.0, width), cy / max(1.0, height), target_yaw_deg, target_pitch_deg))

        if (time.monotonic() - self.point_started_at) < self.point_hold_seconds:
            return None

        self.current_index += 1
        if self.current_index < len(self.points):
            self.point_started_at = time.monotonic()
            return None

        self.stop()
        return fit_calibration_model(self.samples)


def fit_calibration_model(samples: list[Tuple[float, float, float, float]]) -> CalibrationModel:
    if not samples:
        return CalibrationModel()

    source_matrix = np.array([[sample[0], sample[1], 1.0] for sample in samples], dtype=np.float64)
    target_yaw = np.array([sample[2] for sample in samples], dtype=np.float64)
    target_pitch = np.array([sample[3] for sample in samples], dtype=np.float64)

    affine_x, _, _, _ = np.linalg.lstsq(source_matrix, target_yaw, rcond=None)
    affine_y, _, _, _ = np.linalg.lstsq(source_matrix, target_pitch, rcond=None)

    # Fit 2nd-order polynomial: [1, px, py, px^2, py^2, px*py]
    poly_matrix = np.array([[1.0, sample[0], sample[1], sample[0] * sample[0], sample[1] * sample[1], sample[0] * sample[1]] for sample in samples], dtype=np.float64)
    poly_x_coeffs, _, _, _ = np.linalg.lstsq(poly_matrix, target_yaw, rcond=None)
    poly_y_coeffs, _, _, _ = np.linalg.lstsq(poly_matrix, target_pitch, rcond=None)

    center_samples = [sample for sample in samples if abs(sample[2]) < 1e-6 and abs(sample[3]) < 1e-6]
    if center_samples:
        neutral_x = float(np.mean([sample[0] for sample in center_samples]))
        neutral_y = float(np.mean([sample[1] for sample in center_samples]))
    else:
        neutral_x = 0.5
        neutral_y = 0.5

    return CalibrationModel(
        affine_x=(float(affine_x[0]), float(affine_x[1]), float(affine_x[2])),
        affine_y=(float(affine_y[0]), float(affine_y[1]), float(affine_y[2])),
        neutral_center_x_norm=neutral_x,
        neutral_center_y_norm=neutral_y,
        board_size_cm=CALIBRATION_BOARD_SIZE_CM,
        board_distance_cm=CALIBRATION_BOARD_DISTANCE_CM,
        point_hold_seconds=CALIBRATION_POINT_HOLD_SECONDS,
        sample_count=len(samples),
        point_count=len({(sample[2], sample[3]) for sample in samples}),
        source="multi-point-calibration",
        poly_x=(float(poly_x_coeffs[0]), float(poly_x_coeffs[1]), float(poly_x_coeffs[2]), float(poly_x_coeffs[3]), float(poly_x_coeffs[4]), float(poly_x_coeffs[5])),
        poly_y=(float(poly_y_coeffs[0]), float(poly_y_coeffs[1]), float(poly_y_coeffs[2]), float(poly_y_coeffs[3]), float(poly_y_coeffs[4]), float(poly_y_coeffs[5])),
    )


# resize_and_pad, make_placeholder, add_header, and build_side_by_side are
# imported from mjpeg_stream (shared helpers).


def draw_marker(frame: np.ndarray, point: Tuple[int, int], color: Tuple[int, int, int], label: str) -> None:
    x, y = point
    cv2.circle(frame, (x, y), 8, color, 2)
    cv2.line(frame, (x - 14, y), (x + 14, y), color, 1)
    cv2.line(frame, (x, y - 14), (x, y + 14), color, 1)
    cv2.putText(frame, label, (x + 12, y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)


def clamp_point(x: float, y: float, width: int, height: int) -> Tuple[int, int]:
    return int(np.clip(x, 0, width - 1)), int(np.clip(y, 0, height - 1))


def load_calibration(calibration_path: Path) -> CalibrationModel:
    if not calibration_path.exists():
        return CalibrationModel()

    try:
        data = json.loads(calibration_path.read_text(encoding="utf-8"))
        if "affine_x" in data and "affine_y" in data:
            poly_x = data.get("poly_x")
            poly_y = data.get("poly_y")
            return CalibrationModel(
                affine_x=tuple(float(value) for value in data.get("affine_x", (1.0, 0.0, 0.0))),
                affine_y=tuple(float(value) for value in data.get("affine_y", (0.0, 1.0, 0.0))),
                neutral_center_x_norm=float(data.get("neutral_center_x_norm", 0.5)),
                neutral_center_y_norm=float(data.get("neutral_center_y_norm", 0.5)),
                board_size_cm=float(data.get("board_size_cm", CALIBRATION_BOARD_SIZE_CM)),
                board_distance_cm=float(data.get("board_distance_cm", CALIBRATION_BOARD_DISTANCE_CM)),
                point_hold_seconds=float(data.get("point_hold_seconds", CALIBRATION_POINT_HOLD_SECONDS)),
                sample_count=int(data.get("sample_count", 0)),
                point_count=int(data.get("point_count", 0)),
                source=str(data.get("source", "file")),
                profile=str(data.get("profile", "child")),
                poly_x=tuple(float(v) for v in poly_x) if poly_x is not None else None,
                poly_y=tuple(float(v) for v in poly_y) if poly_y is not None else None,
            )

        return CalibrationModel(
            neutral_center_x_norm=float(data.get("neutral_center_x_norm", 0.5)),
            neutral_center_y_norm=float(data.get("neutral_center_y_norm", 0.5)),
            board_size_cm=float(data.get("board_size_cm", CALIBRATION_BOARD_SIZE_CM)),
            board_distance_cm=float(data.get("board_distance_cm", CALIBRATION_BOARD_DISTANCE_CM)),
            point_hold_seconds=float(data.get("point_hold_seconds", CALIBRATION_POINT_HOLD_SECONDS)),
            sample_count=int(data.get("sample_count", 0)),
            source=str(data.get("source", "file")),
            profile=str(data.get("profile", "child")),
        )
    except Exception:
        return CalibrationModel(source="invalid-file")


def save_calibration(calibration_path: Path, calibration: CalibrationModel) -> None:
    payload = {
        "affine_x": list(calibration.affine_x),
        "affine_y": list(calibration.affine_y),
        "neutral_center_x_norm": calibration.neutral_center_x_norm,
        "neutral_center_y_norm": calibration.neutral_center_y_norm,
        "board_size_cm": calibration.board_size_cm,
        "board_distance_cm": calibration.board_distance_cm,
        "point_hold_seconds": calibration.point_hold_seconds,
        "sample_count": calibration.sample_count,
        "point_count": calibration.point_count,
        "profile": calibration.profile,
        "poly_x": list(calibration.poly_x) if calibration.poly_x is not None else None,
        "poly_y": list(calibration.poly_y) if calibration.poly_y is not None else None,
        "source": calibration.source,
        "saved_at": time.time(),
    }
    calibration_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def detect_pupil(frame: np.ndarray) -> PupilDetection:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]

    # Restrict search to the central 80% to exclude noisy frame borders and eyelashes.
    mx, my = int(w * 0.10), int(h * 0.10)
    roi = gray[my : h - my, mx : w - mx]
    roi_h, roi_w = roi.shape[:2]

    # Kernel sizes scale with frame resolution (blur kernel must be odd).
    k       = max(3, (min(roi_h, roi_w) // 40) | 1)
    open_k  = max(2, min(roi_h, roi_w) // 80)
    close_k = max(3, min(roi_h, roi_w) // 50)

    equalized = _CLAHE_PUPIL.apply(roi)
    blurred   = cv2.GaussianBlur(equalized, (k, k), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    thresh    = cv2.morphologyEx(thresh, cv2.MORPH_OPEN,  np.ones((open_k,  open_k),  np.uint8))
    thresh    = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, np.ones((close_k, close_k), np.uint8))

    contours_info = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = contours_info[0] if len(contours_info) == 2 else contours_info[1]
    if not contours:
        return PupilDetection(None, thresh, 0, 0.0)

    center_x, center_y = roi_w * 0.5, roi_h * 0.5
    max_radius = min(roi_w, roi_h) * 0.35
    min_area   = max(12.0, roi_w * roi_h * 0.0005)
    # Normalize area against a circle whose radius is 45% of the max permitted radius.
    area_denom = math.pi * (max_radius * 0.45) ** 2

    best_score = -1.0
    best_candidate: Optional[Tuple[int, int, int]] = None

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue

        if len(contour) >= 5:
            try:
                (ex, ey), (MA, ma), _ = cv2.fitEllipse(contour)
                minor, major = min(MA, ma), max(MA, ma)
                if minor <= 0:
                    continue
                circularity = minor / major
                radius = minor * 0.5
            except Exception:
                continue
        else:
            (ex, ey), radius = cv2.minEnclosingCircle(contour)
            circularity = 0.5

        if radius < 2.0 or radius > max_radius:
            continue

        # Gate on actual darkness: the blob interior must be dark in the original
        # (pre-CLAHE) ROI to rule out bright iris regions and specular reflections.
        mask = np.zeros((roi_h, roi_w), dtype=np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, cv2.FILLED)
        mean_val = cv2.mean(roi, mask=mask)[0]
        if mean_val > 85:
            continue

        dist_norm = math.hypot(
            (ex - center_x) / max(1.0, center_x),
            (ey - center_y) / max(1.0, center_y),
        )
        area_score     = min(area / max(1.0, area_denom), 1.0)
        darkness_score = max(0.0, 1.0 - mean_val / 85.0)
        score = area_score + 0.7 * circularity + 0.4 * darkness_score - dist_norm

        if score > best_score:
            best_score = score
            # Translate ROI-relative coords back to full-frame coords.
            best_candidate = (int(ex) + mx, int(ey) + my, int(max(1, radius)))

    return PupilDetection(best_candidate, thresh, len(contours), best_score)


def estimate_eye_angles(
    frame: np.ndarray,
    pupil: Optional[Tuple[int, int, int]],
    neutral_center: CalibrationModel,
    eye_max_yaw_deg: float,
    eye_max_pitch_deg: float,
) -> EyeAngles:
    height, width = frame.shape[:2]
    if pupil is None:
        return EyeAngles(0.0, 0.0, 0.0)

    cx, cy, radius = pupil
    neutral_x = neutral_center.neutral_center_x_norm * width
    neutral_y = neutral_center.neutral_center_y_norm * height
    norm_x = (cx - neutral_x) / max(1.0, width / 2.0)
    norm_y = (cy - neutral_y) / max(1.0, height / 2.0)

    yaw = float(np.clip(norm_x * eye_max_yaw_deg, -eye_max_yaw_deg, eye_max_yaw_deg))
    pitch = float(np.clip(-norm_y * eye_max_pitch_deg, -eye_max_pitch_deg, eye_max_pitch_deg))
    confidence = float(np.clip(1.0 - math.hypot(norm_x, norm_y), 0.0, 1.0))
    return EyeAngles(yaw_deg=yaw, pitch_deg=pitch, confidence=confidence)


def eye_angles_to_forward_angles(
    eye_angles: EyeAngles,
    camera_offset_x_mm: float,
    camera_offset_y_mm: float,
    camera_offset_z_mm: float,
    assumed_depth_mm: float,
) -> EyeAngles:
    """
    Convert the eye-gaze estimate into a forward-camera equivalent angle.

    Coordinate system:
    - x points right
    - y points up
    - z points forward
    """
    yaw_rad = math.radians(eye_angles.yaw_deg)
    pitch_rad = math.radians(eye_angles.pitch_deg)

    # Ray starting at the eye and intersecting a plane at the assumed fixation depth.
    target_x = math.tan(yaw_rad) * assumed_depth_mm
    target_y = math.tan(pitch_rad) * assumed_depth_mm
    target_z = assumed_depth_mm

    dx = target_x - camera_offset_x_mm
    dy = target_y - camera_offset_y_mm
    dz = target_z - camera_offset_z_mm

    forward_yaw = math.degrees(math.atan2(dx, dz))
    forward_pitch = math.degrees(math.atan2(dy, math.sqrt(dx * dx + dz * dz)))
    confidence = eye_angles.confidence
    return EyeAngles(yaw_deg=forward_yaw, pitch_deg=forward_pitch, confidence=confidence)


def angle_to_screen_point(
    angles: EyeAngles,
    frame_shape: Tuple[int, int, int],
    horizontal_fov_deg: float,
    vertical_fov_deg: float,
) -> Tuple[int, int]:
    height, width = frame_shape[:2]
    half_w = width * 0.5
    half_h = height * 0.5

    x = half_w + (angles.yaw_deg / max(1e-6, horizontal_fov_deg * 0.5)) * half_w
    y = half_h - (angles.pitch_deg / max(1e-6, vertical_fov_deg * 0.5)) * half_h
    return clamp_point(x, y, width, height)


def predict_forward_point(
    calibration: CalibrationModel,
    pupil: Optional[Tuple[int, int, int]],
    eye_angles: EyeAngles,
    frame_shape: Tuple[int, int, int],
    forward_hfov_deg: float,
    forward_vfov_deg: float,
) -> Tuple[int, int]:
    if pupil is not None and calibration.sample_count > 0:
        height, width = frame_shape[:2]
        px = pupil[0] / max(1.0, width)
        py = pupil[1] / max(1.0, height)
        # Prefer polynomial mapping if available. The model is fit in gaze angles
        # (yaw/pitch) derived from the calibration plate geometry, then projected
        # back to the forward frame.
        if calibration.poly_x is not None and calibration.poly_y is not None:
            cx = calibration.poly_x
            cy = calibration.poly_y
            pred_yaw_deg = cx[0] + cx[1] * px + cx[2] * py + cx[3] * px * px + cx[4] * py * py + cx[5] * px * py
            pred_pitch_deg = cy[0] + cy[1] * px + cy[2] * py + cy[3] * px * px + cy[4] * py * py + cy[5] * px * py
            return angle_to_screen_point(EyeAngles(yaw_deg=pred_yaw_deg, pitch_deg=pred_pitch_deg, confidence=eye_angles.confidence), frame_shape, horizontal_fov_deg=forward_hfov_deg, vertical_fov_deg=forward_vfov_deg)

        pred_yaw_deg = calibration.affine_x[0] * px + calibration.affine_x[1] * py + calibration.affine_x[2]
        pred_pitch_deg = calibration.affine_y[0] * px + calibration.affine_y[1] * py + calibration.affine_y[2]
        return angle_to_screen_point(EyeAngles(yaw_deg=pred_yaw_deg, pitch_deg=pred_pitch_deg, confidence=eye_angles.confidence), frame_shape, horizontal_fov_deg=forward_hfov_deg, vertical_fov_deg=forward_vfov_deg)

    return angle_to_screen_point(eye_angles, frame_shape, horizontal_fov_deg=forward_hfov_deg, vertical_fov_deg=forward_vfov_deg)


def annotate_eye_frame(
    frame: np.ndarray,
    pupil: Optional[Tuple[int, int, int]],
    eye_angles: EyeAngles,
    title: str,
    neutral_center: CalibrationModel,
    debug_view: Optional[np.ndarray],
    calibration_message: Optional[str],
) -> np.ndarray:
    output = frame.copy()
    height, width = output.shape[:2]
    neutral_point = (
        int(neutral_center.neutral_center_x_norm * width),
        int(neutral_center.neutral_center_y_norm * height),
    )
    cv2.circle(output, neutral_point, 6, (0, 160, 255), 2)
    cv2.line(output, (neutral_point[0] - 12, neutral_point[1]), (neutral_point[0] + 12, neutral_point[1]), (0, 160, 255), 1)
    cv2.line(output, (neutral_point[0], neutral_point[1] - 12), (neutral_point[0], neutral_point[1] + 12), (0, 160, 255), 1)

    if pupil is not None:
        cx, cy, radius = pupil
        cv2.circle(output, (cx, cy), radius, (0, 255, 0), 2)
        cv2.circle(output, (cx, cy), 3, (0, 255, 0), -1)
        cv2.line(output, neutral_point, (cx, cy), (0, 255, 0), 1)
    else:
        cv2.putText(output, "No pupil detected", (18, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 140, 255), 2)

    cv2.putText(output, title, (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.putText(output, f"Eye yaw: {eye_angles.yaw_deg:+.1f} deg", (18, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 2)
    cv2.putText(output, f"Eye pitch: {eye_angles.pitch_deg:+.1f} deg", (18, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 2)
    cv2.putText(output, f"Confidence: {eye_angles.confidence:.2f}", (18, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)

    if calibration_message:
        cv2.putText(output, calibration_message, (18, output.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (120, 220, 255), 1)

    if debug_view is not None:
        inset_h, inset_w = debug_view.shape[:2]
        margin = 12
        x0 = output.shape[1] - inset_w - margin
        y0 = 58
        x1 = min(output.shape[1], x0 + inset_w)
        y1 = min(output.shape[0], y0 + inset_h)
        if x0 >= 0 and y1 <= output.shape[0]:
            output[y0:y1, x0:x1] = debug_view[: y1 - y0, : x1 - x0]
            cv2.rectangle(output, (x0, y0), (x1, y1), (255, 255, 255), 1)
            cv2.putText(output, "Pupil debug", (x0, y0 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return output


def annotate_forward_frame(
    frame: np.ndarray,
    target_point: Tuple[int, int],
    forward_angles: EyeAngles,
    title: str,
    calibration_info: str,
    calibration_target: Optional[CalibrationTarget],
) -> np.ndarray:
    output = frame.copy()
    height, width = output.shape[:2]
    cv2.line(output, (width // 2, 0), (width // 2, height), (60, 60, 60), 1)
    cv2.line(output, (0, height // 2), (width, height // 2), (60, 60, 60), 1)
    draw_marker(output, target_point, (0, 200, 255), "predicted gaze")

    cv2.putText(output, title, (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.putText(output, f"Forward yaw: {forward_angles.yaw_deg:+.1f} deg", (18, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 2)
    cv2.putText(output, f"Forward pitch: {forward_angles.pitch_deg:+.1f} deg", (18, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 2)
    cv2.putText(output, calibration_info, (18, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (180, 180, 180), 1)
    if calibration_target is not None:
        target_x = int(np.clip(calibration_target.display_x_norm, 0.0, 1.0) * width)
        target_y = int(np.clip(calibration_target.display_y_norm, 0.0, 1.0) * height)
        cv2.circle(output, (target_x, target_y), 10, (255, 180, 0), 2)
        cv2.putText(output, f"Cal target: {calibration_target.label}", (18, 134), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 180, 0), 1)
    return output


def build_pupil_debug_view(detection: PupilDetection, pupil: Optional[Tuple[int, int, int]], size: Tuple[int, int] = (120, 180)) -> np.ndarray:
    debug = cv2.cvtColor(detection.threshold, cv2.COLOR_GRAY2BGR)
    debug = cv2.resize(debug, (size[1], size[0]), interpolation=cv2.INTER_NEAREST)
    cv2.putText(debug, f"contours: {detection.contour_count}", (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
    cv2.putText(debug, f"score: {detection.best_score:+.2f}", (6, size[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

    if pupil is not None:
        cx, cy, radius = pupil
        h, w = detection.threshold.shape[:2]
        x = int((cx / max(1.0, w)) * size[1])
        y = int((cy / max(1.0, h)) * size[0])
        r = max(2, int((radius / max(1.0, min(w, h))) * min(size)))
        cv2.circle(debug, (x, y), r, (0, 255, 0), 1)
        cv2.circle(debug, (x, y), 2, (0, 255, 0), -1)

    center = (size[1] // 2, size[0] // 2)
    cv2.line(debug, (center[0] - 10, center[1]), (center[0] + 10, center[1]), (0, 160, 255), 1)
    cv2.line(debug, (center[0], center[1] - 10), (center[0], center[1] + 10), (0, 160, 255), 1)
    return debug


def main() -> int:
    eye_default = os.getenv("EYE_STREAM_URL")
    forward_default = os.getenv("FORWARD_STREAM_URL")

    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--profile", choices=sorted(DEVICE_PROFILES.keys()), default=os.getenv("PROFILE", "child"), help="Device/user profile used for the global calibration defaults (env: PROFILE)")
    pre_args, remaining_argv = pre_parser.parse_known_args()
    profile = get_device_profile(pre_args.profile)

    parser = argparse.ArgumentParser(
        description="Estimate eye angle from an eye stream and map it to the forward camera",
        parents=[pre_parser],
    )
    parser.set_defaults(profile=profile.name)
    parser.add_argument("--eye-source", "--eye-url", dest="eye_source", default=eye_default, required=eye_default is None, help="Eye-facing source: MJPEG URL or local video file (env: EYE_STREAM_URL)")
    parser.add_argument("--forward-source", "--forward-url", dest="forward_source", default=forward_default, required=forward_default is None, help="Forward source: MJPEG URL or local video file (env: FORWARD_STREAM_URL)")
    parser.add_argument("--window-name", default="Eye / Forward Alignment", help="OpenCV window name")
    parser.add_argument("--eye-max-yaw-deg", type=float, default=profile.eye_max_yaw_deg, help="Maximum yaw estimate from the eye view")
    parser.add_argument("--eye-max-pitch-deg", type=float, default=profile.eye_max_pitch_deg, help="Maximum pitch estimate from the eye view")
    parser.add_argument("--forward-hfov-deg", type=float, default=profile.forward_hfov_deg, help="Approximate horizontal FOV used to place the target in the forward view")
    parser.add_argument("--forward-vfov-deg", type=float, default=profile.forward_vfov_deg, help="Approximate vertical FOV used to place the target in the forward view")
    parser.add_argument("--camera-offset-x-mm", type=float, default=profile.camera_offset_x_mm, help="Forward camera offset from the eye in the horizontal axis (mm)")
    parser.add_argument("--camera-offset-y-mm", type=float, default=profile.camera_offset_y_mm, help="Forward camera vertical offset from the eye (mm)")
    parser.add_argument("--camera-offset-z-mm", type=float, default=profile.camera_offset_z_mm, help="Forward camera offset from the eye in the forward axis (mm)")
    parser.add_argument("--assumed-depth-mm", type=float, default=profile.assumed_depth_mm, help="Depth used to convert eye angle into a 3D ray")
    parser.add_argument("--smoothing-alpha", type=float, default=profile.smoothing_alpha, help="EMA smoothing for estimated eye angles (0-1)")
    parser.add_argument("--calibration-file", default=None, help="Path to the calibration file")
    parser.add_argument("--calibration-hold-seconds", type=float, default=profile.calibration_hold_seconds, help="Time to keep the gaze on each calibration target")
    parser.add_argument("--replay-loop", action="store_true", help="Loop local video sources when they reach the end (useful for debug recordings)")
    parser.add_argument("--record-gaze", default=None, help="Optional CSV file to record per-frame gaze predictions")
    args = parser.parse_args(remaining_argv)

    if args.calibration_file is None:
        args.calibration_file = f"eye_forward_alignment_calibration_{profile.name}.json"

    calibration_path = Path(args.calibration_file)
    calibration = load_calibration(calibration_path)
    if calibration.profile != profile.name and calibration.source != "invalid-file":
        calibration.profile = profile.name

    eye_reader = MjpegStreamReader(args.eye_source, "eye", loop=args.replay_loop)
    forward_reader = MjpegStreamReader(args.forward_source, "forward", loop=args.replay_loop)
    eye_reader.start()
    forward_reader.start()

    eye_yaw_smoother = ScalarSmoother(args.smoothing_alpha)
    eye_pitch_smoother = ScalarSmoother(args.smoothing_alpha)
    _template_captured_at: float = 0.0
    calibration_session = CalibrationSession(args.calibration_hold_seconds, board_distance_cm=CALIBRATION_BOARD_DISTANCE_CM)
    gaze_log_file = None
    gaze_log_writer = None
    if args.record_gaze:
        gaze_log_file = open(args.record_gaze, 'w', newline='')
        gaze_log_writer = csv.writer(gaze_log_file)
        gaze_log_writer.writerow([
            'timestamp', 'found', 'pupil_x', 'pupil_y', 'pupil_radius',
            'eye_yaw_deg', 'eye_pitch_deg', 'forward_yaw_deg', 'forward_pitch_deg',
            'pred_x', 'pred_y', 'confidence', 'calibration_active', 'calibration_target'
        ])

    cv2.namedWindow(args.window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            eye_frame = eye_reader.state.frame
            forward_frame = forward_reader.state.frame

            if eye_frame is None:
                eye_frame = make_placeholder("EYE CAMERA")
            if forward_frame is None:
                forward_frame = make_placeholder("FORWARD CAMERA")

            detection = detect_pupil(eye_frame)
            pupil = detection.pupil
            # Drop detections whose score is too low — they are usually noise or
            # specular reflections misidentified as the pupil.
            if pupil is not None and detection.best_score < _PUPIL_MIN_SCORE:
                pupil = None

            calibration_target = calibration_session.current_target()
            if calibration_session.active:
                finished_calibration = calibration_session.add_sample(pupil, eye_frame.shape)
                if calibration_target is not None:
                    remaining_s = max(0.0, calibration_session.point_hold_seconds - (time.monotonic() - calibration_session.point_started_at))
                    progress = (
                        f"Calibration: point {calibration_session.current_index + 1}/{len(calibration_session.points)} "
                        f"({calibration_target.label}), {remaining_s:0.1f}s remaining"
                    )
                else:
                    progress = "Calibration running"
                if finished_calibration is not None:
                    finished_calibration.profile = profile.name
                    calibration = finished_calibration
                    save_calibration(calibration_path, calibration)
                    progress = (
                        f"Calibration saved to {calibration_path.name} [{profile.name}]: "
                        f"{calibration.point_count} points, {calibration.sample_count} samples"
                    )
            else:
                progress = f"Profile={profile.name} | calibrated points={calibration.point_count}, samples={calibration.sample_count} | press c to calibrate"

            raw_eye_angles = estimate_eye_angles(eye_frame, pupil, calibration, args.eye_max_yaw_deg, args.eye_max_pitch_deg)
            # Only advance the smoother when we have a valid detection. When the
            # pupil is lost (blink, occlusion, bad score) hold the last known
            # position — feeding (0,0) into the smoother would pull the gaze
            # circle to the centre every time the eye is briefly not detected.
            if pupil is not None:
                eye_yaw_smoother.update(raw_eye_angles.yaw_deg)
                eye_pitch_smoother.update(raw_eye_angles.pitch_deg)
            eye_angles = EyeAngles(
                yaw_deg=eye_yaw_smoother.value if eye_yaw_smoother.value is not None else 0.0,
                pitch_deg=eye_pitch_smoother.value if eye_pitch_smoother.value is not None else 0.0,
                confidence=raw_eye_angles.confidence if pupil is not None else 0.0,
            )

            forward_angles = eye_angles_to_forward_angles(
                eye_angles,
                camera_offset_x_mm=args.camera_offset_x_mm,
                camera_offset_y_mm=args.camera_offset_y_mm,
                camera_offset_z_mm=args.camera_offset_z_mm,
                assumed_depth_mm=args.assumed_depth_mm,
            )

            target_point = predict_forward_point(
                calibration,
                pupil,
                forward_angles,
                forward_frame.shape,
                forward_hfov_deg=args.forward_hfov_deg,
                forward_vfov_deg=args.forward_vfov_deg,
            )

            if gaze_log_writer is not None:
                gaze_log_writer.writerow([
                    time.time(),
                    pupil is not None,
                    None if pupil is None else pupil[0],
                    None if pupil is None else pupil[1],
                    None if pupil is None else pupil[2],
                    eye_angles.yaw_deg,
                    eye_angles.pitch_deg,
                    forward_angles.yaw_deg,
                    forward_angles.pitch_deg,
                    target_point[0],
                    target_point[1],
                    eye_angles.confidence,
                    calibration_session.active,
                    None if calibration_target is None else calibration_target.label,
                ])
                gaze_log_file.flush()

            debug_view = build_pupil_debug_view(detection, pupil) if detection.threshold is not None else None
            eye_panel = annotate_eye_frame(eye_frame, pupil, eye_angles, "Eye camera", calibration, debug_view, progress)
            forward_panel = annotate_forward_frame(forward_frame, target_point, forward_angles, "Forward camera", progress, calibration_target)
            # Show on-screen confirmation for 2 seconds after template capture.
            if time.monotonic() - _template_captured_at < 2.0:
                cv2.putText(forward_panel, "Template saved! (plate_template_captured.jpg)",
                            (12, forward_panel.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX,
                            0.55, (0, 255, 128), 2)
            combined = build_side_by_side(eye_panel, forward_panel)

            cv2.imshow(args.window_name, combined)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):
                calibration_session.start()
                print(f"Calibration started for profile '{profile.name}': {len(calibration_session.points)} points x {calibration_session.point_hold_seconds:.1f}s")
            if key == ord("r"):
                calibration = CalibrationModel(profile=profile.name)
                save_calibration(calibration_path, calibration)
                print("Calibration reset to default model")
            if key == ord("t"):
                # Capture the current forward frame as the plate template.
                # Use this instead of a phone photo -- the OV2640's noise and JPEG
                # compression characteristics must match the live feed for ORB to work.
                template_path = "plate_template_captured.jpg"
                ok = cv2.imwrite(template_path, forward_frame)
                if ok:
                    _template_captured_at = time.monotonic()
                    print("Plate template captured -> " + template_path + "  (pass this to --plate-template)")
                else:
                    print("ERROR: could not write " + template_path + " (check write permissions)")

            time.sleep(0.01)
    finally:
        eye_reader.stop()
        forward_reader.stop()
        if gaze_log_file is not None:
            gaze_log_file.close()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
