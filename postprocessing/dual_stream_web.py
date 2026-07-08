#!/usr/bin/env python3
"""Web-based dual stream viewer with calibration, exam phases, and live gaze overlay."""

import argparse
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, render_template, Response, jsonify, request

load_dotenv()

from eye_forward_alignment import (
    CalibrationModel,
    CalibrationSession,
    EyeAngles,
    detect_pupil,
    estimate_eye_angles,
    eye_angles_to_forward_angles,
    fit_calibration_model,
    get_device_profile,
    load_calibration,
    predict_forward_point,
    save_calibration,
    ScalarSmoother,
)
from mjpeg_stream import MjpegStreamReader, add_header, make_placeholder
from plate_tracker import PlateTracker


@dataclass
class GazeState:
    pupil: Optional[Tuple[int, int, int]] = None
    eye_angles: Optional[EyeAngles] = None
    gaze_point: Optional[Tuple[int, int]] = None  # pixel coords on forward frame


class StreamProcessor:
    def __init__(
        self,
        left_source: str,
        right_source: str,
        loop: bool = False,
        output_dir: str = "recordings",
        profile_name: str = "child",
        calibration_file: Optional[str] = None,
    ):
        self.left_reader = MjpegStreamReader(left_source, "left", loop=loop)
        self.right_reader = MjpegStreamReader(right_source, "right", loop=loop)
        self.left_reader.start()
        self.right_reader.start()

        self.plate_tracker: Optional[PlateTracker] = None
        self.denoise = False
        self.lock = threading.Lock()

        # Recording
        self.output_dir = output_dir
        self.recording = False
        self.record_thread: Optional[threading.Thread] = None
        self.record_event = threading.Event()
        os.makedirs(output_dir, exist_ok=True)

        # Device profile and calibration
        self.profile = get_device_profile(profile_name)
        self.calibration_path = Path(calibration_file or f"calibration_{profile_name}.json")
        self.calibration: CalibrationModel = load_calibration(self.calibration_path)

        # Phase state machine: idle → calibrating → ready → exam → done
        self.phase = "idle"

        # Live gaze state (written by processing thread, read by stream generators)
        self.gaze_state = GazeState()

        # Smoothers — only touched by the processing thread, no lock needed
        self._yaw_smoother = ScalarSmoother(self.profile.smoothing_alpha)
        self._pitch_smoother = ScalarSmoother(self.profile.smoothing_alpha)

        # Calibration session (active only during "calibrating" phase)
        self.calibration_session: Optional[CalibrationSession] = None

        # Exam metrics
        self.exam_start_time: Optional[float] = None
        self.exam_frames_total = 0
        self.exam_frames_on_target = 0
        self.last_results: Optional[dict] = None

        # Last plate detection result (written by get_right_frame for the processing thread)
        self._last_plate_info: Optional[dict] = None

        # Background processing thread
        self._stop_event = threading.Event()
        self._proc_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._proc_thread.start()

    # ------------------------------------------------------------------ #
    # Processing thread: pupil → gaze → calibration samples → proficiency #
    # ------------------------------------------------------------------ #

    def _process_loop(self) -> None:
        while not self._stop_event.is_set():
            eye_frame = self.left_reader.state.frame
            if eye_frame is None:
                time.sleep(0.02)
                continue

            # Pupil detection
            detection = detect_pupil(eye_frame)
            pupil = detection.pupil

            with self.lock:
                cal = self.calibration
                profile = self.profile
                phase = self.phase
                forward_frame = self.right_reader.state.frame

            # Eye angle estimation + EMA smoothing
            eye_angles_raw = estimate_eye_angles(
                eye_frame, pupil, cal, profile.eye_max_yaw_deg, profile.eye_max_pitch_deg,
            )
            yaw = self._yaw_smoother.update(eye_angles_raw.yaw_deg)
            pitch = self._pitch_smoother.update(eye_angles_raw.pitch_deg)
            eye_angles = EyeAngles(yaw_deg=yaw, pitch_deg=pitch, confidence=eye_angles_raw.confidence)

            # Project gaze to forward camera pixel
            forward_angles = eye_angles_to_forward_angles(
                eye_angles,
                profile.camera_offset_x_mm,
                profile.camera_offset_y_mm,
                profile.camera_offset_z_mm,
                profile.assumed_depth_mm,
            )
            gaze_point: Optional[Tuple[int, int]] = None
            if forward_frame is not None:
                gaze_point = predict_forward_point(
                    cal, pupil, forward_angles, forward_frame.shape,
                    profile.forward_hfov_deg, profile.forward_vfov_deg,
                )

            with self.lock:
                self.gaze_state = GazeState(pupil=pupil, eye_angles=eye_angles, gaze_point=gaze_point)

            # Calibration sample collection
            if phase == "calibrating":
                with self.lock:
                    session = self.calibration_session
                if session is not None and session.active:
                    result = session.add_sample(pupil, eye_frame.shape)
                    # Time-based advancement even when pupil is not detected
                    if result is None and pupil is None:
                        elapsed = time.monotonic() - session.point_started_at
                        if elapsed >= session.point_hold_seconds:
                            session.current_index += 1
                            if session.current_index >= len(session.points):
                                session.stop()
                                result = (
                                    fit_calibration_model(session.samples)
                                    if session.samples
                                    else CalibrationModel()
                                )
                            else:
                                session.point_started_at = time.monotonic()
                    if result is not None:
                        save_calibration(self.calibration_path, result)
                        with self.lock:
                            self.calibration = result
                            self.phase = "ready"
                            self.calibration_session = None

            # Plate tracking (single-threaded here to avoid PlateTracker race condition)
            if self.plate_tracker is not None and forward_frame is not None:
                try:
                    plate_res = self.plate_tracker.process_frame(forward_frame, log_path=None)
                    plate_info = (
                        {
                            "found": True,
                            "bbox": plate_res.get("bbox"),
                            "centroid": plate_res.get("centroid"),
                            "confidence": plate_res.get("confidence"),
                            "angle": plate_res.get("angle"),
                        }
                        if plate_res and plate_res.get("found")
                        else None
                    )
                    with self.lock:
                        self._last_plate_info = plate_info
                except Exception:
                    pass

            # Exam proficiency tracking
            if phase == "exam":
                with self.lock:
                    plate_info = self._last_plate_info
                    self.exam_frames_total += 1
                    if (
                        gaze_point is not None
                        and plate_info
                        and plate_info.get("found")
                        and plate_info.get("bbox")
                    ):
                        bx, by, bw, bh = plate_info["bbox"]
                        gx, gy = gaze_point
                        if bx <= gx <= bx + bw and by <= gy <= by + bh:
                            self.exam_frames_on_target += 1

            time.sleep(0.02)  # ~50 Hz

    # ------------------------------------------------------------------ #
    # Calibration                                                          #
    # ------------------------------------------------------------------ #

    def start_calibration(self, board_distance_cm: float = 30.0, hold_seconds: float = 2.0) -> bool:
        with self.lock:
            if self.phase not in ("idle", "ready"):
                return False
            session = CalibrationSession(
                point_hold_seconds=hold_seconds,
                board_distance_cm=board_distance_cm,
            )
            session.start()
            self.calibration_session = session
            self.phase = "calibrating"
        return True

    def abort_calibration(self) -> bool:
        with self.lock:
            if self.phase != "calibrating":
                return False
            if self.calibration_session:
                self.calibration_session.stop()
                self.calibration_session = None
            self.phase = "idle"
        return True

    def get_calibration_status(self) -> dict:
        with self.lock:
            session = self.calibration_session
            phase = self.phase
        if session is None or not session.active:
            return {"active": False, "phase": phase}
        target = session.current_target()
        elapsed = time.monotonic() - session.point_started_at
        return {
            "active": True,
            "phase": phase,
            "current_index": session.current_index,
            "total_points": len(session.points),
            "current_label": target.label if target else None,
            "display_x_norm": target.display_x_norm if target else 0.5,
            "display_y_norm": target.display_y_norm if target else 0.5,
            "elapsed_s": round(min(elapsed, session.point_hold_seconds), 2),
            "hold_s": session.point_hold_seconds,
            "samples_collected": len(session.samples),
        }

    # ------------------------------------------------------------------ #
    # Exam                                                                 #
    # ------------------------------------------------------------------ #

    def start_exam(self) -> bool:
        with self.lock:
            if self.phase not in ("ready", "done"):
                return False
            self.phase = "exam"
            self.exam_start_time = time.time()
            self.exam_frames_total = 0
            self.exam_frames_on_target = 0
            self.last_results = None
        return True

    def stop_exam(self) -> dict:
        with self.lock:
            if self.phase != "exam":
                return {}
            self.phase = "done"
            total = self.exam_frames_total
            on_target = self.exam_frames_on_target
            duration = time.time() - (self.exam_start_time or time.time())
        proficiency = round(on_target / max(1, total) * 100.0, 1)
        results = {
            "duration_s": round(duration, 1),
            "frames_total": total,
            "frames_on_target": on_target,
            "proficiency_pct": proficiency,
        }
        with self.lock:
            self.last_results = results
        return results

    def get_live_proficiency(self) -> dict:
        with self.lock:
            total = self.exam_frames_total
            on_target = self.exam_frames_on_target
            elapsed = time.time() - (self.exam_start_time or time.time())
        return {
            "frames_total": total,
            "frames_on_target": on_target,
            "proficiency_pct": round(on_target / max(1, total) * 100.0, 1),
            "elapsed_s": round(elapsed, 1),
        }

    # ------------------------------------------------------------------ #
    # Frame accessors                                                      #
    # ------------------------------------------------------------------ #

    def set_denoise(self, enabled: bool) -> None:
        with self.lock:
            self.denoise = enabled

    def get_left_frame(self) -> Optional[np.ndarray]:
        frame = self.left_reader.state.frame
        if frame is None:
            return None
        with self.lock:
            if self.denoise:
                frame = cv2.bilateralFilter(frame, 9, 75, 75)
        return frame

    def get_right_frame(self) -> Tuple[Optional[np.ndarray], Optional[dict]]:
        frame = self.right_reader.state.frame
        if frame is None:
            return None, None
        with self.lock:
            if self.denoise:
                frame = cv2.bilateralFilter(frame, 9, 75, 75)
            plate_info = self._last_plate_info
        return frame, plate_info

    def init_plate_tracker(self, template_path: str) -> None:
        try:
            self.plate_tracker = PlateTracker(template_path)
        except Exception as e:
            print(f"Failed to initialize PlateTracker: {e}")

    def get_status(self) -> dict:
        with self.lock:
            phase = self.phase
            gaze = self.gaze_state
            cal = self.calibration
        return {
            "left_connected": self.left_reader.state.connected,
            "left_error": self.left_reader.state.error,
            "right_connected": self.right_reader.state.connected,
            "right_error": self.right_reader.state.error,
            "phase": phase,
            "pupil_detected": gaze.pupil is not None,
            "calibration_samples": cal.sample_count,
            "calibration_points": cal.point_count,
        }

    # ------------------------------------------------------------------ #
    # Recording                                                            #
    # ------------------------------------------------------------------ #

    def start_recording(self) -> bool:
        with self.lock:
            if self.recording:
                return False
            self.recording = True
        self.record_event.clear()
        self.record_thread = threading.Thread(target=self._record_streams, daemon=True)
        self.record_thread.start()
        return True

    def stop_recording(self) -> bool:
        with self.lock:
            if not self.recording:
                return False
            self.recording = False
        self.record_event.set()
        if self.record_thread:
            self.record_thread.join(timeout=5.0)
        return True

    def is_recording(self) -> bool:
        with self.lock:
            return self.recording

    def _record_streams(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        left_path = os.path.join(self.output_dir, f"left_{timestamp}.mp4")
        right_path = os.path.join(self.output_dir, f"right_{timestamp}.mp4")
        left_writer = None
        right_writer = None
        start_time = time.time()
        duration = 30

        try:
            while not self.record_event.is_set() and (time.time() - start_time) < duration:
                left_frame = self.get_left_frame()
                right_frame, _ = self.get_right_frame()

                if left_frame is not None:
                    if left_writer is None:
                        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                        left_writer = cv2.VideoWriter(
                            left_path, fourcc, 30, (left_frame.shape[1], left_frame.shape[0])
                        )
                    if left_writer.isOpened():
                        left_writer.write(left_frame)

                if right_frame is not None:
                    if right_writer is None:
                        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                        right_writer = cv2.VideoWriter(
                            right_path, fourcc, 30, (right_frame.shape[1], right_frame.shape[0])
                        )
                    if right_writer.isOpened():
                        right_writer.write(right_frame)

                time.sleep(0.033)
        finally:
            any_written = False
            if left_writer:
                left_writer.release()
                any_written = True
            if right_writer:
                right_writer.release()
                any_written = True
            if any_written:
                print(f"Recording saved: {left_path}, {right_path}")
            else:
                print("Recording ended: no frames received, no files written")

    def stop(self) -> None:
        self._stop_event.set()
        self.left_reader.stop()
        self.right_reader.stop()


# --------------------------------------------------------------------------- #
# Frame annotation helpers                                                      #
# --------------------------------------------------------------------------- #

def _draw_pupil_overlay(frame: np.ndarray, gaze: GazeState, cal: CalibrationModel) -> np.ndarray:
    """Draw pupil detection and neutral-center crosshair on the eye frame."""
    out = frame.copy()
    h, w = out.shape[:2]
    nx = int(cal.neutral_center_x_norm * w)
    ny = int(cal.neutral_center_y_norm * h)
    cv2.line(out, (nx - 14, ny), (nx + 14, ny), (0, 160, 255), 1)
    cv2.line(out, (nx, ny - 14), (nx, ny + 14), (0, 160, 255), 1)
    cv2.circle(out, (nx, ny), 5, (0, 160, 255), 1)

    if gaze.pupil is not None:
        cx, cy, r = gaze.pupil
        cv2.circle(out, (cx, cy), r, (0, 255, 0), 2)
        cv2.circle(out, (cx, cy), 3, (0, 255, 0), -1)
        cv2.line(out, (nx, ny), (cx, cy), (0, 200, 100), 1)
    return out


def _draw_gaze_overlay(
    frame: np.ndarray,
    gaze: GazeState,
    plate_info: Optional[dict],
    cal_target_norm: Optional[Tuple[float, float]],
) -> np.ndarray:
    """Draw gaze point, plate bbox, and optional calibration target on the forward frame."""
    out = frame.copy()
    h, w = out.shape[:2]

    # Calibration target marker
    if cal_target_norm is not None:
        tx = int(cal_target_norm[0] * w)
        ty = int(cal_target_norm[1] * h)
        cv2.circle(out, (tx, ty), 14, (255, 180, 0), 2)
        cv2.circle(out, (tx, ty), 4, (255, 180, 0), -1)
        cv2.line(out, (tx - 20, ty), (tx + 20, ty), (255, 180, 0), 1)
        cv2.line(out, (tx, ty - 20), (tx, ty + 20), (255, 180, 0), 1)

    # Plate bounding box
    if plate_info and plate_info.get("found") and plate_info.get("bbox"):
        bx, by, bw, bh = plate_info["bbox"]
        cv2.rectangle(out, (int(bx), int(by)), (int(bx + bw), int(by + bh)), (0, 200, 0), 2)
        cent = plate_info.get("centroid")
        if cent:
            cv2.circle(out, (int(cent[0]), int(cent[1])), 4, (0, 0, 255), -1)
        conf = plate_info.get("confidence", 0.0)
        cv2.putText(
            out, f"Plate {conf:.2f}",
            (int(bx), max(0, int(by) - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 0), 1,
        )

    # Gaze point
    if gaze.gaze_point is not None:
        gx, gy = gaze.gaze_point
        overlay = out.copy()
        cv2.circle(overlay, (gx, gy), 18, (0, 120, 255), -1)
        out = cv2.addWeighted(overlay, 0.35, out, 0.65, 0)
        cv2.circle(out, (gx, gy), 18, (0, 180, 255), 2)
        cv2.circle(out, (gx, gy), 3, (255, 255, 255), -1)

    return out


# --------------------------------------------------------------------------- #
# Flask app                                                                     #
# --------------------------------------------------------------------------- #

def create_app(processor: StreamProcessor) -> Flask:
    template_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, template_folder=template_dir)

    # ---- Pages ----

    @app.route("/")
    def index():
        return render_template("dual_stream.html")

    # ---- Status ----

    @app.route("/api/status")
    def status():
        s = processor.get_status()
        if s["phase"] == "exam":
            s["live"] = processor.get_live_proficiency()
        return jsonify(s)

    # ---- Denoise ----

    @app.route("/api/denoise", methods=["GET", "POST"])
    def denoise_control():
        if request.method == "POST":
            data = request.get_json() or {}
            processor.set_denoise(data.get("enabled", False))
        with processor.lock:
            denoise_state = processor.denoise
        return jsonify({"denoise": denoise_state})

    # ---- Calibration ----

    @app.route("/api/calibrate/start", methods=["POST"])
    def calibrate_start():
        data = request.get_json() or {}
        ok = processor.start_calibration(
            board_distance_cm=float(data.get("board_distance_cm", 30.0)),
            hold_seconds=float(data.get("hold_seconds", 2.0)),
        )
        return jsonify({"success": ok, "phase": processor.phase})

    @app.route("/api/calibrate/status")
    def calibrate_status():
        return jsonify(processor.get_calibration_status())

    @app.route("/api/calibrate/abort", methods=["POST"])
    def calibrate_abort():
        ok = processor.abort_calibration()
        return jsonify({"success": ok, "phase": processor.phase})

    # ---- Exam ----

    @app.route("/api/exam/start", methods=["POST"])
    def exam_start():
        ok = processor.start_exam()
        return jsonify({"success": ok, "phase": processor.phase})

    @app.route("/api/exam/stop", methods=["POST"])
    def exam_stop():
        results = processor.stop_exam()
        return jsonify({"success": bool(results), "phase": processor.phase, "results": results})

    @app.route("/api/results")
    def results():
        with processor.lock:
            r = processor.last_results
        return jsonify(r or {})

    # ---- Recording ----

    @app.route("/api/record", methods=["GET", "POST"])
    def record_control():
        if request.method == "POST":
            data = request.get_json() or {}
            action = data.get("action", "status")
            if action == "start":
                success = processor.start_recording()
                return jsonify({"recording": processor.is_recording(), "success": success})
            elif action == "stop":
                success = processor.stop_recording()
                return jsonify({"recording": processor.is_recording(), "success": success})
        return jsonify({"recording": processor.is_recording()})

    # ---- Streams ----

    @app.route("/stream/left")
    def stream_left():
        def generate():
            try:
                while True:
                    frame = processor.get_left_frame()
                    status = processor.get_status()

                    if frame is None:
                        frame = make_placeholder("EYE CAMERA", size=(240, 320))
                    else:
                        with processor.lock:
                            gaze = processor.gaze_state
                            cal = processor.calibration
                        frame = _draw_pupil_overlay(frame, gaze, cal)

                    frame = add_header(frame, "Eye stream", status["left_connected"], status["left_error"])
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    frame_bytes = buf.tobytes()
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame_bytes)).encode()
                        + b"\r\n\r\n"
                        + frame_bytes
                        + b"\r\n"
                    )
                    time.sleep(0.033)
            except GeneratorExit:
                pass

        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/stream/right")
    def stream_right():
        def generate():
            try:
                while True:
                    frame, plate_info = processor.get_right_frame()
                    status = processor.get_status()

                    if frame is None:
                        frame = make_placeholder("FORWARD CAMERA", size=(240, 320))
                    else:
                        with processor.lock:
                            gaze = processor.gaze_state
                            phase = processor.phase
                            session = processor.calibration_session

                        cal_target_norm: Optional[Tuple[float, float]] = None
                        if phase == "calibrating" and session is not None:
                            target = session.current_target()
                            if target is not None:
                                cal_target_norm = (target.display_x_norm, target.display_y_norm)

                        frame = _draw_gaze_overlay(frame, gaze, plate_info, cal_target_norm)

                    frame = add_header(frame, "Forward stream", status["right_connected"], status["right_error"])
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    frame_bytes = buf.tobytes()
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame_bytes)).encode()
                        + b"\r\n\r\n"
                        + frame_bytes
                        + b"\r\n"
                    )
                    time.sleep(0.033)
            except GeneratorExit:
                pass

        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    return app


