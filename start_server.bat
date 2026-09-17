@echo off
cd /d "%~dp0"
title mudrenRAG API Server
echo Starting FastAPI server on http://0.0.0.0:8008
echo Press CTRL+C to quit.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8008
) else (
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8008
)
pause
