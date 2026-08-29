@echo off
cd /d "%~dp0"
start "Sampada API" /min ".venv\Scripts\python.exe" -m uvicorn backend_api:app --host 127.0.0.1 --port 8000
start "Sampada React" /min cmd /c "cd frontend && npm run dev"
timeout /t 3 /nobreak >nul
start http://127.0.0.1:5173

