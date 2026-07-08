# Eye Tracking Obiquos

A DIY eye-tracking glasses project: two ESP32 cameras (one eye-facing, one
forward-facing) stream video over Wi-Fi to a PC, where the postprocessing
pipeline estimates gaze direction and maps it onto the forward-facing view.

## Repository layout

```
firmware/
├── esp32_eye_camera/        ESP-IDF firmware for the eye-facing camera (STA)
└── esp32s3_forward_camera/  ESP-IDF firmware for the forward-facing camera (AP)

postprocessing/
├── dual_stream_viewer.py    Side-by-side viewer for both MJPEG streams
├── dual_stream_web.py       Web-based viewer/recorder (Flask)
├── eye_forward_alignment.py Gaze estimation + calibration + overlay
├── plate_tracker.py         License-plate/target tracking
├── mjpeg_stream.py          Shared MJPEG stream reader
└── validate_session.py      Session/recording validation
```

See [`firmware/esp32_eye_camera/README.md`](firmware/esp32_eye_camera/README.md) and
[`firmware/esp32s3_forward_camera/README.md`](firmware/esp32s3_forward_camera/README.md)
for building and flashing each board, and
[`postprocessing/README.md`](postprocessing/README.md) for the PC-side pipeline.

## Getting started

See [SETUP.md](SETUP.md) for the full environment setup, flashing, and
run instructions.

Quick version:

```bash
# Python side
setup_venv.bat                     # or: python -m venv venv && pip install -r requirements.txt
cp .env.example .env               # then fill in your stream URLs/profile/port

# Firmware side (per board, from its own firmware/ subfolder)
cp sdkconfig.local.example sdkconfig.local   # then fill in your Wi-Fi SSID/password
idf.py build flash monitor
```

**Before flashing real hardware**, change the default Wi-Fi credentials —
`main/Kconfig.projbuild` in each firmware target ships with a placeholder
password (`changeme123`) that must not be left in place on deployed devices.
Set your own via `sdkconfig.local` (gitignored, merged automatically) rather
than editing the checked-in `sdkconfig.defaults`.

## Dependencies

See [`requirements.txt`](requirements.txt) for the full list.

## License

MIT — see [LICENSE](LICENSE). This covers the code in this repository; it does
not relicense third-party dependencies (e.g. ESP-IDF managed components), which
retain their own licenses.
