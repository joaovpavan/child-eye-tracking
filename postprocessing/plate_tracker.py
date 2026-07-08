import os
import time
import csv
from typing import Optional, Dict, Any

import cv2
import numpy as np


class PlateTracker:
    """Detect a fixed plate using multi-scale normalised cross-correlation
    (cv2.TM_CCOEFF_NORMED) and track it with CSRT between detections.

    Template matching is preferred over ORB for fixed plates because it is
    robust to low-contrast, noisy, or gradient-rich images where ORB cannot
    find reliable keypoints.

    Usage:
      tracker = PlateTracker('plate_template_captured.jpg')
      res = tracker.process_frame(frame)
      if res['found']: use res['centroid'], res['bbox']
    """

    def __init__(self, template_path: str, detect_interval: int = 15,
                 match_threshold: float = 0.35):
        if not os.path.exists(template_path):
            raise FileNotFoundError(template_path)
        self.template_path = template_path
        self.detect_interval = detect_interval
        self.match_threshold = match_threshold

        self.template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
        if self.template is None:
            raise RuntimeError('Failed to read template image')

        self.template_h, self.template_w = self.template.shape[:2]
        self._clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        # Preprocess template identically to live frames.
        tpl_blur = cv2.GaussianBlur(self.template, (3, 3), 0)
        self.template_clahe = self._clahe.apply(tpl_blur)

        self.tracker = None
        self.tracker_bbox = None
        self.frame_count = 0
        self._log_initialized = False

    def _create_tracker(self):
        try:
            return cv2.TrackerCSRT_create()
        except Exception:
            try:
                return cv2.legacy.TrackerCSRT_create()
            except Exception:
                return None

    def _detect(self, frame_gray: np.ndarray) -> Optional[Dict[str, Any]]:
        fh, fw = frame_gray.shape[:2]

        denoised = cv2.GaussianBlur(frame_gray, (3, 3), 0)
        fframe = self._clahe.apply(denoised)

        # Search across scales: plate width between 8% and 95% of frame width.
        min_w = max(20, int(fw * 0.08))
        max_w = min(fw - 4, int(fw * 0.95))
        if min_w >= max_w:
            return None

        raw_scales = np.geomspace(
            min_w / max(1, self.template_w),
            max_w / max(1, self.template_w),
            num=10,
        )

        best_val = -1.0
        best_loc: Optional[tuple] = None
        best_tw = best_th = 0

        for scale in raw_scales:
            tw = int(self.template_w * scale)
            th = int(self.template_h * scale)
            if tw < 20 or th < 20 or tw >= fw or th >= fh:
                continue

            tpl = cv2.resize(self.template_clahe, (tw, th),
                             interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(fframe, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val > best_val:
                best_val = max_val
                best_loc = max_loc
                best_tw, best_th = tw, th

        if self.frame_count % 30 == 0:
            print(f"[PlateTracker] frame {self.frame_count}: "
                  f"best match {best_val:.3f} (threshold {self.match_threshold})")

        if best_val < self.match_threshold or best_loc is None:
            return None

        x, y = best_loc
        bbox = (x, y, best_tw, best_th)
        centroid = (x + best_tw / 2.0, y + best_th / 2.0)
        return dict(method='template', corners=None, bbox=bbox,
                    centroid=centroid, angle=0.0, confidence=float(best_val))

    def process_frame(self, frame: np.ndarray,
                      log_path: Optional[str] = None) -> Dict[str, Any]:
        self.frame_count += 1
        res = dict(found=False, method=None, bbox=None, centroid=None,
                   corners=None, angle=None, confidence=0.0,
                   timestamp=time.time())

        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Try CSRT tracker first — fast path between detections.
        if self.tracker is not None and self.tracker_bbox is not None:
            ok, box = self.tracker.update(frame)
            if ok:
                x, y, w, h = map(int, box)
                res.update(found=True, method='tracker',
                           bbox=(x, y, w, h),
                           centroid=(x + w / 2.0, y + h / 2.0),
                           confidence=0.9)
                if log_path:
                    self._append_log(log_path, res)
                return res
            # Tracker lost — fall through to detection.
            self.tracker = None
            self.tracker_bbox = None

        do_detect = (self.frame_count % self.detect_interval == 0
                     or self.tracker is None)

        if do_detect:
            det = self._detect(frame_gray)
            if det is not None:
                res.update(found=True, **{k: det[k] for k in
                           ('method', 'bbox', 'centroid', 'corners',
                            'angle', 'confidence')})
                if res['bbox'] is not None:
                    tr = self._create_tracker()
                    if tr is not None:
                        try:
                            tr.init(frame, tuple(res['bbox']))
                            self.tracker = tr
                            self.tracker_bbox = res['bbox']
                        except Exception:
                            self.tracker = None

        if log_path:
            self._append_log(log_path, res)
        return res

    def _append_log(self, log_path: str, res: Dict[str, Any]):
        exists = os.path.exists(log_path)
        with open(log_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if not exists and not self._log_initialized:
                writer.writerow(['timestamp', 'found', 'method',
                                 'bbox_x', 'bbox_y', 'bbox_w', 'bbox_h',
                                 'centroid_x', 'centroid_y',
                                 'angle', 'confidence'])
                self._log_initialized = True
            bbox = res.get('bbox') or (None, None, None, None)
            centroid = res.get('centroid') or (None, None)
            writer.writerow([res.get('timestamp'), res.get('found'),
                             res.get('method'),
                             bbox[0], bbox[1], bbox[2], bbox[3],
                             centroid[0], centroid[1],
                             res.get('angle'), res.get('confidence')])


if __name__ == '__main__':
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument('--template', required=True)
    p.add_argument('--source', default=0)
    p.add_argument('--threshold', type=float, default=0.35)
    p.add_argument('--log', default=None)
    args = p.parse_args()

    cap = cv2.VideoCapture(
        int(args.source) if str(args.source).isdigit() else args.source)
    pt = PlateTracker(args.template, match_threshold=args.threshold)

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        res = pt.process_frame(frame, log_path=args.log)
        if res['found']:
            x, y, w, h = res['bbox']
            cv2.rectangle(frame, (int(x), int(y)),
                          (int(x + w), int(y + h)), (0, 255, 0), 2)
            if res['centroid']:
                cx, cy = map(int, res['centroid'])
                cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)
            cv2.putText(frame, f"conf={res['confidence']:.2f}",
                        (int(x), int(y) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imshow('plate', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
