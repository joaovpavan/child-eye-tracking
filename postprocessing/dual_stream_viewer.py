from __future__ import annotations

import argparse
import os
import time
from typing import Optional

import cv2
from dotenv import load_dotenv

from mjpeg_stream import MjpegStreamReader, resize_and_pad, make_placeholder, add_header, build_side_by_side
from plate_tracker import PlateTracker

load_dotenv()


def main() -> int:
    left_default = os.getenv("EYE_STREAM_URL")
    right_default = os.getenv("FORWARD_STREAM_URL")

    parser = argparse.ArgumentParser(description="Show two ESP32 MJPEG streams side by side")
    parser.add_argument("--left-source", "--left-url", dest="left_source", default=left_default, required=left_default is None, help="Left source: MJPEG URL or local video file (env: EYE_STREAM_URL)")
    parser.add_argument("--right-source", "--right-url", dest="right_source", default=right_default, required=right_default is None, help="Right source: MJPEG URL or local video file (env: FORWARD_STREAM_URL)")
    parser.add_argument("--window-name", default="Dual ESP32 Streams", help="OpenCV window name")
    parser.add_argument("--plate-template", default=None, help="Path to plate template image for detector (optional)")
    parser.add_argument("--record", default=None, help="CSV path to record per-frame plate logs (optional)")
    parser.add_argument("--replay-loop", action="store_true", help="Loop local video sources when they reach the end (useful for debug recordings)")
    parser.add_argument("--denoise", action="store_true", help="Apply bilateral filtering to reduce noise (useful for ESP32 cameras)")
    args = parser.parse_args()

    left_reader = MjpegStreamReader(args.left_source, "left", loop=args.replay_loop)
    right_reader = MjpegStreamReader(args.right_source, "right", loop=args.replay_loop)
    left_reader.start()
    right_reader.start()

    plate_tracker = None
    log_file = None
    log_writer = None
    if args.plate_template:
        try:
            plate_tracker = PlateTracker(args.plate_template)
        except Exception as e:
            print(f"Failed to initialize PlateTracker: {e}")

    if args.record:
        log_file = open(args.record, 'w', newline='')
        import csv as _csv
        log_writer = _csv.writer(log_file)
        log_writer.writerow(['timestamp', 'left_connected', 'right_connected', 'plate_found', 'bbox_x', 'bbox_y', 'bbox_w', 'bbox_h', 'centroid_x', 'centroid_y', 'angle', 'confidence'])

    cv2.namedWindow(args.window_name, cv2.WINDOW_NORMAL)

    try:
        while True:
            left_frame = left_reader.state.frame
            right_frame = right_reader.state.frame

            if left_frame is None:
                left_frame = make_placeholder("LEFT CAMERA")
            if right_frame is None:
                right_frame = make_placeholder("RIGHT CAMERA")

            left_frame = add_header(left_frame, "Left stream", left_reader.state.connected, left_reader.state.error)
            right_frame = add_header(right_frame, "Right stream", right_reader.state.connected, right_reader.state.error)

            # Apply denoising if requested
            if args.denoise:
                left_frame = cv2.bilateralFilter(left_frame, 9, 75, 75)
                right_frame = cv2.bilateralFilter(right_frame, 9, 75, 75)

            # Plate detection on right (forward) camera
            plate_res = None
            if plate_tracker is not None and right_frame is not None:
                try:
                    plate_res = plate_tracker.process_frame(right_frame, log_path=None)
                except Exception:
                    plate_res = None

            # overlay plate detection on right_frame
            if plate_res and plate_res.get('found'):
                bx, by, bw, bh = plate_res.get('bbox')
                cx, cy = plate_res.get('centroid') or (None, None)
                cv2.rectangle(right_frame, (int(bx), int(by)), (int(bx + bw), int(by + bh)), (0, 200, 0), 2)
                if cx is not None and cy is not None:
                    cv2.circle(right_frame, (int(cx), int(cy)), 4, (0, 0, 255), -1)
                conf = plate_res.get('confidence')
                cv2.putText(right_frame, f"Plate: {conf:.2f}", (int(bx), int(by) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 2)

            # write per-frame log row if requested (includes plate info)
            if log_writer is not None:
                ts = time.time()
                if plate_res and plate_res.get('found'):
                    bx, by, bw, bh = plate_res.get('bbox')
                    cx, cy = plate_res.get('centroid') or (None, None)
                    angle = plate_res.get('angle')
                    conf = plate_res.get('confidence')
                    log_writer.writerow([ts, left_reader.state.connected, right_reader.state.connected, True, bx, by, bw, bh, cx, cy, angle, conf])
                else:
                    log_writer.writerow([ts, left_reader.state.connected, right_reader.state.connected, False, None, None, None, None, None, None, None, None])
                log_file.flush()

            combined = build_side_by_side(left_frame, right_frame)
            cv2.imshow(args.window_name, combined)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break

            time.sleep(0.01)
    finally:
        left_reader.stop()
        right_reader.stop()
        if log_file is not None:
            log_file.close()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
