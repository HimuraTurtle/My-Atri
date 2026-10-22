# ATRI one-click installer (five-phase wizard).
# Entry: 安装亚托莉.bat (project root). All phases are idempotent and re-runnable.
#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# tools\install -> tools -> project root
$ProjectDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$VenvDir    = Join-Path $ProjectDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$EnvFile    = Join-Path $ProjectDir '.env'
$EnvExample = Join-Path $ProjectDir '.env.example'
$LogFile    = Join-Path $PSScriptRoot 'install.log'
$StartAllBat = Join-Path $ProjectDir 'tools\launch\qq_legacy\start-all.bat'

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch { }

Start-Transcript -Path $LogFile -Append | Out-Null

function Write-Title([string]$Text) {
    Write-Host ''
    Write-Host ('===== ' + $Text + ' =====') -ForegroundColor Cyan
}

function Write-Ok([string]$Text)   { Write-Host ('  [OK] ' + $Text) -ForegroundColor Green }
function Write-Bad([string]$Text)  { Write-Host ('  [X]  ' + $Text) -ForegroundColor Red }
function Write-Note([string]$Text) { Write-Host ('  [!]  ' + $Text) -ForegroundColor Yellow }
function Write-Info([string]$Text) { Write-Host ('  ' + $Text) }

function Read-YesNo([string]$Question, [bool]$Default = $true) {
    $hint = if ($Default) { '(Y/n)' } else { '(y/N)' }
    Write-Host ('  ? ' + $Question + ' ' + $hint) -ForegroundColor Magenta -NoNewline
    $answer = Read-Host ' '
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return ($answer.Trim().ToLowerInvariant()) -in @('y', 'yes')
}

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*?)\s*$'
    foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
        $match = [regex]::Match($line, $pattern)
        if ($match.Success) { return $match.Groups[1].Value }
    }
    return $null
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = if (Test-Path -LiteralPath $EnvFile) {
        [IO.File]::ReadAllLines($EnvFile)
    } else {
        @()
    }
    $pattern = '^(\s*)' + [regex]::Escape($Key) + '\s*='
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    [IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
}

function Resolve-Python {
    $specs = @(
        @{ Exe = 'py';      Args = @('-3.11') },
        @{ Exe = 'py';      Args = @() },
        @{ Exe = 'python';  Args = @() }
    )
    foreach ($spec in $specs) {
        try {
            & $spec.Exe @($spec.Args) -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $spec }
        } catch { continue }
    }
    return $null
}

function Resolve-QqExe {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Tencent\QQNT\QQ.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Tencent\QQNT\QQ.exe'),
        'D:\Program Files\Tencent\QQNT\QQ.exe'
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    foreach ($hive in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe'
    )) {
        $value = (Get-ItemProperty -LiteralPath $hive -ErrorAction SilentlyContinue).'(default)'
        if ($value -and (Test-Path -LiteralPath $value)) { return $value }
    }
    return $null
}

function Test-NapcatRuntime([string]$Dir) {
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return $false }
    foreach ($name in @('NapCatWinBootMain.exe', 'NapCatWinBootHook.dll', 'napcat.mjs', 'qqnt.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dir $name))) { return $false }
    }
    return $true
}

