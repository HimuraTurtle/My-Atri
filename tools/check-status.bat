@echo off
chcp 65001 >nul
setlocal

set "LISTEN="
set "ESTAB="

for /f "tokens=4" %%S in ('netstat -ano ^| findstr /C:":8765"') do (
    if /I "%%S"=="LISTENING" set "LISTEN=1"
    if /I "%%S"=="ESTABLISHED" set "ESTAB=1"
)

if defined LISTEN (
    echo Atri service: running
) else (
    echo Atri service: not running
)

if defined ESTAB (
    echo NapCat connection: connected
) else (
    echo NapCat connection: not connected
)

echo.

if defined LISTEN (
    if defined ESTAB (
        echo Status OK. Use another QQ account to message your bot QQ.
    ) else (
        echo NapCat not connected. Check NapCat process or WebUI login.
    )
) else (
    echo ATRI is not running. Double-click tools\launch\start_atri.bat to start.
)

endlocal
