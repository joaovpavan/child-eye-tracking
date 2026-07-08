# ESP32-S3 Forward Camera Firmware

This firmware target is for the **forward-facing ESP32-S3 camera** node.

## Build and Flash (ESP-IDF)
1. Open this folder as the project root: irmware/esp32s3_forward_camera
2. Run:
   - idf.py set-target esp32s3
   - idf.py build
   - idf.py -p <PORT> flash monitor

## Notes
- Verify camera pin mapping in main/camera_pinout.h for the forward camera hardware.
- Wi-Fi SSID/password default to placeholders (`main/Kconfig.projbuild`). Before
  flashing real hardware, copy `sdkconfig.local.example` to `sdkconfig.local`
  (gitignored) and set your own values there — it's merged automatically on
  top of `sdkconfig.defaults`. `idf.py menuconfig` also works if you prefer that.
