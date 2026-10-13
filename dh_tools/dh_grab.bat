@echo off
REM Thin wrapper: the real grabber lives in dh_grab.ps1 (same folder).
REM Keep this file name/path stable -- the scheduled task points here.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dh_grab.ps1"
exit /b 0
