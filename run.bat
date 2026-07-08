@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ===============================================
echo  Eye Tracking Obiquos - Setup and Launch
echo ===============================================
echo.

REM --- 1. Locate Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.8+ from https://www.python.org/downloads/, make sure
    echo "Add python.exe to PATH" is checked during install, then re-run this script.
    pause
    exit /b 1
)

REM --- 2. Create the virtual environment if missing ---
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

REM --- 3. Install dependencies ---
echo Installing dependencies ^(this can take a minute the first time^)...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. See the output above.
    pause
    exit /b 1
)

REM --- 4. Create .env from the template if missing ---
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
)

REM --- 5. Load .env into this script's environment ---
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
)

REM --- 6. Make sure the user is on the glasses' Wi-Fi network ---
echo.
echo Before continuing, connect this PC's Wi-Fi to the glasses' network
echo ^(check the SSID/password you were given for this device^).
pause

REM --- 7. Launch the web viewer and open it in the browser ---
if not defined WEB_PORT set "WEB_PORT=5000"
echo.
echo Starting the dual stream viewer on port !WEB_PORT! ...
start "Eye Tracking Server (close this window to stop)" venv\Scripts\python.exe postprocessing\dual_stream_web.py
timeout /t 4 /nobreak >nul
start "" "http://localhost:!WEB_PORT!"

echo.
echo The viewer is opening in your browser at http://localhost:!WEB_PORT!
echo The server is running in the other window - close it when you're done.
pause
