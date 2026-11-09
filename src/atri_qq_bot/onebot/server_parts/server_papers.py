"""OneBot 层的论文整理投递：compile_paper_digest 生成的文档发送。

文档由聊天模型通过 compile_paper_digest 工具触发（自然语言指令），
工具执行时已落盘到 data/runtime/paper_digests 并排入待投递队列；
本层在文字回复发出后消费队列真正发文件（与 minimax/anima 同一模式）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")


class OneBotPapersMixin(OneBotServerMixin):
    async def _maybe_queue_citation_digest(
        self,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> None:
        """题录概要确定性侧挂入口：不消耗消息、不经聊天模型。

        用户消息里带文献标识（DOI / doi.org / arxiv 链接或编号）且表达
        概要/整理/文档输出意图时，直接按标识符从 OpenAlex 抓取该篇
        元数据生成概要文档入队；文字回复照常走模型，文件在回复发出
        后投递。文档是否送达不再依赖模型是否调用工具。
        """
        from ...config.env import _project_root as _project_root_for_digest
        from ...papers import build_paper_digest, get_papers_service
        from ...papers.citation import parse_citation_digest_request

        identifier = parse_citation_digest_request(str(plain_text or ""))
        if identifier is None:
            return
        service = get_papers_service(self.config)
        if service is None:
            return
        try:
            # 侧挂抓取串行在回复生成前，加硬上限防止网络黑洞拖慢聊天。
            result = await asyncio.wait_for(service.fetch_work(identifier), timeout=15.0)
        except Exception as exc:
            LOGGER.warning("论文题录抓取异常：identifier=%s error=%s", identifier, exc)
            return
        if not result.get("ok"):
            LOGGER.warning(
                "论文题录抓取失败：identifier=%s error=%s",
                identifier,
                result.get("error"),
            )
            return
        paper = result.get("paper") or {}
        title = str(paper.get("title") or "").strip()
        if not title:
            return
        output_dir = _project_root_for_digest() / "data" / "runtime" / "paper_digests"
        try:
            digest = build_paper_digest(
                query=title[:80],
                sort="relevance",
                papers=[paper],
                summary="",
                output_dir=output_dir,
            )
        except Exception:
            LOGGER.exception("论文题录概要文档生成失败：identifier=%s", identifier)
            return
        if not digest.get("ok"):
            LOGGER.warning(
                "论文题录概要文档生成失败：identifier=%s error=%s",
                identifier,
                digest.get("error"),
            )
            return
        service.queue_digest_delivery(conversation_id, Path(str(digest["path"])))
        LOGGER.info(
            "论文题录概要文档已入队：conversation=%s file=%s",
            conversation_id,
            digest.get("name"),
        )

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
            delivered = await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("file", str(path))],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
            if not delivered:
                LOGGER.warning(
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
