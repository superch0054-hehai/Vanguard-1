@echo off
REM Thin wrapper: the real pusher lives in dh_push.ps1 (same folder).
REM Keep this file name/path stable -- the scheduled task points here.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dh_push.ps1"
exit /b 0
