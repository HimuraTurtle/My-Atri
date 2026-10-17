"""OneBot 层的 MiniMax 绘图集成：draw_image 工具生图后的图片投递。

生图由聊天模型通过 draw_image 工具触发（自然语言指令），工具执行时
图片已落盘到 data/minimax_draw/outputs 并排入待投递队列；本层在文字
回复发出后消费队列真正发图（与 anima/manga 同一模式）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_MINIMAX_IMAGE_SEND_GAP_SECONDS = 1.0


class OneBotMinimaxDrawMixin(OneBotServerMixin):
    async def _deliver_pending_minimax_draw(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
    ) -> None:
        """消费 draw_image 排队的生成图片并发送到当前会话。"""
        from ...minimax_draw import get_minimax_draw_service

        service = get_minimax_draw_service(self.config)
        if service is None:
            return
        paths: list[Path] = service.consume_deliveries(conversation_id)
        if not paths:
            return
        for path in paths:
            try:
                await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("image", str(path))],
                    model_type="chat",
                    model_name=self._delivery_model_name("chat"),
                )
            except Exception:
                LOGGER.exception(
                    "minimax 图片投递失败：conversation=%s", conversation_id
                )
                await self._send_minimax_draw_text(
                    websocket, event, "图片发送失败，请稍后再试。"
                )
                return
            await asyncio.sleep(_MINIMAX_IMAGE_SEND_GAP_SECONDS)

    async def _send_minimax_draw_text(
        self, websocket: Any, event: dict[str, Any], text: str
    ) -> None:
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("minimax 错误提示发送失败")
