"""OneBot 层的漫画助手集成：命令拦截、章节投递、更新推送循环。

漫画命令在聊天模型之前处理（与点歌同级的确定性入口）；LLM 工具受理的
章节发送请求由本层在文字回复后消费并真正下载、打包、上传。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from ...manga import MangaDeliveryRequest, get_manga_service
from ...message_plan import OutgoingMessage, outgoing_to_onebot_message
from .server_support import OneBotServerMixin, _sync_server_globals

LOGGER = logging.getLogger("atri.onebot")

_MANGA_COMMAND_TIMEOUT_SECONDS = 300.0
_MANGA_PUSH_SEND_GAP_SECONDS = 2.0


class OneBotMangaMixin(OneBotServerMixin):
    @property
    def manga(self) -> Any | None:
        service = getattr(self, "_manga_service", None)
        if service is None:
            service = get_manga_service(self.config)
            self._manga_service = service
        return service

    async def _maybe_handle_manga_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中「漫画 xxx」命令时完成处理并返回 True，调用方直接结束本轮。"""
        service = self.manga
        if service is None or not service.is_command(plain_text):
            return False

        try:
            result = await asyncio.wait_for(
                service.handle_command(plain_text, conversation_id),
                timeout=_MANGA_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._send_manga_text(websocket, event, "漫画命令处理超时，请稍后重试。")
            return True
        except Exception as exc:
            LOGGER.exception("漫画命令处理异常：conversation=%s", conversation_id)
            await self._send_manga_text(websocket, event, "漫画命令执行失败。")
            return True

        delivered_text = await self.send_reply(
            websocket,
            event,
            result.messages,
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        self.reply_engine.record_bot_reply(
            conversation_id,
            delivered_text or "[漫画助手回复]",
            any(message.kind in {"image", "file"} for message in result.messages),
        )

        for tmp_dir in result.cleanup_dirs:
            from ...manga.downloader import schedule_cleanup

            schedule_cleanup(tmp_dir, delay=result.cleanup_delay)

        for notification in result.notifications:
            with contextlib.suppress(Exception):
                await self._deliver_manga_notification(websocket, notification)
        return True

    async def _send_manga_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("漫画错误提示发送失败")

    async def _deliver_pending_manga(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
    ) -> None:
        """消费 LLM 工具受理的章节发送请求，下载打包后上传。"""
        request = self.reply_engine.consume_manga_delivery(conversation_id)
        if request is None or self.manga is None:
            return
        service = self.manga
        try:
            await asyncio.wait_for(
                self._execute_manga_delivery(websocket, event, service, request),
                timeout=_MANGA_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("漫画章节投递超时：conversation=%s", conversation_id)
            await self._send_manga_text(websocket, event, "章节加载超时了，请稍后再试。")
        except Exception as exc:
            LOGGER.exception("漫画章节投递失败：conversation=%s", conversation_id)
            await self._send_manga_text(websocket, event, f"章节发送失败：{exc}")

    async def _execute_manga_delivery(
        self,
        websocket: Any,
        event: dict[str, Any],
        service: Any,
        request: MangaDeliveryRequest,
    ) -> None:
        manga = await service.client.get_manga(request.manga_id)
        chapters = await service.get_or_fetch_chapters(request.manga_id)
        target = next((ch for ch in chapters if ch.id == request.chapter_id), None)
        if target is None:
            await self._send_manga_text(websocket, event, "指定章节已失效。")
            return

        if request.fmt == "image":
            from ...manga.service import fmt_chapter_display

            paths, tmp_dir = await service.prepare_chapter_images(target.id)
            if not paths:
                await self._send_manga_text(websocket, event, "该章节图片加载失败。")
                return
            messages = [OutgoingMessage("text", f"📖「{manga.title}」{fmt_chapter_display(target)}")]
            messages.extend(OutgoingMessage("image", path) for path in paths)
            try:
                await self.send_reply(
                    websocket, event, messages,
                    model_type="chat", model_name=self._delivery_model_name("chat"),
                )
            finally:
                from ...manga.downloader import schedule_cleanup

                schedule_cleanup(tmp_dir, delay=60)
            return

        output_path, tmp_dir, actual_fmt = await service.prepare_chapter_file(
            manga, target, request.fmt
        )
        if output_path is None:
            await self._send_manga_text(websocket, event, "该章节图片下载失败，无法打包。")
            return
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("file", str(output_path))],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        finally:
            from ...manga.downloader import schedule_cleanup

            schedule_cleanup(tmp_dir, delay=180)

    # ── 主动推送（更新通知 + 自动推送内容） ────────────────────────

    async def manga_update_loop(self) -> None:
        interval_minutes = int(getattr(self.config, "manga_check_interval_minutes", 60))
        interval = max(5, interval_minutes) * 60
        LOGGER.info("漫画更新循环已启动，间隔 %s 分钟", interval // 60)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._manga_update_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error("漫画更新检查失败：%s", exc)

    async def _manga_update_tick(self) -> None:
        service = self.manga
        if service is None:
            return
        websocket = self._first_active_websocket()
        if websocket is None:
            return
        notifications = await service.check_updates(force=True)
        for notification in notifications:
            try:
                await self._deliver_manga_notification(websocket, notification)
            except Exception as exc:
                LOGGER.warning(
                    "漫画更新推送到 %s 失败: %s", notification.conversation_id, exc
                )

    async def _deliver_manga_notification(self, websocket: Any, notification: Any) -> None:
        if not notification.items:
            return
        lines: list[str] = []
        if len(notification.items) == 1:
            item = notification.items[0]
            lines.append(f"📢「{item['title']}」更新了！")
            lines.append(f"新增章节：{', '.join(item['chapters'])}")
            lines.append(f"发送「漫画 阅读 {item['title']} {item['latest_num']}」开始阅读")
        else:
            lines.append(f"📢 {len(notification.items)} 部漫画更新了")
            for item in notification.items:
                lines.append(f"「{item['title']}」: {', '.join(item['chapters'])}")
        await self._send_to_conversation(
            websocket, notification.conversation_id, OutgoingMessage("text", "\n".join(lines))
        )

        if self.manga is None or not notification.push_chapters:
            return
        push_mode = str(getattr(self.config, "manga_auto_push_mode", "image"))
        for manga_id, title, chapter in notification.push_chapters:
            try:
                if push_mode == "file":
                    await self._push_chapter_file(websocket, notification.conversation_id, manga_id, title, chapter)
                else:
                    await self._push_chapter_images(websocket, notification.conversation_id, manga_id, title, chapter)
                await asyncio.sleep(_MANGA_PUSH_SEND_GAP_SECONDS)
            except Exception as exc:
                LOGGER.warning(
                    "自动推送「%s」到 %s 失败: %s", title, notification.conversation_id, exc
                )

    async def _push_chapter_images(
        self,
        websocket: Any,
        conversation_id: str,
        manga_id: int,
        title: str,
        chapter: Any,
    ) -> None:
        from ...manga.service import fmt_chapter_display

        service = self.manga
        paths, tmp_dir = await service.prepare_chapter_images(chapter.id)
        try:
            if not paths:
                await self._send_to_conversation(
                    websocket,
                    conversation_id,
                    OutgoingMessage(
                        "text", f"📖「{title}」{fmt_chapter_display(chapter)}已更新，但图片发送失败。"
                    ),
                )
                return
            header = OutgoingMessage("text", f"📖「{title}」{fmt_chapter_display(chapter)}")
            await self._send_to_conversation(websocket, conversation_id, header)
            for path in paths:
                await self._send_to_conversation(
                    websocket, conversation_id, OutgoingMessage("image", path)
                )
        finally:
            from ...manga.downloader import schedule_cleanup

            schedule_cleanup(tmp_dir, delay=60)

    async def _push_chapter_file(
        self,
        websocket: Any,
        conversation_id: str,
        manga_id: int,
        title: str,
        chapter: Any,
    ) -> None:
        from ...manga.service import fmt_chapter_display

        service = self.manga
        manga = await service.client.get_manga(manga_id)
        fmt = str(getattr(self.config, "manga_download_format", "pdf"))
        output_path, tmp_dir, _actual = await service.prepare_chapter_file(manga, chapter, fmt)
        try:
            if output_path is None:
                await self._send_to_conversation(
                    websocket,
                    conversation_id,
                    OutgoingMessage(
                        "text", f"📖「{title}」{fmt_chapter_display(chapter)}已更新，但文件发送失败。"
                    ),
                )
                return
            await self._send_to_conversation(
                websocket, conversation_id, OutgoingMessage("file", str(output_path))
            )
        finally:
            from ...manga.downloader import schedule_cleanup

            schedule_cleanup(tmp_dir, delay=180)

    async def _send_to_conversation(
        self, websocket: Any, conversation_id: str, message: OutgoingMessage
    ) -> bool:
        """向 conversation_id（group:/private: 前缀）直接发送一条消息。"""
        try:
            if conversation_id.startswith("group:"):
                group_id = conversation_id.split(":", 1)[1]
                if message.kind == "file":
                    result = await self.call_action_and_wait(
                        websocket,
                        "upload_group_file",
                        {
                            "group_id": group_id,
                            "file": message.content,
                            "name": Path(message.content).name,
                        },
                        120.0,
                    )
                else:
                    result = await self.call_action_and_wait(
                        websocket,
                        "send_group_msg",
                        {"group_id": group_id, "message": outgoing_to_onebot_message(message)},
                        12.0,
                    )
                self._require_message_delivery(result)
                return True
            if conversation_id.startswith("private:"):
                user_id = conversation_id.split(":", 1)[1]
                if message.kind == "file":
                    result = await self.call_action_and_wait(
                        websocket,
                        "upload_private_file",
                        {
                            "user_id": user_id,
                            "file": message.content,
                            "name": Path(message.content).name,
                        },
                        120.0,
                    )
                else:
                    result = await self.call_action_and_wait(
                        websocket,
                        "send_private_msg",
                        {"user_id": user_id, "message": outgoing_to_onebot_message(message)},
                        12.0,
                    )
                self._require_message_delivery(result)
                return True
        except Exception as exc:
            LOGGER.warning("漫画推送发送失败：conversation=%s error=%s", conversation_id, exc)
            return False
        return False
