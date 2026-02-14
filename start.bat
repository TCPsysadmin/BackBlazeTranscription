@echo off
REM Media Transcription Service - Start Script (Windows)

echo Starting Media Transcription Service...
echo.

REM Check if .env exists
if not exist .env (
    echo Error: .env file not found!
    echo Please copy .env.example to .env and configure your API keys.
    pause
    exit /b 1
)

REM Check if ffmpeg is installed
where ffmpeg >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: ffmpeg is not installed or not in PATH!
    echo.
    echo The service will work but will be MUCH slower and use more memory.
    echo For large files, ffmpeg is REQUIRED.
    echo.
    echo Please install ffmpeg:
    echo   1. Using Chocolatey: choco install ffmpeg
    echo   2. Manual: See WINDOWS_SETUP.md for instructions
    echo.
    echo Press any key to continue anyway, or Ctrl+C to exit and install ffmpeg...
    pause
)

REM Check if virtual environment exists
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo ============================================
echo Service starting on http://localhost:8000
echo API Docs: http://localhost:8000/docs
echo Press Ctrl+C to stop
echo ============================================
echo.

REM Start the service
python main.py
