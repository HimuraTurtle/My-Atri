$ErrorActionPreference = "SilentlyContinue"

# ATRI 守卫：同步等待 ATRI 进程退出，退出后立即重启（事件驱动，无轮询）。
# 用法：watchdog.ps1           单次检查（ATRI 不在则拉起，不驻留）
#       watchdog.ps1 -Guard    守卫模式（登录自启动/日常启动使用；秒级重启；互斥单实例）
#       watchdog.ps1 -Stop     停止守卫与 ATRI（维护用；start_atri.bat 可再次启动）

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$guardLog = Join-Path $root "logs\watchdog.log"

function Test-AtriAlive {
    # 命令行匹配是最准判据：能区分 ATRI（-m atri_qq_bot）与同为 pythonw
    # 的 NapCat 分片（hidden_launcher napcat）。不能用日志新鲜度——刚死
    # 的进程会留下新鲜日志，误判为存活并短路立即重启。
    $atri = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'atri_qq_bot' }
    if ($atri) { return $true }
    $listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($listening) { return $true }
    return $false
}

function Write-GuardLog($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $guardLog -Value "$stamp $message" -Encoding UTF8
}

if ($args -contains "-Stop") {
    $guards = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'watchdog\.ps1' -and $_.CommandLine -notmatch '-Stop' }
    foreach ($g in $guards) {
        Stop-Process -Id $g.ProcessId -Force
        Write-Output "guard stopped: $($g.ProcessId)"
    }
    if (-not $guards) { Write-Output "guard not running" }

    $conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $proc = Get-Process -Id $conn[0].OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $proc.Id -Force
            Write-Output "atri stopped: $($proc.Id) $($proc.ProcessName)"
        }
    } else {
        Write-Output "atri not running"
    }
    exit 0
}

if ($args -contains "-Guard") {
    # 互斥：同一时刻只允许一个守卫实例，防止并发拉起多个 ATRI。
    # 注意 PowerShell P/Invoke 后 GetLastError() 会不可靠，必须用
    # SetLastError=true + Marshal.GetLastWin32Error()。
    $existing = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -match 'watchdog\.ps1' -and $_.CommandLine -match '-Guard' -and $_.ProcessId -ne $PID }
    if ($existing) { exit 0 }

    $kernel32 = Add-Type -Name "Kernel32" -Namespace "Win32" -PassThru -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)]
public static extern System.IntPtr CreateMutexW(System.IntPtr lpMutexAttributes, bool bInitialOwner, string lpName);
'@
    [void]$kernel32::CreateMutexW([System.IntPtr]::Zero, $false, "Global\AtriWatchdog")
    if ([System.Runtime.InteropServices.Marshal]::GetLastWin32Error() -eq 183) { exit 0 }   # ERROR_ALREADY_EXISTS

    $crashStreak = 0
    while ($true) {
        # 已有实例在跑（如用户手动启动）：纯等待，不干预。
        if (Test-AtriAlive) {
            Start-Sleep -Seconds 30
            continue
        }

        $started = Get-Date
        # Start-Process -Wait 同步等待：ATRI 进程退出才继续（& 对 GUI 子系统
        # 程序不等待，会让守卫退化为轮询）。
        $proc = Start-Process -FilePath $pythonw -ArgumentList "-m", "atri_qq_bot" `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru -Wait
        $exitCode = $proc.ExitCode
        $uptime = (Get-Date) - $started

        # 8765 已被新实例监听说明等待期间别人重启了 ATRI：视为正常。
        if (Test-AtriAlive) { continue }

        Write-GuardLog "guard: ATRI exited (code=$exitCode uptime=$([int]$uptime.TotalSeconds)s), restarting."

        # 快速崩溃保护：60 秒内连崩 3 次则退避 5 分钟，避免死循环刷日志。
        if ($uptime.TotalSeconds -lt 60) { $crashStreak++ } else { $crashStreak = 0 }
        if ($crashStreak -ge 3) {
            Write-GuardLog "guard: crash streak=$crashStreak, backing off 5 minutes."
            $crashStreak = 0
            Start-Sleep -Seconds 300
        } else {
            Start-Sleep -Seconds 3
        }
    }
}
else {
    if (-not (Test-AtriAlive)) {
        Start-Process -FilePath $pythonw -ArgumentList "-m", "atri_qq_bot" `
            -WorkingDirectory $root -WindowStyle Hidden
        Write-GuardLog "watchdog: ATRI was down, restarted."
    }
}
