#!/usr/bin/env python3
"""Validate a recorded session by computing residuals between predicted gaze
and the plate centroid. Supports a single combined CSV or two CSVs (gaze log
and plate log) which are time-joined.

Outputs a JSON report and optional plots.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from typing import List, Dict, Optional, Tuple

import numpy as np


def read_csv_rows(path: str) -> List[Dict[str, Optional[float]]]:
    rows: List[Dict[str, Optional[float]]] = []
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            out: Dict[str, Optional[float]] = {}
            for k, v in r.items():
                if v is None or v == "":
                    out[k] = None
                else:
                    # try numeric
                    try:
                        out[k] = float(v)
                    except Exception:
                        out[k] = v
            rows.append(out)
    return rows


def _first_not_none(d: dict, *keys):
    """Return the first value in d whose key is in keys and whose value is not None."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def is_truthy(value) -> bool:
    if value in (True, 1, 1.0):
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return False


def join_on_timestamp(primary: List[Dict], secondary: List[Dict], max_dt: float = 0.05) -> List[Tuple[Dict, Dict]]:
    # Both lists should have 'timestamp' as float
    prim = [r for r in primary if r.get('timestamp') is not None]
    sec = [r for r in secondary if r.get('timestamp') is not None]
    prim.sort(key=lambda x: x['timestamp'])
    sec.sort(key=lambda x: x['timestamp'])

    pairs: List[Tuple[Dict, Dict]] = []
    j = 0
    nsec = len(sec)
    for p in prim:
        t = p['timestamp']
        best = None
        best_dt = max_dt + 1.0
        while j < nsec and sec[j]['timestamp'] < t - max_dt:
            j += 1
        # look around j
        for k in range(max(0, j - 2), min(nsec, j + 3)):
            dt = abs(sec[k]['timestamp'] - t)
            if dt <= max_dt and dt < best_dt:
                best_dt = dt
                best = sec[k]
        if best is not None:
            pairs.append((p, best))
    return pairs


def compute_metrics(pairs: List[Tuple[Dict, Dict]]) -> Dict:
    pred_x = []
    pred_y = []
    targ_x = []
    targ_y = []

    for g, p in pairs:
        # g = gaze row, p = plate row (or viceversa) depending on join order
        # detect which has predicted columns
        if 'pred_x' in g or 'predicted_x' in g or 'pred_x_px' in g:
            gx = _first_not_none(g, 'pred_x', 'predicted_x', 'pred_x_px')
            gy = _first_not_none(g, 'pred_y', 'predicted_y', 'pred_y_px')
            tx = _first_not_none(p, 'centroid_x', 'centroid_x_px')
            ty = _first_not_none(p, 'centroid_y', 'centroid_y_px')
        else:
            gx = _first_not_none(p, 'pred_x', 'predicted_x', 'pred_x_px')
            gy = _first_not_none(p, 'pred_y', 'predicted_y', 'pred_y_px')
            tx = _first_not_none(g, 'centroid_x', 'centroid_x_px')
            ty = _first_not_none(g, 'centroid_y', 'centroid_y_px')

        if gx is None or gy is None or tx is None or ty is None:
            continue
        pred_x.append(float(gx))
        pred_y.append(float(gy))
        targ_x.append(float(tx))
        targ_y.append(float(ty))

    if len(pred_x) == 0:
        return {'count': 0}

    pred = np.vstack([pred_x, pred_y]).T
    targ = np.vstack([targ_x, targ_y]).T
    dif = pred - targ
    dists = np.linalg.norm(dif, axis=1)

    metrics = {
        'count': int(len(dists)),
        'rmse_pixels': float(math.sqrt(float(np.mean(dists ** 2)))),
        'mean_pixels': float(np.mean(dists)),
        'median_pixels': float(np.median(dists)),
        'std_pixels': float(np.std(dists)),
        'p95_pixels': float(np.percentile(dists, 95)),
    }
    return metrics


