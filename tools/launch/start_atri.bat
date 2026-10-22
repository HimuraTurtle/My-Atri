@echo off
rem Start ATRI guard (auto restarts ATRI if it exits).
start "" /B powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1" -Guard
