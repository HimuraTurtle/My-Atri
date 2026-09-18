"""ATRI 主进程入口。

本文件只负责生命周期编排：加载配置、建立单实例保护、配置日志并启动
OneBot 服务。具体的消息处理、人格生成和工具调用都留在各自模块。
"""

from __future__ import annotations

import asyncio
import ctypes
import faulthandler
import logging
import os
import sys
from pathlib import Path


_BOT_MUTEX_HANDLE: int | None = None
# faulthandler 的目标文件必须保持模块级引用，否则对象被 GC 关闭 fd 后
# 悬空描述符会在写入时造成未定义行为（进程随机崩溃）。
_CRASH_LOG_FILE = None


def _acquire_bot_mutex() -> bool:
    """防止桌宠、启动器和手动命令并发拉起多个 ATRI 进程。"""
    if os.name != "nt":
        return True
    global _BOT_MUTEX_HANDLE
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Global\\AtriQQBotService")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _BOT_MUTEX_HANDLE = handle
    return True

from .config import load_config
from .onebot import run_server
from .runtime import configure_project_logging, napcat_webui_access
from .runtime.service_bootstrap import bootstrap_plugin_services


def main() -> None:
    if not _acquire_bot_mutex():
        return
    # 启动顺序很重要：配置和日志必须先准备好，OneBot 才能把后续事件
    # 写入正确的日志文件；NapCat 作为反向 WebSocket 客户端随后自动接入。
    # 启动顺序：先加载 .env，再启动 OneBot/WebUI；NapCat 是反向 WS 客户端，
    # 会在服务监听后自行连接，因此不在这里启动或修改 QQ/NapCat 进程。
    configure_project_logging()
    logger = logging.getLogger("atri.startup")
    # 崩溃留痕：pythonw 无控制台，致命错误无处可写时进程会静默消失；
    # faulthandler 把 Python 回溯写进 crash.log，便于事后定位幽灵退出。
    try:
        _crash_file = Path("logs") / "crash.log"
        _crash_file.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG_FILE = open(_crash_file, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)
    except Exception:
        pass
    try:
        config = load_config()
    except (OSError, TypeError, ValueError) as exc:
        logger.exception("配置加载失败，ATRI 未启动：%s", exc)
        return
    # 插件依赖服务引导：拉起 Suwayomi-Server 与 NapCat（异常不阻塞启动；
    # Suwayomi 就绪等待最长 BOOTSTRAP_SUWAYOMI_TIMEOUT_SECONDS，默认 120 秒）。
    bootstrap_plugin_services(config)
    access = napcat_webui_access()
    if access.get("configured"):
        # 桌宠后台启动时不写控制台，避免 Windows 为输出创建黑色窗口。
        # 手动在终端启动时仍可看到本机访问地址；文件日志只记录端口，
        # 避免长期落盘泄露 Token。
        if sys.stdout is not None and sys.stdout.isatty():
            print(f"[developer] NapCat WebUI: {access.get('url')}")
        logger.info("NapCat WebUI 配置已发现：port=%s reachable=%s", access.get("port"), access.get("reachable"))
    else:
        logger.warning("NapCat WebUI 未就绪：%s", access.get("reason", "未知原因"))
    try:
        logger.info("启动 OneBot 服务：ws://%s:%s/onebot", config.host, config.port)
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        logger.info("收到停止信号，ATRI 已退出")


if __name__ == "__main__":
    main()
