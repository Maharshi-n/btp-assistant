@echo off
cd /d "%~dp0"
start "ngrok" cmd /k "ngrok http 8000"
call .venv\Scripts\activate
python run.py
pause
