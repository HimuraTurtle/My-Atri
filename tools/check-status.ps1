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

Write-Host ''
if ($listen -and $established) {
    Write-Host '  状态正常。可以用另一个 QQ 号给机器人发消息测试。'
} elseif ($listen) {
    Write-Host '  NapCat 未连上。请确认 QQ 已扫码登录（NapCat WebUI：http://127.0.0.1:6099）。'
} else {
    Write-Host '  ATRI 未运行。双击 tools\launch\qq_legacy\start-all.bat 启动。'
}
Write-Host ''
