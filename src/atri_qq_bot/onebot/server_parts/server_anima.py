"""OneBot 层的 Anima 绘画助手集成：LLM 工具生图后的图片投递。

生图由聊天模型通过 anima_draw 工具触发（自然语言指令），插件在
工具执行时把图片落盘到 data/anima/workspace/outputs 并排入
AnimaService 的待投递队列；本层在文字回复发出后消费队列真正发图
（与漫画章节投递同一模式）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_ANIMA_IMAGE_SEND_GAP_SECONDS = 1.0


class OneBotAnimaMixin(OneBotServerMixin):
    @property
    def anima(self) -> Any | None:
        service = getattr(self, "_anima_service", None)
        if service is None:
            from ...anima import get_anima_service

            service = get_anima_service(self.config)
            self._anima_service = service
        return service

    async def _deliver_pending_anima(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
    ) -> None:
        """消费 anima_draw 排队的生成图片并发送到当前会话。"""
        service = self.anima
        if service is None:
            return
        paths = service.consume_deliveries(conversation_id)
        if not paths:
            return
        for path in paths:
            try:
                await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("image", path)],
                    model_type="chat",
                    model_name=self._delivery_model_name("chat"),
                )
            except Exception as exc:
                LOGGER.exception(
                    "anima 图片投递失败：conversation=%s", conversation_id
                )
                await self._send_anima_text(websocket, event, f"图片发送失败：{exc}")
                return
            await asyncio.sleep(_ANIMA_IMAGE_SEND_GAP_SECONDS)

    async def _send_anima_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("anima 错误提示发送失败")
