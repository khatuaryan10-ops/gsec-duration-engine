@echo off
rem Start the G-Sec Duration Engine (API + web app) and open it in the browser.
rem Works from anywhere: it switches to this file's folder first.
cd /d "%~dp0"
echo Starting G-Sec Duration Engine at http://127.0.0.1:8000/  (close this window or press Ctrl+C to stop)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/"
python -m uvicorn gsec_engine.api:app --reload --port 8000
pause
