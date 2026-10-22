@echo off
chcp 65001 >nul
title Stop ATRI
rem Stop ATRI core and its watchdog guard. Real logic: tools\launch\watchdog.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch\watchdog.ps1" -Stop
pause
