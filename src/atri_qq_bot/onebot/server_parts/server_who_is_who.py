"""OneBot 层的 Who Is Who 接入（移植自 astrbot_plugin_who_is_who）。

三类接入点：
- 群消息记录：每条群消息归一化入库（身份/称呼/话题），不要求被 @；
- /wiw 指令：确定性命令，在回复门控之前拦截，不经过聊天模型；
- 回复上下文：门控与确定性命令都通过后构建共享上下文注入 system
  提示，AI 回复落地后记录 bot 消息并裁决承诺。

全部本地 SQLite + 正则，共享上下文的构建不增加额外 LLM 调用。
"""
from __future__ import annotations

import logging
from typing import Any

from ...message_plan import OutgoingMessage
from ...who_is_who import PreparedTurn, WhoIsWhoService, get_who_is_who_service
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")


class OneBotWhoIsWhoMixin(OneBotServerMixin):
    @property
    def who_is_who(self) -> WhoIsWhoService | None:
        if not bool(getattr(self.config, "wiw_enabled", True)):
            return None
        service = getattr(self, "_who_is_who_service", None)
        if service is None:
            service = get_who_is_who_service(self.config)
            self._who_is_who_service = service
        return service

    def _record_who_is_who_message(self, event: dict[str, Any], plain_text: str) -> None:
        service = self.who_is_who
        if service is None or not service.enabled_for_event(event):
            return
        try:
            service.record_group_message(event, plain_text)
        except Exception:
            LOGGER.exception("Who Is Who 记录群消息失败：group=%s", event.get("group_id"))

    async def _maybe_handle_who_is_who_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中 /wiw 指令时发送回复并返回 True，调用方直接结束本轮。"""
        service = self.who_is_who
        if service is None or event.get("message_type") != "group":
            return False
        try:
            reply = service.handle_command(event, plain_text)
        except Exception:
            LOGGER.exception("Who Is Who 指令处理失败：conversation=%s", conversation_id)
            return False
        if reply is None:
            return False
        trust_tier = getattr(self.reply_engine.memory, "trust_tier", None)
        if callable(trust_tier) and trust_tier(_profile_id(event)) == "blocked":
            return False
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", reply)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("Who Is Who 指令回复发送失败：conversation=%s", conversation_id)
            return False
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        self.reply_engine.record_bot_reply(conversation_id, reply, False)
        return True

    def _prepare_who_is_who_turn(self, event: dict[str, Any]) -> PreparedTurn | None:
        service = self.who_is_who
        if service is None or not service.enabled_for_event(event):
            return None
        return service.prepare_turn(event)

    def _finish_who_is_who_turn(
        self,
        event: dict[str, Any],
        reply_text: str,
        prepared: PreparedTurn | None,
    ) -> None:
        if prepared is None:
            return
        service = self.who_is_who
        if service is None:
            return
        try:
            service.finish_turn(event, reply_text, prepared)
        except Exception:
            LOGGER.exception("Who Is Who 记录 AI 回复失败：group=%s", event.get("group_id"))
