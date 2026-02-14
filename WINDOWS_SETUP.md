# Windows Setup Guide

Complete guide for setting up the Media Transcription Service on Windows.

## Prerequisites

- Windows 10 or 11
- Python 3.11 or higher
- Git (optional, for cloning)

## Step 1: Install Python

1. Download Python from https://www.python.org/downloads/
2. Run the installer
3. **Important:** Check "Add Python to PATH"
4. Click "Install Now"

Verify installation:
```cmd
python --version
```

## Step 2: Install ffmpeg (Required)

ffmpeg is essential for processing video files and handling large audio files efficiently.

### Option A: Using Chocolatey (Recommended)

1. Install Chocolatey (if not already installed):
   - Open PowerShell as Administrator
   - Run:
   ```powershell
   Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
   ```

2. Install ffmpeg:
   ```powershell
   choco install ffmpeg
   ```

3. Verify installation:
   ```cmd
   ffmpeg -version
   ffprobe -version
   ```

### Option B: Manual Installation

1. Download ffmpeg from https://www.gyan.dev/ffmpeg/builds/
   - Choose "ffmpeg-release-essentials.zip"

2. Extract the ZIP file to `C:\ffmpeg`

3. Add to PATH:
   - Open "Environment Variables" (search in Start menu)
   - Under "System variables", find "Path"
   - Click "Edit"
   - Click "New"
   - Add: `C:\ffmpeg\bin`
   - Click "OK" on all dialogs

4. **Restart your terminal/PowerShell**

5. Verify installation:
   ```cmd
   ffmpeg -version
   ffprobe -version
   ```

### Option C: Using Scoop

1. Install Scoop (if not already installed):
   ```powershell
   Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
   irm get.scoop.sh | iex
   ```

2. Install ffmpeg:
   ```powershell
   scoop install ffmpeg
   ```

3. Verify installation:
   ```cmd
   ffmpeg -version
   ffprobe -version
   ```

## Step 3: Clone or Download the Project

### Using Git:
```cmd
git clone https://github.com/yourusername/media-transcription-service.git
cd media-transcription-service
```

### Or download ZIP:
1. Download the project ZIP
2. Extract to a folder
3. Open terminal in that folder

## Step 4: Create Virtual Environment

```cmd
python -m venv .venv
```

Activate it:
```cmd
.venv\Scripts\activate
```

You should see `(.venv)` in your prompt.

## Step 5: Install Dependencies

```cmd
pip install -r requirements.txt
```

This will install:
- FastAPI
- OpenAI SDK
- B2 SDK
- pydub
- And other dependencies

## Step 6: Configure Environment Variables

1. Copy the example file:
   ```cmd
   copy .env.example .env
   ```

2. Edit `.env` with Notepad or your favorite editor:
   ```cmd
   notepad .env
   ```

3. Fill in your credentials:
   ```
   API_KEY=your-secret-api-key-here
   OPENAI_API_KEY=sk-your-openai-key-here
   B2_KEY_ID=your-b2-key-id-here
   B2_APPLICATION_KEY=your-b2-app-key-here
   ```

4. Save and close

## Step 7: Run the Service

```cmd
python main.py
```

You should see:
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

## Step 8: Test the Service

Open a new terminal and test:

```cmd
curl http://localhost:8000/health
```

Should return:
```json
{"status":"healthy"}
```

## Step 9: View API Documentation

Open your browser and go to:
```
http://localhost:8000/docs
```

You'll see the interactive API documentation.

## Common Windows Issues

### Issue: "python is not recognized"

**Solution:** Python not in PATH
1. Reinstall Python with "Add to PATH" checked
2. Or manually add Python to PATH

### Issue: "ffmpeg is not recognized"

**Solution:** ffmpeg not in PATH
1. Verify ffmpeg is installed: Check `C:\ffmpeg\bin\ffmpeg.exe` exists
2. Add to PATH (see Option B above)
3. **Restart your terminal**
4. Test: `ffmpeg -version`

### Issue: "No module named 'services'"

**Solution:** Not in correct directory or venv not activated
1. Make sure you're in the project directory
2. Activate virtual environment: `.venv\Scripts\activate`
3. Install dependencies: `pip install -r requirements.txt`

### Issue: Port 8000 already in use

**Solution:** Another service using port 8000
1. Find and stop the other service
2. Or change port in main.py:
   ```python
   uvicorn.run(app, host="0.0.0.0", port=8001)
   ```

### Issue: "Access denied" when installing

