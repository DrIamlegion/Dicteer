@echo off
rem Double-clickable build: app folder + Windows installer.
rem Switches are passed through, e.g.  build-installer.cmd -SkipTests
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-installer.ps1" %*
if errorlevel 1 pause
