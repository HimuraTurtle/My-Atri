$ErrorActionPreference = "Stop"

# 开机自启注册：计划任务（登录触发）替代启动文件夹快捷方式。
# 2026-11 实测启动文件夹 .lnk 方案不可靠（重启后未生效，守卫进程消失），
# 且旧方案只拉 ATRI、不拉 NapCat/QQ——开机后 bot 在 QQ 上仍不在线。
# 计划任务方案：
#   - 触发器：当前用户登录后延迟 30 秒（等网络与系统就绪）
#   - 动作：watchdog.ps1 -Guard（现已全栈监护：ATRI 进程 + NapCat 连接）
#   - ExecutionTimeLimit 用 PT0S（无限期）——默认 72 小时会把常驻守卫杀掉；
#     New-ScheduledTaskSettingsSet 的 [TimeSpan]::Zero 会序列化成
#     "00:00:00" 被任务 XML 拒绝，所以走 -Xml 直接注册
#   - 互斥锁保证不会双开；旧 .lnk 一并清理避免混淆
# 幂等：重复运行会用 -Force 覆盖注册。
# 注意：本文件包含中文，必须保存为带 BOM 的 UTF-8（PowerShell 5.1 无
# BOM 时按 GBK 解析会语法错乱）。

$watchdog = Join-Path $PSScriptRoot "watchdog.ps1"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskName = "ATRI Watchdog"
$fullUser = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "watchdog.ps1 not found: $watchdog"
}

# 清理旧方案：启动文件夹快捷方式（互斥锁其实能防双开，清掉是为了
# 不留两份让人困惑的自启入口）
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
foreach ($legacy in @("ATRI Watchdog.lnk", "ATRI Bot.lnk")) {
    $lnkPath = Join-Path $startupDir $legacy
    if (Test-Path -LiteralPath $lnkPath) {
        Remove-Item -LiteralPath $lnkPath -Force
        Write-Output "removed legacy startup shortcut: $lnkPath"
    }
}

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ATRI full-stack guard: start ATRI + NapCat/QQ after logon and keep supervising.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$fullUser</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$fullUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowHardTerminate>false</AllowHardTerminate>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "$watchdog" -Guard</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $taskName -Xml $taskXml -User $env:USERNAME -Force | Out-Null

Write-Output "registered scheduled task: $taskName"
Write-Output "trigger : $fullUser logon + 30s delay, no execution time limit"
Write-Output "action  : powershell -File watchdog.ps1 -Guard"
