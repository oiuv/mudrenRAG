@echo off
title MyRAG API Server
echo Starting FastAPI server on http://0.0.0.0:8008
echo Press CTRL+C to quit.

uvicorn app.main:app --host 0.0.0.0 --port 8008

pause
