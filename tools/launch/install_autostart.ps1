$ErrorActionPreference = "Stop"

# 看门狗常驻脚本（登录自启 + 每 5 分钟巡检拉起）
$watchdog = Join-Path $PSScriptRoot "watchdog.ps1"

$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$lnkPath = Join-Path $startupDir "ATRI Watchdog.lnk"
$oldLnk = Join-Path $startupDir "ATRI Bot.lnk"
if (Test-Path $oldLnk) { Remove-Item $oldLnk -Force }

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`" -Guard"
$lnk.WorkingDirectory = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$lnk.Description = "ATRI guard: auto start + keep alive (event driven)"
$lnk.WindowStyle = 7
$lnk.Save()

Write-Output "created: $lnkPath"
Write-Output "target : $($lnk.TargetPath) $($lnk.Arguments)"
if (Test-Path $oldLnk) { Write-Output "removed old: $oldLnk" }