function Find-NapcatDir {
    $configured = Get-EnvValue 'NAPCAT_DIR'
    if (-not $configured) { $configured = $env:NAPCAT_DIR }
    if ($configured) {
        $expanded = [Environment]::ExpandEnvironmentVariables($configured.Trim('"'))
        if (Test-NapcatRuntime $expanded) { return (Resolve-Path -LiteralPath $expanded).Path }
    }
    $roots = @('D:\Tools\NapCat\OneKey', (Join-Path $env:LOCALAPPDATA 'NapCat'))
    foreach ($root in $roots) {
        if (Test-NapcatRuntime $root) { return (Resolve-Path -LiteralPath $root).Path }
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $hits = @(Get-ChildItem -Path $root -Recurse -Depth 5 -Directory -Filter 'napcat' -ErrorAction SilentlyContinue |
            Where-Object { Test-NapcatRuntime $_.FullName })
        if ($hits.Count -gt 0) {
            return ($hits | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
        }
    }
    return $null
}

function Get-AtriPortState {
    $listen = $false
    $established = $false
    $lines = netstat -ano | Where-Object { $_ -match ':8765\s' }
    foreach ($line in $lines) {
        if ($line -match 'LISTENING')    { $listen = $true }
        if ($line -match 'ESTABLISHED')  { $established = $true }
    }
    return @{ Listen = $listen; Established = $established }
}

function Wait-AtriConnected([int]$TimeoutSeconds = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $state = Get-AtriPortState
        if ($state.Established) { return $true }
        Start-Sleep -Seconds 5
    }
    return (Get-AtriPortState).Established
}

$script:PythonOk = $false
$script:VenvOk = $false
$script:QqPath = $null
$script:NapcatDir = $null

function Invoke-Phase0 {
    Write-Title '阶段 0/4 · 环境预检'

    $python = Resolve-Python
    $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    if ($python) {
        Write-Ok ('Python 3.11+ 可用：' + $python.Exe + ' ' + ($python.Args -join ' '))
        $script:PythonOk = $true
    } elseif ($hasUv) {
        Write-Ok '检测到 uv（PATH 无 Python，将由 uv 自动解析/下载 Python 并创建虚拟环境）'
        $script:PythonOk = $true
    } else {
        Write-Bad '未找到 Python 3.11+ 或 uv'
        Write-Info '请先安装 Python 3.11+（勾选 Add python.exe to PATH），装完后重新运行本安装器。'
        Write-Info '已用 uv 的用户无需全局 Python，直接重跑安装器即可。'
        if (Read-YesNo '现在打开 Python 官方下载页？') {
            Start-Process 'https://www.python.org/downloads/windows/'
        }
        return $false
    }

    $drive = Get-PSDrive -Name ($ProjectDir.Substring(0, 1)) -ErrorAction SilentlyContinue
    if ($drive -and $drive.Free -lt 3GB) {
        Write-Note ('磁盘剩余空间不足 3 GB（当前约 ' + [int]($drive.Free / 1MB) + ' MB），依赖安装可能失败')
    } else {
        Write-Ok '磁盘空间充足'
    }

    $script:QqPath = Resolve-QqExe
    if ($script:QqPath) {
        Write-Ok ('QQ 客户端已安装：' + $script:QqPath)
    } else {
        Write-Note '未检测到 QQ 官方客户端（QQNT）'
        if (Read-YesNo '现在打开 QQ 官方下载页？稍后可在阶段 2 继续检测') {
            Start-Process 'https://im.qq.com/pcqq/index.shtml'
        }
    }

    try {
        $request = [Net.HttpWebRequest]::Create('https://pypi.org/simple/')
        $request.Timeout = 5000
        $request.Method = 'HEAD'
        $response = $request.GetResponse()
        $response.Close()
        Write-Ok 'PyPI 网络连通'
        return $true
    } catch {
        Write-Note '访问 PyPI 官方源超时，稍后将默认使用清华镜像安装依赖'
        return $true
    }
}

