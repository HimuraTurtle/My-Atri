from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from atri_qq_bot.config import BotConfig, load_config
from atri_qq_bot.llm_tools.web_search_tool import search_web
from atri_qq_bot.runtime import PROJECT_ROOT

from .evaluation_handlers import EvaluationHandlersMixin
from .memory_admin import memory_admin_for_config
from .qzone_admin import QZoneAdmin
from .qzone_automation import (
    QZoneAutomation,
    QZoneAutomationStore,
    sanitize_qzone_fact_context,
    topic_for_schedule,
)
from .server_parts.shared import (
    LOGGER,
    generate_qzone_text,
    run_coro,
)
from .server_parts.shared import bind_loop, test_chat  # 再导出：onebot 与测试从本模块取用

# WebUI 是本地运维面板，不承载对话主循环。API 管理配置、记忆、日志和音频资源；
# QQ 连接仍由 OneBot 进程管理，避免浏览器请求直接触碰 QQ 状态。


class WebUIState:
    def __init__(self, config: BotConfig, server: Any) -> None:
        self.config = config
        self.server = server
        self.lock = asyncio.Lock()
        self.group_name_lock = asyncio.Lock()
        self._group_name_refresh_at = 0.0
        self.memory_admin = memory_admin_for_config(config)
        configured_database = Path(
            os.getenv("QZONE_AUTOMATION_DB", "data/qzone_automation.sqlite3")
        )
        database = (
            configured_database
            if configured_database.is_absolute()
            else PROJECT_ROOT / configured_database
        )
        self.qzone_automation = QZoneAutomation(
            QZoneAutomationStore(database),
            lambda: QZoneAdmin(
                self.memory_admin.summary(),
                bot_qq=self.config.bot_qq,
                owner_qqs=self.config.owner_qqs,
            ),
            lambda prompt, profile_id: run_coro(generate_qzone_text(self.config, prompt, "post", profile_id)),
            lambda prompt, profile_id: run_coro(generate_qzone_text(self.config, prompt, "comment", profile_id)),
            lambda settings, scheduled_at, slot_index=None: qzone_fact_context(
                self.config,
                settings,
                scheduled_at,
                slot_index,
            ),
        )
        self.qzone_login_lock = threading.Lock()
        self.qzone_login_running = False
        self.qzone_login_last: dict[str, Any] = {"ok": None, "message": ""}

    async def refresh_group_names(self, force: bool = False) -> dict[str, str]:
        """定期从 NapCat 同步群名，WebUI 断线时保留 SQLite 中的缓存。"""
        runtime = getattr(self.server, "insights", None)
        if runtime is None:
            return {}
        names = runtime.store.group_name_map()
        now = time.monotonic()
        if not force and now - self._group_name_refresh_at < 30:
            return names
        async with self.group_name_lock:
            now = time.monotonic()
            if not force and now - self._group_name_refresh_at < 30:
                return runtime.store.group_name_map()
            self._group_name_refresh_at = now
            fetch = getattr(self.server, "fetch_group_list", None)
            if not callable(fetch):
                return names
            try:
                groups = await fetch()
            except Exception as exc:
                LOGGER.debug("WebUI 同步群名失败：%s", exc)
                return names
            for item in groups:
                if isinstance(item, dict) and item.get("group_id") and item.get("group_name"):
                    runtime.store.set_group_name(str(item["group_id"]), str(item["group_name"]))
            return runtime.store.group_name_map()

    async def reload_config(self) -> BotConfig:
        async with self.lock:
            new_config = load_config()
            self.config = new_config
            self.memory_admin = memory_admin_for_config(new_config)
            self.server.config = new_config
            self.server.reply_engine.config = new_config
            if hasattr(self.server.reply_engine, "memory"):
                self.server.reply_engine.memory.update_retrieval_config(new_config)
            self.server.tools.config = new_config
            self.server.tools.enabled = bool(new_config.toolbox_enabled)
            self.server.tools.timeout = float(new_config.toolbox_timeout_seconds)
            self.server.tools.max_bytes = int(new_config.toolbox_max_bytes)
            self.server.tools.vision_enabled = bool(new_config.toolbox_vision_enabled)
            self.server.tools.vision_model = str(
                new_config.toolbox_vision_model or new_config.openai_model or ""
            )
            self.server.tools.vision_fallback_model = str(
                new_config.toolbox_vision_fallback_model or ""
            )
            self.server.tools.vision_base_url = str(
                new_config.toolbox_vision_base_url or new_config.openai_base_url or ""
            ).rstrip("/")
            self.server.tools.vision_fallback_base_url = str(
                new_config.toolbox_vision_fallback_base_url or ""
            ).rstrip("/")
            self.server.tools.vision_api_key = (
                new_config.toolbox_vision_api_key or new_config.openai_api_key
            )
            self.server.tools.vision_retry_count = int(
                new_config.toolbox_vision_retry_count
            )
            self.server.tools.vision_resource_wait_seconds = float(
                new_config.toolbox_vision_resource_wait_seconds
            )
            self.server.tools.vision_unload_other_ollama_models = bool(
                new_config.toolbox_vision_unload_other_ollama_models
            )
            self.server.tools.ocr_enabled = bool(
                new_config.toolbox_ocr_enabled
            )
            if hasattr(self.server, "proactive_planner"):
                self.server.proactive_planner.update_owner_qqs(new_config.owner_qqs)
            # 模型页面切换聊天模型后，评测旁路立即复用同一模型；不再维护第二套
            # Judge 接口和密钥配置。评测自身仍然在后台队列中执行。
            if getattr(self.server, "insights", None) is not None:
                await self.server.insights.reload_config()
            return new_config


