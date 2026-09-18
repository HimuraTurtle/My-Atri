@echo off
rem Start ATRI guard (auto restarts ATRI if it exits).
start "" /B powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\BOT-ATRI\BOT-ATRI-main\tools\launch\watchdog.ps1" -Guard
