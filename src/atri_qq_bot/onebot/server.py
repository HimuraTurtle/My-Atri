from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..application import ConversationTurnRequest
try:
    from ..cognition import CognitionShadowRunner, evidence_from_tool_context
except Exception as cognition_import_error:  # pragma: no cover - 仅用于缺依赖部署兜底
    CognitionShadowRunner = None  # type: ignore[assignment,misc]

    def evidence_from_tool_context(tool_context: Any | None) -> tuple[Any, ...]:
        return ()

    _COGNITION_IMPORT_ERROR = cognition_import_error
else:
    _COGNITION_IMPORT_ERROR = None
from ..group_reply_focus import GroupReplyFocusStore
from ..insights import (
    EvaluationSample,
    InsightsRuntime,
    context_from_tool_context,
)
from ..message_plan import OutgoingMessage, build_outgoing_messages, outgoing_to_onebot_message
from ..music import (
    SingingConversationController,
    SingingDecision,
    detect_explicit_delivery_intent,
)
from ..persona import AtriReplyEngine
from ..proactive import ProactivePlanner, load_proactive_policy, safe_zoneinfo
from ..runtime.control import (
    publish_napcat_runtime_state,
    publish_onebot_event_activity,
    publish_onebot_probe_result,
)
from ..runtime.paths import PROJECT_ROOT
from ..runtime.modality_access import is_event_modality_allowed
from ..stickers import StickerManager
from ..toolbox import ToolAnalyzer
from atri_webui import bind_loop, start_webui, stop_webui
from .message_batch import _merge_message_batch, _message_to_segments
from .message_parser import (
    extract_plain_text,
    extract_reply_inline_message,
    extract_reply_message_id,
    is_poke_event,
    normalize_poke_event,
)
from .reply_policy import _as_int, is_bot_mentioned, should_reply

SMART_GROUP_REPLY_COOLDOWN_SECONDS = 75.0
MESSAGE_DEBOUNCE_SECONDS = 1.2
QUEUE_IDLE_TIMEOUT_SECONDS = 20.0
CONVERSATION_QUEUE_MAXSIZE = 64
VISUAL_CONTEXT_TTL_SECONDS = 15 * 60
PENDING_GROUP_VISUAL_TTL_SECONDS = 60
POKE_REPLY_COOLDOWN_SECONDS = 60.0
ONEBOT_HEALTH_INTERVAL_SECONDS = 30.0
ONEBOT_HEALTH_FAILURES_BEFORE_MANUAL_LOGIN_NOTICE = 2
NAPCAT_MANUAL_LOGIN_NOTICE_COOLDOWN_SECONDS = 10 * 60
LOGGER = logging.getLogger("atri.onebot")


from .server_parts.helpers import (
    _compose_model_input,
    _conversation_id,
    _event_has_visual_material,
    _is_visual_followup,
    _merge_pending_visual_event,
    _message_queue_id,
    _nickname,
    _now_text,
    _onebot_action_error_detail,
    _onebot_status_is_healthy,
    _pending_visual_key,
    _personal_event_message,
    _profile_id,
    _require_onebot_action_success,
    _send_delay,
    _should_reuse_cached_visual,
    _singing_context_id,
)

from .server_parts.server_anima import OneBotAnimaMixin
from .server_parts.server_bilibili import OneBotBilibiliMixin
from .server_parts.server_avatar import OneBotAvatarMixin
from .server_parts.server_connection import OneBotConnectionMixin
from .server_parts.server_delivery import OneBotDeliveryMixin
from .server_parts.server_documents import OneBotDocumentsMixin
from .server_parts.server_events import OneBotEventMixin
from .server_parts.server_image_edit import OneBotImageEditMixin
from .server_parts.server_manga import OneBotMangaMixin
from .server_parts.server_meme_stealer import OneBotMemeStealMixin
from .server_parts.server_minimax_draw import OneBotMinimaxDrawMixin
from .server_parts.server_papers import OneBotPapersMixin
from .server_parts.server_proactive import OneBotProactiveMixin
from .server_parts.server_qzone import OneBotQzoneMixin
from .server_parts.server_screen_watch import OneBotScreenWatchMixin
from .server_parts.server_what_to_eat import OneBotWhatToEatMixin
from .server_parts.server_who_is_who import OneBotWhoIsWhoMixin