async def start_webui(config: BotConfig, onebot_server: Any) -> Any | None:
    if not getattr(config, "webui_enabled", True):
        return None

    host = str(getattr(config, "webui_host", "127.0.0.1") or "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        host = "127.0.0.1"
    port = int(getattr(config, "webui_port", 8787) or 8787)
    state = WebUIState(config, onebot_server)

    class Handler(AtriWebUIHandler):
        webui_state = state

    try:
        httpd = LocalThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        LOGGER.warning("WebUI 端口 http://%s:%s 不可用，已跳过：%s", host, port, exc)
        return None
    task = asyncio.create_task(asyncio.to_thread(httpd.serve_forever))
    httpd._atri_task = task  # type: ignore[attr-defined]
    httpd._atri_webui_state = state  # type: ignore[attr-defined]
    state.qzone_automation.start()
    LOGGER.info("WebUI 已监听 http://%s:%s", host, port)
    return httpd


async def stop_webui(httpd: Any | None) -> None:
    if httpd is None:
        return
    state = getattr(httpd, "_atri_webui_state", None)
    if state is not None:
        await state.qzone_automation.stop()
    httpd.shutdown()
    task = getattr(httpd, "_atri_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


from .server_parts.handler_config import HandlerConfigMixin
from .server_parts.handler_core import HandlerCoreMixin
from .server_parts.handler_insights import HandlerInsightsMixin
from .server_parts.handler_music import HandlerMusicMixin
from .server_parts.handler_qzone import HandlerQZoneMixin
from .server_parts.handler_response import HandlerResponseMixin
from .server_parts.handler_stickers_memory import HandlerStickersMemoryMixin


class AtriWebUIHandler(
    HandlerCoreMixin,
    HandlerConfigMixin,
    HandlerMusicMixin,
    HandlerInsightsMixin,
    HandlerStickersMemoryMixin,
    HandlerQZoneMixin,
    HandlerResponseMixin,
    EvaluationHandlersMixin,
    BaseHTTPRequestHandler,
):
    webui_state: WebUIState


def qzone_fact_context(
    config: BotConfig,
    settings: dict[str, Any],
    scheduled_at: float,
    slot_index: int | None = None,
) -> str:
    """Fetch recent evidence, then discard stale or low-attribution search results."""
    topic = topic_for_schedule(settings, scheduled_at, slot_index)
    query = f"{topic} 最新消息 {datetime.fromtimestamp(float(scheduled_at)).strftime('%Y-%m-%d')}"
    research_topic = any(marker in topic for marker in ("前沿", "科普", "技术", "科学", "研究"))
    trusted_domains = ["cas.cn", "arxiv.org", "nature.com"] if research_topic else ["gov.cn", "news.cn", "people.com.cn"]
    raw = str(
        run_coro(
            search_web(
                {
                    "query": query,
                    "mode": "news",
                    "domains": trusted_domains,
                    "max_results": 5,
                },
                config,
            )
        )
        or ""
    ).strip()
    return sanitize_qzone_fact_context(raw, scheduled_at)
