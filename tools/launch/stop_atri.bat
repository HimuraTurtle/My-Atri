@echo off
rem Stop ATRI and its guard (for maintenance). Run start_atri.bat to start again.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1" -Stop
