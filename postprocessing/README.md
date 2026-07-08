# Postprocessing

This folder is for the PC-side video processing pipeline.

The shared MJPEG reader lives in `mjpeg_stream.py` and is reused by the viewers.

Current first step:
- `dual_stream_viewer.py` opens two ESP32 MJPEG feeds and shows them side by side in one window.
- `eye_forward_alignment.py` estimates an eye angle from the eye-facing stream and remaps it to the forward stream using the glasses camera offset.
- `eye_forward_alignment.py` also supports multi-point calibration, persistent fit parameters, and an on-screen pupil debug view.

Run example:

```bash
python dual_stream_viewer.py --left-url http://192.168.1.6/stream --right-url http://192.168.1.7/stream
```

Alignment example:

```bash
python eye_forward_alignment.py --eye-url http://192.168.1.6/stream --forward-url http://192.168.1.7/stream
```

Useful keys while the alignment viewer is running:

- `c`: start a multi-point calibration pass and save the fitted mapping to a JSON file.
- `r`: reset the calibration back to the default model.
- `q`: quit.

The calibration file is saved next to the script by default as `eye_forward_alignment_calibration.json`.