class OneBotServer(
    OneBotConnectionMixin,
    OneBotEventMixin,
    OneBotDeliveryMixin,
    OneBotMangaMixin,
    OneBotAnimaMixin,
    OneBotMinimaxDrawMixin,
    OneBotPapersMixin,
    OneBotDocumentsMixin,
    OneBotBilibiliMixin,
    OneBotAvatarMixin,
    OneBotImageEditMixin,
    OneBotWhatToEatMixin,
    OneBotWhoIsWhoMixin,
    OneBotMemeStealMixin,
    OneBotProactiveMixin,
    OneBotQzoneMixin,
    OneBotScreenWatchMixin,
):
    """OneBot v11 的接入适配器。

    消息流转为：NapCat 反向 WebSocket -> ``handle_payload`` -> 按会话队列
    防抖合并 -> ``_handle_event``（回复策略、ASR、多模态）-> 人格引擎 ->
    ``send_reply`` -> OneBot action。每个会话独占队列，避免慢模型请求导致
    同一用户的消息乱序。
    """


BACKGROUND_LOOP_RESTART_SECONDS = 10.0


async def _resilient_background_loop(name: str, factory: Any) -> None:
    """后台循环的统一防death wrapper。

    主动消息/定时任务循环内部曾有裸露调用（policy 加载、提示词渲染等），
    一次异常会让 asyncio.create_task 的协程静默死亡直至重启。这里在任务
    边界兜底：异常记完整 traceback，退避后重新调用工厂方法重启循环。
    正常返回视为终态（功能被配置禁用时循环自己 return，重启它只会每 10
    秒刷一条告警）。CancelledError 原样上抛，不影响关停流程。
    """
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "%s 后台循环异常退出，%.1f 秒后重启",
                name,
                BACKGROUND_LOOP_RESTART_SECONDS,
            )
            await asyncio.sleep(BACKGROUND_LOOP_RESTART_SECONDS)
        else:
            LOGGER.info("%s 后台循环已正常退出（视为终态，不再重启）", name)
            return


def _spawn_background_loops(server: Any) -> list[asyncio.Task]:
    factories = (
        ("闲置提醒", server.idle_nudge_loop),
        ("早安问候", server.morning_greeting_loop),
        ("主动消息V2", server.proactive_v2_loop),
        ("群聊主动", server.group_proactive_loop),
        ("表情学习", server.meme_learning_loop),
        ("漫画更新", server.manga_update_loop),
        ("晚安问候", server.goodnight_loop),
        ("屏幕看护", server.screen_watch_loop),
        ("围观模式", server.watch_party_loop),
        ("QQ空间", server.qzone_loop),
    )
    return [
        asyncio.create_task(_resilient_background_loop(name, factory))
        for name, factory in factories
    ]


async def run_server(config: Any) -> None:
    import websockets

    bind_loop(asyncio.get_running_loop())
    reply_engine = AtriReplyEngine(config)
    insights_runtime: InsightsRuntime | None = None
    try:
        insights_runtime = InsightsRuntime()
    except Exception as exc:
        # 评测/总结是纯旁路：初始化失败时只关闭扩展，不影响 OneBot 回复能力。
        LOGGER.warning("评测与群聊总结旁路未启用，继续运行核心回复链路：%s", exc)
    server = OneBotServer(config, reply_engine, insights_runtime)
    if server.insights is not None:
        await server.insights.start()

    async with websockets.serve(server.handle_connection, config.host, config.port):
        print(
            f"[onebot] Listening on ws://{config.host}:{config.port}/onebot "
            f"for QQ {config.bot_qq}; reply_mode={config.reply_mode}"
        )
        background_tasks = _spawn_background_loops(server)
        webui_server = await start_webui(config, server)
        try:
            await asyncio.Future()
        finally:
            await stop_webui(webui_server)
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await server.shutdown()
            if getattr(server, "_manga_service", None) is not None:
                with contextlib.suppress(Exception):
                    await server._manga_service.close()
            if server.insights is not None:
                await server.insights.close()
            if server.cognition_shadow is not None:
                await server.cognition_shadow.close()
