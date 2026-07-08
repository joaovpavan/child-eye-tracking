@echo off
REM Setup virtual environment on Windows

if exist venv (
    echo Virtual environment already exists.
    echo Activating existing environment...
) else (
    echo Creating virtual environment...
    python -m venv venv
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies from requirements.txt...
pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Setup complete!
echo.
echo To activate the environment in the future, run:
echo   venv\Scripts\activate.bat
echo.
echo To deactivate, run:
echo   deactivate
echo.
pause
