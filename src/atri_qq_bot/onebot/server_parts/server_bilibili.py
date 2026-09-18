"""OneBot 层的 B站视频助手集成：命令拦截与自动总结。

「B站总结 <链接>」命令与漫画命令同级的确定性入口（在聊天模型之前）；
纯链接/分享卡片的自动总结发生在回复门控之前——分享视频的人通常
不会 @ 机器人。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...bilibili import get_bilibili_service
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_BILIBILI_NOTICE_PREFIX = "⏳ 正在解析视频并生成总结，请稍候（无字幕视频需转写，约 1-3 分钟）…"


class OneBotBilibiliMixin(OneBotServerMixin):
    @property
    def bilibili(self) -> Any | None:
        if not bool(getattr(self.config, "bilibili_enabled", True)):
            return None
        service = getattr(self, "_bilibili_service", None)
        if service is None:
            service = get_bilibili_service(self.config)
            self._bilibili_service = service
        return service

    async def _maybe_handle_bilibili_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中「B站总结/解析/帮助」命令时完成处理并返回 True。"""
        service = self.bilibili
        if service is None or not service.is_command(plain_text):
            return False

        timeout = float(getattr(self.config, "bilibili_timeout_seconds", 300.0))
        if self._bilibili_is_summary_command(plain_text) and service.cooldown_remaining(
            event.get("user_id"), conversation_id
        ) <= 0:
            await self._send_bilibili_text(websocket, event, _BILIBILI_NOTICE_PREFIX)

        try:
            result = await asyncio.wait_for(
                service.handle_command(
                    plain_text, conversation_id, user_id=event.get("user_id")
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._send_bilibili_text(websocket, event, "B站总结处理超时，请稍后重试。")
            return True
        except Exception:
            LOGGER.exception("B站命令处理异常：conversation=%s", conversation_id)
            await self._send_bilibili_text(websocket, event, "B站命令执行失败。")
            return True

        await self.send_reply(
            websocket,
            event,
            result.messages,
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        reply_text = result.record_reply_text or "[B站助手回复]"
        self.reply_engine.record_bot_reply(conversation_id, reply_text, False)
        return True

    async def _maybe_auto_summarize_bilibili(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """回复门控之前的纯链接/分享卡片自动总结；不命中返回 False 放行。"""
        service = self.bilibili
        if service is None:
            return False

        result = None
        try:
            result = await service.auto_summarize(event, plain_text, conversation_id)
        except Exception:
            LOGGER.exception("B站自动总结异常：conversation=%s", conversation_id)
            return False
        if result is None:
            return False

        await self.send_reply(
            websocket,
            event,
            result.messages,
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        reply_text = result.record_reply_text or "[B站视频总结]"
        self.reply_engine.record_bot_reply(conversation_id, reply_text, False)
        return True

    @staticmethod
    def _bilibili_is_summary_command(plain_text: str) -> bool:
        from ...bilibili.service import SUMMARY_PREFIXES

        stripped = (plain_text or "").strip()
        return any(stripped.startswith(prefix) for prefix in SUMMARY_PREFIXES)

    async def _send_bilibili_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        from ...message_plan.schema import OutgoingMessage

        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("B站提示发送失败")
