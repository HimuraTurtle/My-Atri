$ErrorActionPreference = "SilentlyContinue"

# ATRI 守卫：全栈监护（ATRI 进程 + NapCat 连接）。
# 用法：watchdog.ps1           单次检查（ATRI 不在则拉起，不驻留）
#       watchdog.ps1 -Guard    守卫模式（开机自启动/日常启动使用；ATRI 死
#                              了拉起、NapCat 掉线触发 hidden_launcher napcat
#                              恢复分片；Global 互斥锁单实例）
#       watchdog.ps1 -Stop     停止守卫与 ATRI（维护用；start_atri.bat 可再次启动）

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$guardLog = Join-Path $root "logs\watchdog.log"
$hiddenLauncher = Join-Path $root "tools\launch\qq_legacy\hidden_launcher.py"

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

function Test-NapcatConnected {
    # 8765 上存在 established 连接 = NapCat 反向 WS 已连上 ATRI。
    # 该端口只有 NapCat 一个客户端，listen 不算数——ATRI 活着但 QQ
    # 侧掉线时（登录失效/节点卡死）正是要触发恢复的场景。
    $conn = Get-NetTCPConnection -LocalPort 8765 -State Established -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Start-NapcatSlice {
    # hidden_launcher napcat 模式自带互斥与进程状态判定（登录中/需扫码/
    # 已连接各走各的分支），重复调用幂等；这里只负责触发。
    Start-Process -FilePath $pythonw -ArgumentList "`"$hiddenLauncher`"", "napcat" `
        -WorkingDirectory $root -WindowStyle Hidden
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
    # 互斥：Global 互斥锁是权威判据（内核级）。不再扫描其它 powershell 的
    # 命令行做预检查——任何碰巧含 "watchdog.ps1 -Guard" 字样的终端（比如
    # 用户开着曾运行过该命令的窗口）都会让新守卫误判已有实例而自杀，
    # 2026-11-13 开机自启失效的直接死因之一。
    $kernel32 = Add-Type -Name "Kernel32" -Namespace "Win32" -PassThru -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)]
public static extern System.IntPtr CreateMutexW(System.IntPtr lpMutexAttributes, bool bInitialOwner, string lpName);
'@
    [void]$kernel32::CreateMutexW([System.IntPtr]::Zero, $false, "Global\AtriWatchdog")
    if ([System.Runtime.InteropServices.Marshal]::GetLastWin32Error() -eq 183) { exit 0 }   # ERROR_ALREADY_EXISTS

    # 全栈监护：ATRI 进程死了就拉起（原来就有的能力）；NapCat 未连上
    # 8765 就触发 hidden_launcher napcat 恢复分片（新能力——此前守卫只
    # 管 ATRI，开机后 QQ 侧永远不会在线，等于"开机启动"名存实亡）。
    # 轮询间隔 10 秒：比原事件驱动方案的秒级重启慢几秒，但换来同一线程
    # 可以顺带监护 NapCat；Start-Process -Wait 会阻塞到 ATRI 退出，根本
    # 没有机会做第二件事。
    $crashStreak = 0
    $ownedByGuard = $false
    $ownedSince = Get-Date
    $napcatRetryAt = Get-Date   # 首次立即检查
    while ($true) {
        if (-not (Test-AtriAlive)) {
            # 只对守卫自己拉起的实例统计崩溃连击；用户手动的实例死了
            # 视为普通重启，不惩罚。
            if ($ownedByGuard) {
                $uptime = (Get-Date) - $ownedSince
                if ($uptime.TotalSeconds -lt 60) { $crashStreak++ } else { $crashStreak = 0 }
            }
            if ($crashStreak -ge 3) {
                Write-GuardLog "guard: crash streak=$crashStreak, backing off 5 minutes."
                $crashStreak = 0
                Start-Sleep -Seconds 300
            }
            Start-Process -FilePath $pythonw -ArgumentList "-m", "atri_qq_bot" `
                -WorkingDirectory $root -WindowStyle Hidden
            $ownedByGuard = $true
            $ownedSince = Get-Date
            Write-GuardLog "guard: ATRI was down, started."
            Start-Sleep -Seconds 10
            continue
        }

        # NapCat 监护（带 5 分钟重试冷却，登录页等待期间不刷屏）
        if ((Get-Date) -ge $napcatRetryAt -and -not (Test-NapcatConnected)) {
            Write-GuardLog "guard: NapCat not connected, dispatching recovery slice."
            Start-NapcatSlice
            $napcatRetryAt = (Get-Date).AddMinutes(5)
        }
        Start-Sleep -Seconds 10
    }
}
else {
    if (-not (Test-AtriAlive)) {
        Start-Process -FilePath $pythonw -ArgumentList "-m", "atri_qq_bot" `
            -WorkingDirectory $root -WindowStyle Hidden
        Write-GuardLog "watchdog: ATRI was down, restarted."
    }
}
