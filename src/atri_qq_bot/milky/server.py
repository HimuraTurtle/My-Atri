"""Milky 模式的 ATRI 启动入口：复用 OneBotServer，接入 Lagrange.Milky。

与 run_server（反向 WebSocket 服务端）不同，这里 ATRI 作为客户端主动连接
Milky 的 ``/event`` WebSocket，并把动作映射到 ``/api/:api``。人格、记忆、
工具、论文、QQ 空间等全部复用，仅替换接入层。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..onebot import OneBotServer
from ..persona import AtriReplyEngine
from .config import MilkyConfig
from .transport import MilkyTransport

LOGGER = logging.getLogger("atri.milky")

_RECONNECT_DELAY_SECONDS = 5.0


async def run_milky_server(config: Any) -> None:
    milky = MilkyConfig.from_env()
    from atri_webui import bind_loop, start_webui, stop_webui

    bind_loop(asyncio.get_running_loop())
    reply_engine = AtriReplyEngine(config)
    insights = None
    try:
        from ..insights import InsightsRuntime

        insights = InsightsRuntime()
    except Exception as exc:
        LOGGER.warning("评测与群聊总结旁路未启用：%s", exc)
    server = OneBotServer(config, reply_engine, insights)
    if server.insights is not None:
        await server.insights.start()

    tasks = _start_background_tasks(server)
    webui = await start_webui(config, server)
    transport = MilkyTransport(milky, server)
    LOGGER.info("Milky 模式启动：base=%s", milky.base_url)
    try:
        await _serve_with_reconnect(server, transport)
    finally:
        await stop_webui(webui)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await transport.close()
        await server.shutdown()
        if server.insights is not None:
            await server.insights.close()


async def _serve_with_reconnect(server: OneBotServer, transport: MilkyTransport) -> None:
    while True:
        try:
            await transport.connect()
            await server.handle_connection(transport)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Milky 连接异常，%.0f 秒后重连：%s", _RECONNECT_DELAY_SECONDS, exc)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


def _start_background_tasks(server: OneBotServer) -> list[asyncio.Task[Any]]:
    """启动全部后台循环；返回任务列表供退出时取消。"""
    return [
        asyncio.create_task(server.idle_nudge_loop()),
        asyncio.create_task(server.morning_greeting_loop()),
        asyncio.create_task(server.proactive_v2_loop()),
        asyncio.create_task(server.group_proactive_loop()),
        asyncio.create_task(server.meme_learning_loop()),
        asyncio.create_task(server.manga_update_loop()),
        asyncio.create_task(server.goodnight_loop()),
        asyncio.create_task(server.screen_watch_loop()),
        asyncio.create_task(server.watch_party_loop()),
        asyncio.create_task(server.qzone_loop()),
    ]
