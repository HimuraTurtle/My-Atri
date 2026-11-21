@echo off
rem Start ATRI guard (auto restarts ATRI if it exits).
rem 不用 start /B：/B 会把守卫挂进本 bat 的控制台，窗口一关守卫就收到
rem CTRL_CLOSE 陪葬（与计划任务旧动作同源死因）。独立隐藏窗口的
rem powershell 进程不受父窗口关闭影响；停止走 stop_atri.bat。
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0watchdog.ps1" -Guard