**Solution:** Run as Administrator
1. Right-click PowerShell/CMD
2. Choose "Run as Administrator"
3. Try installation again

### Issue: Slow processing without ffmpeg

**Symptom:** Jobs take very long, high memory usage

**Solution:** Install ffmpeg (see Step 2)
- Without ffmpeg: Uses pydub (slow, memory-intensive)
- With ffmpeg: 10-100x faster, uses less memory

## Performance Notes

### With ffmpeg (Recommended):
- ✅ Fast audio extraction
- ✅ Efficient chunking
- ✅ Low memory usage
- ✅ Handles large files (3.5GB+)

### Without ffmpeg (Fallback):
- ⚠️ Slow audio extraction (10x slower)
- ⚠️ Memory-intensive chunking
- ⚠️ May crash on large files
- ⚠️ Not recommended for production

## Running as a Service

### Option 1: Using NSSM (Non-Sucking Service Manager)

1. Download NSSM from https://nssm.cc/download

2. Extract and open terminal in NSSM folder

3. Install service:
   ```cmd
   nssm install TranscriptionService "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\main.py"
   ```

4. Configure service:
   ```cmd
   nssm set TranscriptionService AppDirectory "C:\path\to\project"
   nssm set TranscriptionService DisplayName "Media Transcription Service"
   nssm set TranscriptionService Description "Transcribes media files using OpenAI"
   ```

5. Start service:
   ```cmd
   nssm start TranscriptionService
   ```

### Option 2: Using Task Scheduler

1. Open Task Scheduler
2. Create Basic Task
3. Name: "Media Transcription Service"
4. Trigger: "When the computer starts"
5. Action: "Start a program"
6. Program: `C:\path\to\.venv\Scripts\python.exe`
7. Arguments: `C:\path\to\main.py`
8. Start in: `C:\path\to\project`

## Development Tips

### Use PowerShell (Better than CMD)

PowerShell has better features:
```powershell
# Activate venv
.\.venv\Scripts\Activate.ps1

# Run service
python main.py

# View logs with colors
python main.py | Out-Host
```

### Use Windows Terminal

Download from Microsoft Store for better experience:
- Multiple tabs
- Better colors
- Copy/paste support

### Use VS Code

Recommended IDE for development:
1. Download from https://code.visualstudio.com/
2. Install Python extension
3. Open project folder
4. Select Python interpreter (`.venv`)
5. Run/debug from IDE

## Firewall Configuration

If you need to access from other machines:

1. Open Windows Defender Firewall
2. Click "Advanced settings"
3. Click "Inbound Rules"
4. Click "New Rule"
5. Choose "Port"
6. TCP port 8000
7. Allow the connection
8. Apply to all profiles
9. Name: "Media Transcription Service"

## Updating the Service

```cmd
# Activate venv
.venv\Scripts\activate

# Pull latest changes (if using git)
git pull

# Update dependencies
pip install -r requirements.txt --upgrade

# Restart service
# Press Ctrl+C to stop, then run again
python main.py
```

## Troubleshooting

### Check Python Version
```cmd
python --version
```
Should be 3.11 or higher

### Check ffmpeg Installation
```cmd
ffmpeg -version
ffprobe -version
```
Both should show version info

### Check Dependencies
```cmd
pip list
```
Should show all required packages

### Check Environment Variables
```cmd
type .env
```
Should show your configuration (without exposing in logs)

### View Detailed Logs
```cmd
# Set log level to DEBUG
set LOG_LEVEL=DEBUG
python main.py
```

### Test Individual Components

**Test B2 Connection:**
```python
from services.b2_client import B2Client
import asyncio

client = B2Client("your_key_id", "your_app_key")
# Try listing buckets
```

**Test OpenAI:**
```python
from openai import OpenAI
client = OpenAI(api_key="your_key")
# Try a simple request
```

## Next Steps

1. ✅ Service running locally
2. ✅ ffmpeg installed
3. ✅ Test with small file
4. ✅ Test with larger file
5. ✅ Deploy to Render (see DEPLOYMENT.md)

## Getting Help

- Check `TROUBLESHOOTING.md` for common issues
- Check `README.md` for full documentation
- Check `API_EXAMPLES.md` for code examples
- Visit `/docs` endpoint for API reference

## Windows-Specific Resources

- ffmpeg Windows builds: https://www.gyan.dev/ffmpeg/builds/
- Python Windows installer: https://www.python.org/downloads/windows/
- Windows Terminal: https://aka.ms/terminal
- VS Code: https://code.visualstudio.com/
- Git for Windows: https://git-scm.com/download/win