function Invoke-Phase1 {
    Write-Title '阶段 1/4 · Python 环境'

    if (Test-Path -LiteralPath $VenvPython) {
        Write-Ok '.venv 已存在，跳过创建'
    } else {
        $python = Resolve-Python
        $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
        $created = $false
        if ($python) {
            Write-Info '创建虚拟环境 .venv ...'
            & $python.Exe @($python.Args) -m venv $VenvDir | Out-Null
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $VenvPython)) {
                $created = $true
            }
        } elseif ($hasUv) {
            Write-Info '使用 uv 创建虚拟环境 .venv（首次会自动下载 Python）...'
            & uv venv $VenvDir
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $VenvPython)) {
                $created = $true
            }
        }
        if (-not $created) {
            Write-Bad '虚拟环境创建失败'
            return $false
        }
        Write-Ok '虚拟环境创建完成'
    }

    $useMirror = $false
    try {
        $request = [Net.HttpWebRequest]::Create('https://pypi.tuna.tsinghua.edu.cn/simple/')
        $request.Timeout = 3000
        $request.Method = 'HEAD'
        $response = $request.GetResponse()
        $response.Close()
        $reachable = $true
    } catch {
        $reachable = $false
    }
    if ($reachable -and (Read-YesNo '是否使用清华镜像加速依赖下载？' $false)) {
        $useMirror = $true
    }
    $pipArgs = @()
    if ($useMirror) {
        $pipArgs = @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple')
        Write-Info '使用清华镜像源'
    }

    Write-Info '安装核心依赖（requirements.txt）...'
    & $VenvPython -m pip install --upgrade pip @pipArgs --quiet 2>$null
    & $VenvPython -m pip install -r (Join-Path $ProjectDir 'requirements.txt') @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Bad '依赖安装失败，可检查网络后重跑本安装器'
        return $false
    }
    Write-Ok '核心依赖安装完成'
    $script:VenvOk = $true

    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
        Write-Ok '已从 .env.example 生成 .env 配置文件'
    } else {
        Write-Note '.env 已存在，保留现有配置'
    }

    $botQQ = Get-EnvValue 'BOT_QQ'
    if ($botQQ -and $botQQ -ne 'YOUR_BOT_QQ') {
        Write-Ok ('BOT_QQ 已配置（' + $botQQ + '）')
    } else {
        while ($true) {
            $inputQQ = Read-Host '  请输入 ATRI 的 QQ 号（用于接收消息的机器人号）'
            $inputQQ = ('' + $inputQQ).Trim()
            if ($inputQQ -match '^\d{5,12}$') {
                Set-EnvValue 'BOT_QQ' $inputQQ
                Write-Ok 'BOT_QQ 已写入 .env'
                break
            }
            Write-Note 'QQ 号应为 5-12 位数字，请重新输入'
        }
    }

    $ownerQQ = Get-EnvValue 'OWNER_QQ'
    if ($ownerQQ) {
        Write-Ok ('OWNER_QQ 已配置（' + $ownerQQ + '）')
    } else {
        $inputOwner = Read-Host '  请输入你自己的 QQ 号（可选，直接回车跳过；用于早安推送等功能）'
        $inputOwner = ('' + $inputOwner).Trim()
        if ($inputOwner -match '^\d{5,12}$') {
            Set-EnvValue 'OWNER_QQ' $inputOwner
            Write-Ok 'OWNER_QQ 已写入 .env'
        } else {
            Write-Info '跳过 OWNER_QQ，可稍后手动编辑 .env'
        }
    }

    Write-Info '运行环境自检（check_env.py）...'
    & $VenvPython (Join-Path $ProjectDir 'tools\check_env.py')
    Write-Info '若上方仍有未通过项，可修复后重跑安装器（各阶段幂等可重复）'
    return $true
}