# --------------------------------------------------------------------------- #
# Entry point                                                                   #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    left_default = os.getenv("EYE_STREAM_URL")
    right_default = os.getenv("FORWARD_STREAM_URL")

    parser = argparse.ArgumentParser(description="Web-based dual ESP32 stream viewer")
    parser.add_argument("--left-source", "--left-url", dest="left_source", default=left_default, required=left_default is None, help="env: EYE_STREAM_URL")
    parser.add_argument("--right-source", "--right-url", dest="right_source", default=right_default, required=right_default is None, help="env: FORWARD_STREAM_URL")
    parser.add_argument("--plate-template", default=None)
    parser.add_argument("--replay-loop", action="store_true")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "5000")), help="env: WEB_PORT")
    parser.add_argument("--denoise", action="store_true")
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "recordings"), help="env: OUTPUT_DIR")
    parser.add_argument("--profile", choices=["child", "adult"], default=os.getenv("PROFILE", "child"), help="env: PROFILE")
    parser.add_argument("--calibration-file", default=None)
    args = parser.parse_args()

    processor = StreamProcessor(
        args.left_source,
        args.right_source,
        loop=args.replay_loop,
        output_dir=args.output_dir,
        profile_name=args.profile,
        calibration_file=args.calibration_file,
    )

    if args.plate_template:
        processor.init_plate_tracker(args.plate_template)

    if args.denoise:
        processor.set_denoise(True)

    app = create_app(processor)

    print(f"Starting web server on http://localhost:{args.port}")
    print(f"Left source:  {args.left_source}")
    print(f"Right source: {args.right_source}")
    print(f"Profile:      {args.profile}")
    print(f"Calibration:  {processor.calibration_path}")
    print(f"Output dir:   {args.output_dir}")

    try:
        app.run(host="localhost", port=args.port, debug=False, threaded=True)
    finally:
        if processor.is_recording():
            processor.stop_recording()
        processor.stop()
