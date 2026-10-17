"""OneBot 层的头像识别命令（移植自 astrbot_plugin_shitu 的头像识别）。

确定性入口：识别目标 QQ 头像中的二次元角色，走 AnimeTrace 角色库链路。
支持「头像识别」（自己）、「头像识别 <QQ号>」、@目标 三种目标指定方式。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ...bilibili.parsing import strip_address_prefix
from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

AVATAR_COMMAND_PREFIX = "头像识别"
_AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640"
_AVATAR_COMMAND_TIMEOUT_SECONDS = 25.0
_AVATAR_COOLDOWN_SECONDS = 20.0
_AVATAR_QQ_RE = re.compile(r"\b(\d{5,12})\b")


class OneBotAvatarMixin(OneBotServerMixin):
    def _avatar_cooldown_remaining(self, conversation_id: str) -> int:
        punched_at = getattr(self, "_avatar_cooldowns", {}).get(conversation_id)
        if not punched_at:
            return 0
        remaining = _AVATAR_COOLDOWN_SECONDS - (time.time() - punched_at)
        return max(0, int(remaining + 0.999))

    def _avatar_resolve_target(self, event: dict[str, Any], stripped: str) -> str | None:
        """目标解析：@提及 > 显式 QQ 号 > 发送者自己。"""
        message = event.get("message") or []
        if isinstance(message, list):
            bot_qq = str(getattr(self.config, "bot_qq", "") or "")
            for segment in message:
                if not isinstance(segment, dict) or segment.get("type") != "at":
                    continue
                qq = str((segment.get("data") or {}).get("qq") or "")
                if qq and qq != "all" and qq != bot_qq:
                    return qq
        argument = stripped[len(AVATAR_COMMAND_PREFIX) :].strip()
        if argument:
            match = _AVATAR_QQ_RE.search(argument)
            if match:
                return match.group(1)
        sender = event.get("user_id")
        return str(sender) if sender else None

    def _avatar_format_hits(self, hits: list[Any]) -> str:
        animetrace = [hit for hit in hits if hit.provider == "animetrace"]
        if animetrace:
            lines = ["🔍 头像识别结果："]
            for index, hit in enumerate(animetrace, start=1):
                name = hit.characters or hit.title
                line = f"{index}. {name}"
                if hit.characters and hit.title:
                    line += f" —— 《{hit.title}》"
                if hit.detail:
                    line += f"（{hit.detail}）"
                lines.append(line)
            lines.append("来源：AnimeTrace 二次元角色库，仅供参考～")
            for hit in hits:
                if hit.provider == "trace.moe":
                    lines.append(
                        f"另：头像画面与《{hit.title}》{hit.episode}某帧相似度 {hit.similarity:.0f}%。"
                    )
                    break
            return "\n".join(lines)
        for hit in hits:
            if hit.provider == "trace.moe":
                return (
                    f"🔍 头像画面与《{hit.title}》{hit.episode}的某一帧高度相似"
                    f"（相似度 {hit.similarity:.0f}%），像是动画截图头像～"
                )
            if hit.provider == "saucenao" and (hit.characters or hit.title):
                parts = []
                if hit.characters:
                    parts.append(f"角色「{hit.characters}」")
                if hit.title:
                    parts.append(f"来源《{hit.title}》")
                return f"🔍 头像识别结果：SauceNAO 找到相似度 {hit.similarity:.0f}% 的{'，'.join(parts)}～"
        return (
            "这张头像没认出来……角色库里没有检测到二次元角色，"
            "可能是真人照片、风景图，或者是还没收录的角色～"
        )

    async def _fetch_qq_avatar(self, qq: str) -> bytes:
        import httpx

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(_AVATAR_URL_TEMPLATE.format(qq=qq))
        response.raise_for_status()
        if not response.content:
            raise ValueError("头像内容为空")
        return response.content

    async def _maybe_handle_avatar_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中「头像识别」命令时完成处理并返回 True，调用方直接结束本轮。"""
        if not bool(getattr(self.config, "animetrace_enabled", True)):
            return False
        stripped = strip_address_prefix(plain_text)
        if not stripped.startswith(AVATAR_COMMAND_PREFIX):
            return False

        target = self._avatar_resolve_target(event, stripped)
        if not target:
            await self._send_avatar_text(websocket, event, "不知道要识别谁的头像～试试「头像识别 <QQ号>」或 @目标 哦。")
            return True

        remaining = self._avatar_cooldown_remaining(conversation_id)
        if remaining > 0:
            notice = f"⏳ 头像识别太频繁，请等 {remaining} 秒后再试。"
            await self._send_avatar_text(websocket, event, notice)
            return True

        if not hasattr(self, "_avatar_cooldowns"):
            self._avatar_cooldowns: dict[str, float] = {}
        self._avatar_cooldowns[conversation_id] = time.time()

        try:
            result = await asyncio.wait_for(
                self._recognize_avatar(target),
                timeout=_AVATAR_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._send_avatar_text(websocket, event, "头像识别超时，请稍后再试。")
            return True
        except Exception as exc:
            LOGGER.warning("头像识别失败：qq=%s err=%s", target, exc)
            await self._send_avatar_text(websocket, event, "头像识别失败，可能是头像下载或识别服务暂时不可用。")
            return True

        await self._send_avatar_text(websocket, event, result)
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        self.reply_engine.record_bot_reply(conversation_id, result, False)
        return True

    async def _recognize_avatar(self, qq: str) -> str:
        avatar_bytes = await self._fetch_qq_avatar(qq)
        from ...toolbox import reverse_image_search

        saucenao_key = str(getattr(self.config, "saucenao_api_key", "") or "")
        hits = await reverse_image_search(avatar_bytes, saucenao_key, True)
        return self._avatar_format_hits(hits)

    async def _send_avatar_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("头像识别回复发送失败")