function Invoke-Phase2 {
    Write-Title '阶段 2/4 · 协议端（QQ + NapCat）'

    if (-not $script:QqPath) {
        $script:QqPath = Resolve-QqExe
    }
    if ($script:QqPath) {
        Write-Ok ('QQ 客户端已就绪：' + $script:QqPath)
    } else {
        Write-Note '仍未检测到 QQ 客户端，这是 ATRI 收发消息的必需组件'
        if (Read-YesNo '现在打开 QQ 官方下载页？安装后按回车重新检测') {
            Start-Process 'https://im.qq.com/pcqq/index.shtml'
        }
        Read-Host '  安装完成后按回车继续'
        $script:QqPath = Resolve-QqExe
        if ($script:QqPath) {
            Write-Ok ('QQ 客户端已就绪：' + $script:QqPath)
        } else {
            Write-Bad '仍未检测到 QQ，请先安装 QQ 再重跑安装器'
            return $false
        }
    }

    $script:NapcatDir = Find-NapcatDir
    if ($script:NapcatDir) {
        Write-Ok ('NapCat 已就绪：' + $script:NapcatDir)
        Set-EnvValue 'NAPCAT_DIR' $script:NapcatDir
        Write-Ok 'NAPCAT_DIR 已写入 .env'
        return $true
    }

    Write-Note '未检测到 NapCat（QQ 协议端）'
    Write-Info 'NapCat 下载指引：'
    Write-Info '  1. 打开 NapCat 官方发布页（稍后可点选打开）'
    Write-Info '  2. 下载 NapCat OneKey 压缩包并解压到任意目录（推荐 D:\Tools\NapCat\OneKey）'
    Write-Info '  3. 目录中需包含 NapCatWinBootMain.exe 等四个文件'
    if (Read-YesNo '现在打开 NapCat 发布页？') {
        Start-Process 'https://github.com/NapNeko/NapCatQQ/releases'
    }
    $answer = Read-Host '  解压完成后输入 NapCat 所在目录路径（直接回车自动重新检测）'
    $answer = ('' + $answer).Trim().Trim('"')
    if ($answer) {
        if (Test-NapcatRuntime $answer) {
            $script:NapcatDir = (Resolve-Path -LiteralPath $answer).Path
            Set-EnvValue 'NAPCAT_DIR' $script:NapcatDir
            Write-Ok ('NapCat 已就绪：' + $script:NapcatDir)
            return $true
        }
        Write-Bad '该目录缺少 NapCat 必需文件（NapCatWinBootMain.exe / NapCatWinBootHook.dll / napcat.mjs / qqnt.json）'
        return $false
    }
    $script:NapcatDir = Find-NapcatDir
    if ($script:NapcatDir) {
        Set-EnvValue 'NAPCAT_DIR' $script:NapcatDir
        Write-Ok ('NapCat 已就绪：' + $script:NapcatDir)
        return $true
    }
    Write-Note '仍未找到 NapCat，请解压后重跑安装器（前面各阶段会自动跳过）'
    return $false
}

function Invoke-Phase3 {
    Write-Title '阶段 3/4 · 启动与登录'
    if (-not (Read-YesNo '现在启动 ATRI 并扫码登录？（也可稍后手动启动）' $false)) {
        Write-Info '跳过。手动启动方式：双击 tools\launch\qq_legacy\start-all.bat'
        return $true
    }

    if (Test-Path -LiteralPath $StartAllBat) {
        Start-Process -FilePath $StartAllBat -WorkingDirectory $ProjectDir
        Write-Info '已请求启动 ATRI + QQ（hidden_launcher 全权负责，QQ 窗口弹出后请扫码登录）'
    } else {
        Write-Note ('启动脚本缺失：' + $StartAllBat)
        return $false
    }

    Write-Info '等待 NapCat 连接（最长 120 秒，扫码登录后自动完成）...'
    if (Wait-AtriConnected 120) {
        Write-Ok 'NapCat 已连接 ATRI（端口 8765 已建立连接）'
        Write-Info '最后一步：用另一个 QQ 号给机器人发一条消息，测试是否回复'
        return $true
    }
    $state = Get-AtriPortState
    if ($state.Listen) {
        Write-Note 'ATRI 已在运行，但 NapCat 尚未连上（可能还没扫码）'
        Write-Info '请打开 NapCat WebUI（http://127.0.0.1:6099）完成扫码登录'
        Write-Info '之后可用「检查亚托莉状态.bat」确认连接状态'
        return $true
    }
    Write-Bad 'ATRI 服务未监听 8765 端口，请查看 logs\ 目录中的最新日志'
    return $false
}

