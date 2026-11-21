#Requires -Version 5.1
# ATRI runtime status check (read-only). Entry: root 检查亚托莉状态.bat
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

Write-Host ''
Write-Host '  亚托莉运行状态' -ForegroundColor Cyan
Write-Host '  =============================' -ForegroundColor Cyan

$listen = $false
$established = $false
$lines = netstat -ano | Where-Object { $_ -match ':8765\s' }
foreach ($line in $lines) {
    if ($line -match 'LISTENING')   { $listen = $true }
    if ($line -match 'ESTABLISHED') { $established = $true }
}

if ($listen) {
    Write-Host '  ATRI 服务：运行中' -ForegroundColor Green
} else {
    Write-Host '  ATRI 服务：未运行' -ForegroundColor Red
}
if ($established) {
    Write-Host '  NapCat 连接：已连接' -ForegroundColor Green
} else {
    Write-Host '  NapCat 连接：未连接' -ForegroundColor Red
}

# 守卫进程与开机自启任务状态：守卫死了 ATRI 崩溃就没人拉、NapCat 掉线
# 没人恢复，必须能一眼看到（2026-11-21 实测守卫曾被关闭的控制台带走）。
$guard = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'watchdog\.ps1' -and $_.CommandLine -notmatch '-Stop' }
if ($guard) {
    Write-Host '  守卫进程：运行中' -ForegroundColor Green
} else {
    Write-Host '  守卫进程：未运行' -ForegroundColor Red
}

$task = Get-ScheduledTask -TaskName 'ATRI Watchdog' -ErrorAction SilentlyContinue
if ($task) {
    Write-Host '  开机自启：已注册' -ForegroundColor Green
} else {
    Write-Host '  开机自启：未注册' -ForegroundColor Yellow
}

Write-Host ''
if (-not $listen) {
    Write-Host '  ATRI 未运行。运行 tools\launch\start_atri.bat 启动（含守卫）；'
    Write-Host '  守卫未运行时同样执行上面的启动，或重启电脑由自启任务拉起。'
} elseif (-not $established) {
    Write-Host '  NapCat 未连上。守卫会每 5 分钟自动重试；也可确认 QQ 是否需要'
    Write-Host '  扫码登录（NapCat WebUI：http://127.0.0.1:6099）。'
} elseif (-not $guard) {
    Write-Host '  核心链路正常，但守卫不在。运行 tools\launch\start_atri.bat 补上守卫。'
} else {
    Write-Host '  状态正常。可以用另一个 QQ 号给机器人发消息测试。'
}
Write-Host ''
