# Eye Tracking Obsequious - Setup Guide

## Environment Setup

This project requires Python 3.8+ and uses a virtual environment to manage dependencies.

### Quick Start

#### Windows
```bash
setup_venv.bat
```

#### macOS / Linux
```bash
chmod +x setup_venv.sh
./setup_venv.sh
```

### Manual Setup

If you prefer to set up manually:

#### Windows
```bash
python -m venv venv
venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
```

#### macOS / Linux
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Activating the Virtual Environment

#### Windows
```bash
venv\Scripts\activate.bat
```

#### macOS / Linux
```bash
source venv/bin/activate
```

### Deactivating the Virtual Environment
```bash
deactivate
```

## Running Applications

Make sure the virtual environment is activated before running any scripts.

### Configuring stream URLs, profile, etc. via `.env`

Instead of retyping `--left-source`, `--right-source`, `--profile`, etc. on every run,
copy `.env.example` to `.env` (gitignored) at the repo root and fill in your values:

```bash
cp .env.example .env
```

Every script below loads `.env` automatically and uses it to fill in the matching flag's
default — any CLI flag you do pass still overrides the `.env` value.

## Flashing the ESP32s

The glasses use two ESP32-S3 boards: one acts as a **Wi-Fi Access Point (AP)**, the other
as a **Station (STA)** that joins the AP's network. Both run the same firmware project;
only the Wi-Fi role lines differ.

### Setting your own Wi-Fi credentials

Each firmware target ships with a placeholder Wi-Fi password (`changeme123`) in
`sdkconfig.defaults` — do not deploy real hardware with it left in place. Rather than
editing the checked-in defaults, copy the local override template and fill in your own
network name/password:

```bash
cd firmware/esp32s3_forward_camera   # or firmware/esp32_eye_camera
cp sdkconfig.local.example sdkconfig.local
# edit sdkconfig.local with your real SSID/password
```

`sdkconfig.local` is gitignored and picked up automatically by `CMakeLists.txt` on top
of `sdkconfig.defaults` — no need to run `menuconfig` or touch tracked files.

### Step 1 — Flash the AP unit (forward/right camera)

In `firmware/esp32s3_forward_camera/sdkconfig.local`, set:

```
CONFIG_WIFI_ROLE_AP=y
CONFIG_AP_SSID="YourNetworkName"
CONFIG_AP_PASSWORD="YourPassword"
```

Then build and flash:

```bash
cd firmware/esp32s3_forward_camera
idf.py build flash monitor
```

On boot the AP unit logs: `AP started — SSID: "YourNetworkName" | IP: 192.168.4.1`

### Step 2 — Flash the STA unit (eye/left camera)

In `firmware/esp32_eye_camera/sdkconfig.local`, set:

```
CONFIG_WIFI_ROLE_STA=y
CONFIG_WIFI_SSID="YourNetworkName"
CONFIG_WIFI_PASSWORD="YourPassword"
```

Build and flash the second board. On boot it connects to the AP and logs its assigned IP
(e.g. `Connected. Open http://192.168.4.2`).

> **Tip:** delete the generated `sdkconfig` file between the two flashes so the new defaults
> are picked up cleanly: `idf.py fullclean` or just `rm sdkconfig`.

---

## Running the Exam Software

### Connect the PC

1. On the PC, join the Wi-Fi network you set in `sdkconfig.local` above.
2. Note the IPs from the ESP32 serial logs (AP is always `192.168.4.1`; STA gets an address
   in the `192.168.4.x` range, typically `192.168.4.2`). Put these into `EYE_STREAM_URL` /
   `FORWARD_STREAM_URL` in your `.env` so you don't have to pass them as flags every time.

### Start the web server

```bash
cd postprocessing
python dual_stream_web.py \
  --plate-template path/to/template.jpg
# --left-source/--right-source/--profile/--port/--output-dir come from .env if set,
# or pass them explicitly, e.g. --left-source http://192.168.4.2/stream
```

Then open **http://localhost:5000** in any browser on that PC.

### Available options

```bash
python dual_stream_web.py \
  --left-source  http://192.168.4.2/stream \
  --right-source http://192.168.4.1/stream \
  --plate-template path/to/template.jpg \
  --profile child          # or 'adult'
  --calibration-file cal.json
  --denoise
  --port 5000
  --output-dir ./recordings
```

## Dependencies

See `requirements.txt` for the full list of dependencies:

- **opencv-python**: Computer vision library
- **numpy**: Numerical computing
- **requests**: HTTP library for streaming
- **python-dotenv**: Loads `.env` for default stream URLs/ports/profile
- **Flask**: Web framework for the web viewer

## Directory Structure

```
eye-tracking-obiquos/
├── requirements.txt          # All project dependencies
├── .env.example              # Template for stream URLs/profile/port (copy to .env)
├── setup_venv.bat           # Windows setup script
├── setup_venv.sh            # Unix/Linux setup script
├── postprocessing/
│   ├── dual_stream_web.py   # Web-based dual stream viewer
│   ├── dual_stream.html     # HTML template for web viewer
│   ├── recordings/          # Recorded video clips (auto-created)
│   ├── mjpeg_stream.py      # MJPEG stream reader
│   ├── plate_tracker.py     # License plate detection
│   ├── eye_forward_alignment.py
│   └── validate_session.py
└── firmware/                # ESP32 firmware
    ├── esp32_eye_camera/
    │   └── sdkconfig.local.example  # Template for Wi-Fi creds (copy to sdkconfig.local)
    └── esp32s3_forward_camera/
        └── sdkconfig.local.example  # Template for Wi-Fi creds (copy to sdkconfig.local)
```

## Troubleshooting

### "No module named 'cv2'"
Make sure the virtual environment is activated and dependencies are installed:
```bash
pip install -r requirements.txt
```

### Connection Issues to ESP32
- Verify ESP32 IP address: Open `http://<ip>/` in browser
- Check network connectivity: `ping 192.168.1.15`
- Ensure no other application is holding the stream

### Permission Denied on setup script (Unix)
```bash
chmod +x setup_venv.sh
./setup_venv.sh
```

## Notes

- The virtual environment folder (`venv/`) is not included in version control
- Always activate the venv before running scripts
- Use `pip freeze > requirements.txt` to update dependencies after adding new packages
