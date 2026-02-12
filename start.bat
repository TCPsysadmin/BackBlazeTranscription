@echo off
REM Media Transcription Service - Start Script (Windows)

echo Starting Media Transcription Service...

REM Check if .env exists
if not exist .env (
    echo Error: .env file not found!
    echo Please copy .env.example to .env and configure your API keys.
    exit /b 1
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

REM Start the service
echo Starting service on http://localhost:8000
python main.py
