"""插件依赖服务引导。

ATRI 主进程启动时自动拉起插件所需的本机服务：
- Suwayomi-Server（漫画助手，可执行启动脚本并等待就绪）；
- NapCat/QQNT（复用 tools/launch/qq_legacy/hidden_launcher.py 的 napcat
  分片，其内部自带已运行检测、隔离启动与扫码登录状态机）。

引导异常只记录警告，绝不阻塞 OneBot 服务启动；Suwayomi 启动后会同步
等待 HTTP 就绪，最长 ``bootstrap_suwayomi_timeout_seconds``（默认 120 秒）。
"""

from __future__ import annotations

import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .observability import napcat_webui_access
from .paths import PROJECT_ROOT

LOGGER = logging.getLogger("atri.startup")

_HIDDEN_LAUNCHER = PROJECT_ROOT / "tools" / "launch" / "qq_legacy" / "hidden_launcher.py"
_VENV_PYTHONW = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
_VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def bootstrap_plugin_services(config: Any) -> None:
    """在主服务启动前拉起插件依赖；任何异常都不阻塞 bot。"""
    try:
        if not bool(getattr(config, "bootstrap_services_enabled", True)):
            LOGGER.info("依赖服务引导已禁用（BOOTSTRAP_SERVICES_ENABLED=false）")
            return
        _ensure_suwayomi(config)
        _ensure_napcat(config)
    except Exception as exc:  # noqa: BLE001 - 引导属于尽力而为的附加能力
        LOGGER.warning("依赖服务引导出现异常（不影响 ATRI 启动）：%s", exc)


def _ensure_suwayomi(config: Any) -> None:
    if not bool(getattr(config, "manga_enabled", True)):
        return
    if not bool(getattr(config, "bootstrap_suwayomi_enabled", True)):
        return
    server_url = str(getattr(config, "manga_server_url", "") or "").rstrip("/")
    if not server_url:
        return
    if _http_ready(server_url):
        LOGGER.info("依赖服务引导：Suwayomi-Server 已在运行（%s）", server_url)
        return

    raw_script = str(getattr(config, "bootstrap_suwayomi_script", "") or "").strip()
    script = Path(raw_script) if raw_script else (PROJECT_ROOT.parent / "Suwayomi" / "start-suwayomi.ps1")
    if not script.is_file():
        LOGGER.warning(
            "依赖服务引导：未找到 Suwayomi 启动脚本 %s，跳过（漫画服务需手动启动）",
            script,
        )
        return

    LOGGER.info("依赖服务引导：正在启动 Suwayomi-Server（%s）", script)
    # 不能用 capture_output：PowerShell 经 Start-Process 拉起的 java.exe 会
    # 继承捕获管道的写端，PowerShell 退出后 communicate() 永远等不到 EOF，
    # timeout 触发后 Windows 分支还会再阻塞一次，bootstrap 因此卡死。
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        LOGGER.warning(
            "依赖服务引导：Suwayomi 启动脚本返回码 %s，继续等待 HTTP 就绪检测",
            completed.returncode,
        )

    timeout_seconds = max(
        10.0, float(getattr(config, "bootstrap_suwayomi_timeout_seconds", 120.0) or 120.0)
    )
    started_at = time.monotonic()
    while time.monotonic() - started_at < timeout_seconds:
        if _http_ready(server_url):
            LOGGER.info(
                "依赖服务引导：Suwayomi-Server 已就绪（耗时 %.1fs）",
                time.monotonic() - started_at,
            )
            return
        time.sleep(2.0)
    LOGGER.warning(
        "依赖服务引导：Suwayomi-Server 未在 %.0fs 内就绪，漫画功能暂不可用；"
        "稍后可手动运行 %s 修复",
        timeout_seconds,
        script,
    )


def _ensure_napcat(config: Any) -> None:
    if not bool(getattr(config, "bootstrap_napcat_enabled", True)):
        return
    access = napcat_webui_access()
    if not access.get("configured"):
        LOGGER.warning(
            "依赖服务引导：无法确定 NapCat 状态（%s），跳过自动启动",
            access.get("reason", "未知原因"),
        )
        return
    if access.get("reachable"):
        LOGGER.info(
            "依赖服务引导：NapCat 已在运行（WebUI 端口 %s 可达）",
            access.get("port"),
        )
        return

    python_exe = _VENV_PYTHONW if _VENV_PYTHONW.exists() else _VENV_PYTHON
    if not python_exe.exists() or not _HIDDEN_LAUNCHER.is_file():
        LOGGER.warning(
            "依赖服务引导：缺少 %s 或 %s，无法自动启动 NapCat",
            python_exe,
            _HIDDEN_LAUNCHER,
        )
        return

    LOGGER.info("依赖服务引导：NapCat 未运行，正在后台启动 napcat 分片")
    try:
        subprocess.Popen(
            [str(python_exe), str(_HIDDEN_LAUNCHER), "napcat"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        LOGGER.warning("依赖服务引导：NapCat 启动分片发送失败：%s", exc)
        return
    LOGGER.info(
        "依赖服务引导：NapCat 启动分片已发出；QQ 需要扫码登录时会写入 napcat-state.json，"
        "可通过 NapCat WebUI 处理"
    )


def _http_ready(url: str) -> bool:
    """任意 HTTP 响应（含 4xx）都视为服务已监听。"""
    try:
        with urllib.request.urlopen(url, timeout=2.0):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
