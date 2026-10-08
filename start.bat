@echo off
title ACADES CBV Dashboard
cd /d "%~dp0"
echo Starting ACADES CBV Dashboard... open http://127.0.0.1:8078 in your browser
start "" http://127.0.0.1:8078
call python -m uvicorn app.main:app --port 8078 --reload
pause
