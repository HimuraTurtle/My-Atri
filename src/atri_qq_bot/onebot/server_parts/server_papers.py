"""OneBot 层的论文整理投递：compile_paper_digest 生成的文档发送。

文档由聊天模型通过 compile_paper_digest 工具触发（自然语言指令），
工具执行时已落盘到 data/runtime/paper_digests 并排入待投递队列；
本层在文字回复发出后消费队列真正发文件（与 minimax/anima 同一模式）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")


class OneBotPapersMixin(OneBotServerMixin):
    async def _deliver_pending_paper_digest(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
    ) -> None:
        """消费 compile_paper_digest 排队的文档并发送到当前会话。"""
        from ...papers import get_papers_service

        service = get_papers_service(self.config)
        if service is None:
            return
        for path in service.consume_digest_deliveries(conversation_id):
            try:
                await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("file", str(path))],
                    model_type="chat",
                    model_name=self._delivery_model_name("chat"),
                )
            except Exception:
                LOGGER.exception(
                    "论文整理文档投递失败：conversation=%s file=%s",
                    conversation_id,
                    path,
                )
                await self._send_paper_digest_text(
                    websocket, event, "文档发送失败了，稍后再让我试一次吧。"
                )
                return

    async def _send_paper_digest_text(
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
            LOGGER.exception("论文整理错误提示发送失败")
