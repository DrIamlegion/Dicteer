@echo off
rem Visible-console launcher: shows live status and transcription output.
cd /d "%~dp0"
".venv\Scripts\python.exe" dictate.py %*
if errorlevel 1 pause