def main():
    parser = argparse.ArgumentParser(description='Validate session logs and compute gaze-to-plate residuals')
    parser.add_argument('--session-log', required=True, help='CSV session log (may contain both gaze and plate columns)')
    parser.add_argument('--gaze-log', default=None, help='Optional separate gaze CSV to join with session-log')
    parser.add_argument('--out', default='validation_report.json', help='JSON report output path')
    parser.add_argument('--residuals-csv', default='residuals.csv', help='CSV of per-sample residuals')
    parser.add_argument('--max-dt', type=float, default=0.05, help='Max timestamp difference (s) when joining logs')
    parser.add_argument('--plot', default=None, help='Optional output PNG for scatter and histogram (requires matplotlib)')
    args = parser.parse_args()

    if not os.path.exists(args.session_log):
        raise SystemExit(f"session log not found: {args.session_log}")

    sess_rows = read_csv_rows(args.session_log)

    if args.gaze_log:
        if not os.path.exists(args.gaze_log):
            raise SystemExit(f"gaze log not found: {args.gaze_log}")
        gaze_rows = read_csv_rows(args.gaze_log)
        pairs = join_on_timestamp(gaze_rows, sess_rows, max_dt=args.max_dt)
    else:
        # Expect session CSV to contain both prediction and plate centroid columns in same row.
        # Accept either 'plate_found' or 'found' because different writers use different names.
        pairs = []
        for r in sess_rows:
            plate_found = r.get('plate_found')
            if plate_found is None:
                plate_found = r.get('found')
            if is_truthy(plate_found):
                # we treat same row as both
                pairs.append((r, r))

    metrics = compute_metrics(pairs)

    # write report
    with open(args.out, 'w') as f:
        json.dump({'input': {'session_log': args.session_log, 'gaze_log': args.gaze_log}, 'metrics': metrics}, f, indent=2)

    print('Validation complete. Metrics:')
    print(json.dumps(metrics, indent=2))

    # write residuals CSV if possible
    try:
        import matplotlib.pyplot as plt
        have_matplotlib = True
    except Exception:
        have_matplotlib = False

    # save residuals list
    residuals = []
    for g, p in pairs:
        if 'pred_x' in g or 'predicted_x' in g or 'pred_x_px' in g:
            gx = _first_not_none(g, 'pred_x', 'predicted_x', 'pred_x_px')
            gy = _first_not_none(g, 'pred_y', 'predicted_y', 'pred_y_px')
            tx = _first_not_none(p, 'centroid_x', 'centroid_x_px')
            ty = _first_not_none(p, 'centroid_y', 'centroid_y_px')
        else:
            gx = _first_not_none(p, 'pred_x', 'predicted_x', 'pred_x_px')
            gy = _first_not_none(p, 'pred_y', 'predicted_y', 'pred_y_px')
            tx = _first_not_none(g, 'centroid_x', 'centroid_x_px')
            ty = _first_not_none(g, 'centroid_y', 'centroid_y_px')
        if gx is None or gy is None or tx is None or ty is None:
            continue
        dx = float(gx) - float(tx)
        dy = float(gy) - float(ty)
        dist = math.hypot(dx, dy)
        residuals.append({'timestamp': g.get('timestamp') or p.get('timestamp'), 'pred_x': float(gx), 'pred_y': float(gy), 'targ_x': float(tx), 'targ_y': float(ty), 'dx': dx, 'dy': dy, 'dist': dist})

    if residuals:
        keys = ['timestamp', 'pred_x', 'pred_y', 'targ_x', 'targ_y', 'dx', 'dy', 'dist']
        with open(args.residuals_csv, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(keys)
            for r in residuals:
                writer.writerow([r[k] for k in keys])

        if args.plot and have_matplotlib:
            xs = [r['targ_x'] for r in residuals]
            ys = [r['targ_y'] for r in residuals]
            px = [r['pred_x'] for r in residuals]
            py = [r['pred_y'] for r in residuals]
            d = [r['dist'] for r in residuals]

            fig, axes = plt.subplots(1, 2, figsize=(10, 5))
            axes[0].scatter(xs, ys, c='blue', s=10, label='target')
            axes[0].scatter(px, py, c='red', s=10, label='pred')
            axes[0].set_title('Target vs Predicted (pixels)')
            axes[0].legend()

            axes[1].hist(d, bins=40, color='gray')
            axes[1].set_title('Residual distances (pixels)')

            plt.tight_layout()
            plt.savefig(args.plot)


if __name__ == '__main__':
    main()
