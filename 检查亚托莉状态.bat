@echo off
chcp 65001 >nul
title ATRI Status
rem Read-only runtime status check. Real logic (Chinese UI): tools\check-status.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check-status.ps1"
pause