function Invoke-Phase4 {
    Write-Title '阶段 4/4 · 可选组件'

    if (Read-YesNo '创建桌宠快捷方式（桌面 + 开始菜单，装完自动启动桌宠）？' $false) {
        $petInstaller = Join-Path $ProjectDir 'tools\desktop_pet\install-desktop-pet.ps1'
        if (Test-Path -LiteralPath $petInstaller) {
            & $petInstaller -Launch
        } else {
            Write-Note ('桌宠安装脚本缺失：' + $petInstaller)
        }
    }

    if (Read-YesNo '设置开机自启（看门狗登录后自动拉起 ATRI）？' $false) {
        $autostart = Join-Path $ProjectDir 'tools\launch\install_autostart.ps1'
        if (Test-Path -LiteralPath $autostart) {
            & $autostart
        } else {
            Write-Note ('自启脚本缺失：' + $autostart)
        }
    }

    $ollamaUp = $false
    try {
        $client = New-Object Net.Sockets.TcpClient
        $task = $client.ConnectAsync('127.0.0.1', 11434)
        if ($task.Wait(500) -and $client.Connected) { $ollamaUp = $true }
        $client.Close()
    } catch { }
    if ($ollamaUp) {
        Write-Ok 'Ollama 已运行（默认聊天模型 qwen3:4b-instruct 走本地）'
        Write-Info '如未拉取模型可执行：ollama pull qwen3:4b-instruct 和 ollama pull bge-m3'
    } else {
        Write-Note '未检测到 Ollama。默认 .env 配置使用本地模型（qwen3:4b-instruct），没有 Ollama 时聊天功能不可用'
        Write-Info '两个选择：'
        Write-Info '  A. 安装 Ollama 并拉取模型（免费、数据不出本机）：https://ollama.com/download'
        Write-Info '  B. 编辑 .env 将 OPENAI_BASE_URL/API_KEY/MODEL 改为任意 OpenAI 兼容云端接口'
    }

    $visionKey = Get-EnvValue 'TOOLBOX_VISION_API_KEY'
    if ($visionKey -and $visionKey -ne 'YOUR_DEEPSEEK_API_KEY') {
        Write-Ok 'DeepSeek 视觉 API key 已配置'
    } else {
        Write-Info '可选：在 .env 的 TOOLBOX_VISION_API_KEY 填入 DeepSeek key，解锁图像理解/屏幕看护等能力'
    }
    return $true
}

try {
    Write-Host ''
    Write-Host '==========================================' -ForegroundColor Cyan
    Write-Host '  亚托莉（ATRI）一键安装向导' -ForegroundColor Cyan
    Write-Host '  各阶段幂等，中断后重跑会自动跳过已完成步骤' -ForegroundColor DarkGray
    Write-Host '==========================================' -ForegroundColor Cyan

    $results = @{}
    $results['阶段 0 预检']   = Invoke-Phase0
    if (-not $results['阶段 0 预检']) { throw '阶段 0 预检未通过，安装终止' }

    $results['阶段 1 Python'] = Invoke-Phase1
    if (-not $results['阶段 1 Python']) { throw '阶段 1 未通过，安装终止' }

    $results['阶段 2 协议端'] = Invoke-Phase2
    if (-not $results['阶段 2 协议端']) { throw '阶段 2 未通过，安装终止' }

    $results['阶段 3 登录']   = Invoke-Phase3
    $results['阶段 4 可选']   = Invoke-Phase4

    Write-Title '安装总结'
    foreach ($name in @('阶段 0 预检', '阶段 1 Python', '阶段 2 协议端', '阶段 3 登录', '阶段 4 可选')) {
        $mark = if ($results[$name]) { 'OK' } else { '未完成' }
        Write-Info ($name + '：' + $mark)
    }
    Write-Info ('安装日志：' + $LogFile)
    Write-Info '日常操作：启动用 tools\launch\qq_legacy\start-all.bat，停止用根目录「停止亚托莉.bat」'
} catch {
    Write-Host ''
    Write-Bad ('安装中断：' + $_.Exception.Message)
    Write-Info '修复后重新双击 安装亚托莉.bat 即可续装，已完成阶段会自动跳过。'
} finally {
    Stop-Transcript | Out-Null
}
