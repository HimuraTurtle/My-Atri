@echo off
chcp 65001 >nul
title ATRI Installer
rem One-click installer entry. Real logic (Chinese UI): tools\install\install.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install\install.ps1"
pause
