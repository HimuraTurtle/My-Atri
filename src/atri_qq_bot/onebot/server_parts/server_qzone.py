"""OneBot 层的 QQ 空间入口：主人口令、每日自动评论与聊天随机触发。

口令与触发（沿用屏幕看护的确定性拦截风格：主人限定、门控之前、不要求 @）：
- 「发条说说」[可选正文]：仅此口令会发布说说；给了正文直接发，
  没给就按当日主题轮换由模型生成，生成稿过质量闸后才发布；
- 「更新日志」：跑一遍开发日志闭环（采集提交 → 生成手记 → 发布），
  水位、草稿与每日上限全部沿用 SQLite 状态机，绝不重复发布；
- 每日自动评论：锚点时间 ± 抖动分钟内随机执行，拉好友动态、跳过
  自己和已评论过的，由模型按动态内容生成评论；
- 聊天随机触发：亚托莉参与会话的每条消息按概率在后台评论一条好友动态。

会话安全：本模块不独立登录 QQ 空间，Cookie 通过 OneBot get_cookies
从 NapCat 按需借取，流量只在使用时产生。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import replace
from datetime import datetime, time as dt_time, timedelta
from typing import Any

from ...bilibili.parsing import strip_address_prefix
from ...message_plan import OutgoingMessage
from ...persona import AtriReplyEngine
from ..reply_policy import _as_int
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_POST_COMMANDS = ("发条说说", "发个说说", "发一条说说")
_POST_COMMAND_RE = re.compile("(?:发条说说|发个说说|发一条说说)")
_DEVLOG_COMMANDS = ("更新日志", "同步更新日志", "发布更新日志", "同步日志")
_CONTENT_SEP_RE = re.compile(r"^[\s：:，,、\-—~～]+")

_AT_NICK_RE = re.compile(r"^@\S{1,24}[\s，。,、；;！!？?~～]+")
_TAIL_TRIM_RE = re.compile(r"[?？!！。.，,、~～呀呢啊吧哦啦呗\s]+$")

_QZONE_LOOP_INTERVAL_SECONDS = 60.0
# 错过排期超过这个窗口（进程停机/断网）就不补跑，直接排下一轮。
_QZONE_CATCHUP_SECONDS = 90 * 60.0
# 两次真实执行之间的最小间隔，防止抖动排期贴着当前时刻连续触发。
_QZONE_MIN_RUN_GAP_SECONDS = 3600.0


def _normalize_command(text: str) -> str:
    stripped = _AT_NICK_RE.sub("", (text or "").strip(), count=1)
    stripped = strip_address_prefix(stripped)
    stripped = _TAIL_TRIM_RE.sub("", stripped)
    return "".join(stripped.split())


class OneBotQzoneMixin(OneBotServerMixin):
    @property
    def qzone_service(self) -> Any | None:
        """进程级单例接入；首次取用时把 Cookie 提供器与生成器接线进来。"""
        from ...qzone import get_qzone_service

        service = get_qzone_service(self.config)
        if service is None:
            return None
        if not getattr(self, "_qzone_bound", False):
            service.bind_cookie_provider(self._qzone_cookie_provider)
            service.bind_generators(
                self._qzone_generate_post,
                self._qzone_generate_comment,
            )
            self._qzone_bound = True
        return service

    # ---- NapCat 会话桥 ----

    async def _qzone_cookie_provider(self, domain: str) -> dict[str, Any]:
        websocket = self._first_active_websocket()
        if websocket is None:
            raise RuntimeError("NapCat 未连接，暂时无法访问 QQ 空间")
        result = await self.call_action_and_wait(
            websocket,
            "get_cookies",
            {"domain": domain},
            10.0,
        )
        data = result.get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) else {}

    # ---- 文本生成（说说/评论/开发手记共用回复引擎人格）----

    async def _qzone_generate_post(self, prompt: str) -> dict[str, Any]:
        return await self._qzone_generate_text(prompt, is_comment=False)

    async def _qzone_generate_comment(self, prompt: str) -> dict[str, Any]:
        return await self._qzone_generate_text(prompt, is_comment=True)

    async def _qzone_generate_text(self, prompt: str, *, is_comment: bool) -> dict[str, Any]:
        instruction = str(prompt or "").strip()
        if not instruction:
            raise ValueError("生成提示词为空")
        engine = AtriReplyEngine(
            replace(
                self.config,
                idle_proactive_enabled=False,
                group_proactive_enabled=False,
                chat_quality_retries=0,
            )
        )
        kind_text = "评论" if is_comment else "动态"
        system = (
            "你现在在为 QQ 空间自动互动生成文案。你是亚托莉，必须保持当前项目已配置的人格、记忆和情绪状态。"
            f"请根据用户提示生成一条{kind_text}，只输出可以直接发布的{kind_text}正文，"
            "不要解释、不要加引号、不要写『提示词』或『生成结果』，不得使用「主人」称呼。"
            + (
                f"{kind_text}控制在 1-80 字，自然、像真人评论。"
                if is_comment
                else "动态要贴合提示中的发布时间与主题方向，避免模板化；"
                "只能依据提示明确给出的信息，不得编造新闻、日期、人物行动或亲身经历。"
            )
        )
        reply = await engine._reply_with_guarded_api(
            "qzone:generate",
            instruction,
            "",
            extra_system=system,
        )
        reply = engine._finalize_reply(
            "qzone:generate", instruction, reply, strict_quality=False
        ).strip()
        return {"ok": True, "content": reply[: (200 if is_comment else 1000)]}

    # ---- 主人口令（确定性拦截）----

    async def _maybe_handle_qzone_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        service = self.qzone_service
        if service is None:
            return False
        try:
            user_id = _as_int(event.get("user_id"))
        except (TypeError, ValueError):
            return False
        if user_id not in {int(qq) for qq in getattr(self.config, "owner_qqs", ())}:
            return False

        raw = str(plain_text or "")
        normalized = _normalize_command(raw)
        try:
            if any(normalized == cmd or normalized.startswith(cmd) for cmd in _POST_COMMANDS):
                await self._qzone_post_command(websocket, event, raw, normalized)
                return True
            if normalized in _DEVLOG_COMMANDS:
                await self._qzone_devlog_command(websocket, event)
                return True
        except Exception:
            # 空间链路自身故障不能拖垮整条消息：记录后放行到聊天回复流程。
            LOGGER.exception("QQ 空间口令处理异常，放行到聊天回复流程")
        return False

    async def _qzone_post_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        raw: str,
        normalized: str,
    ) -> None:
        service = self.qzone_service
        explicit = ""
        parts = _POST_COMMAND_RE.split(raw, maxsplit=1)
        if len(parts) > 1:
            explicit = _CONTENT_SEP_RE.sub("", parts[1]).strip()
        elif any(normalized.startswith(cmd) and len(normalized) > len(cmd) for cmd in _POST_COMMANDS):
            for cmd in _POST_COMMANDS:
                if normalized.startswith(cmd):
                    explicit = _CONTENT_SEP_RE.sub("", normalized[len(cmd):]).strip()
                    break
        if not explicit:
            await self._qzone_send_text(websocket, event, "（想了想）等我一下，我看看最近适合发点什么~")
        result = await service.owner_publish_post(explicit or None)
        if result.get("ok"):
            content = str(result.get("data", {}).get("content") or "") or explicit
            await self._qzone_send_text(
                websocket, event, f"发好啦，已经更新到 QQ 空间：\n{content}"
            )
        else:
            await self._qzone_send_text(
                websocket,
                event,
                f"呜……这条说说没发出去：{result.get('message') or '接口未确认'}。稍后再让我试试吧。",
            )

    async def _qzone_devlog_command(self, websocket: Any, event: dict[str, Any]) -> None:
        service = self.qzone_service
        if not bool(getattr(self.config, "qzone_devlog_enabled", True)):
            await self._qzone_send_text(websocket, event, "开发日志功能现在是关闭的（QZONE_DEVLOG_ENABLED）。")
            return
        await self._qzone_send_text(websocket, event, "好，我去把最近的代码更新整理成一篇手记~")
        result = await service.devlog_cycle(force=True)
        await self._qzone_send_text(websocket, event, self._qzone_devlog_reply_text(result))

    def _qzone_devlog_reply_text(self, result: dict[str, Any]) -> str:
        scan = result.get("scan") or {}
        generate = result.get("generate") or {}
        publish = result.get("publish") or {}
        if result.get("ok") is False and result.get("skipped"):
            return f"这次没能同步：{result.get('skipped')}"
        if scan.get("initialized"):
            return (
                "刚把开发日志的采集起点对齐到最新一次提交，之前的更新我就不翻旧账啦。"
                "之后有新提交，你再对我说「更新日志」我就会同步上去~"
            )
        if not scan.get("ok"):
            return f"呜……更新记录没读出来：{scan.get('error') or '采集失败'}。稍后再试一次吧。"
        if publish.get("ok") and publish.get("content"):
            return f"开发手记已经发到空间啦：\n{publish.get('content')}"
        if generate.get("ok") and generate.get("item"):
            item = generate.get("item") or {}
            return f"手记草稿已经写好了，但还没发布：{str(item.get('content') or '').strip()[:120]}"
        if str(publish.get("skipped") or "").find("上限") >= 0:
            return "今天的开发手记已经发到每日上限啦，明天再让我同步吧~"
        if str(generate.get("skipped") or "") == "没有待写进手记的提交":
            return "最近没有新的可见提交要写进日志哦。要不改点代码再来找我？"
        if not generate.get("ok"):
            return f"手记写得不太顺利：{generate.get('skipped') or generate.get('error') or '生成失败'}。我缓一缓再试。"
        return "这次没有需要同步的更新。"

    async def _qzone_send_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        await self.send_reply(
            websocket,
            event,
            [OutgoingMessage("text", text)],
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )

    # ---- 聊天随机触发 ----

    def _maybe_trigger_random_qzone_comment(self, event: dict[str, Any], addressed: bool) -> None:
        """聊天中以概率触发一条好友动态评论；同步掷骰，动作异步后台执行。"""
        try:
            service = self.qzone_service
        except Exception:
            return
        if service is None or not addressed:
            return
        if not bool(getattr(self.config, "qzone_auto_comment_enabled", True)):
            return
        probability = float(
            getattr(self.config, "qzone_random_comment_probability", 0.001)
        )
        if probability <= 0 or random.random() >= probability:
            return
        now = time.monotonic()
        cooldown = max(
            60.0,
            float(getattr(self.config, "qzone_random_comment_cooldown_minutes", 30)) * 60.0,
        )
        if now < getattr(self, "_qzone_random_after", 0.0):
            return
        self._qzone_random_after = now + cooldown
        LOGGER.info("聊天随机触发 QQ 空间评论：user=%s", event.get("user_id"))
        task = asyncio.create_task(self._run_random_qzone_comment())
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    async def _run_random_qzone_comment(self) -> None:
        service = self.qzone_service
        if service is None:
            return
        try:
            await service.auto_comment_run(max_count=1, source="chat")
        except Exception:
            LOGGER.exception("聊天随机评论执行失败")

    # ---- 每日自动评论循环 ----

    def _qzone_anchor(self) -> tuple[int, int]:
        raw = str(getattr(self.config, "qzone_auto_comment_time", "23:30") or "23:30")
        match = re.match(r"^(\d{1,2}):(\d{1,2})$", raw.strip())
        if not match:
            return 23, 30
        hour = max(0, min(23, int(match.group(1))))
        minute = max(0, min(59, int(match.group(2))))
        return hour, minute

    def _qzone_plan_next_run(self, now: float, *, not_before: float = 0.0) -> float:
        """锚点 ± 抖动的下一个执行时刻；not_before 保证与刚执行的一轮拉开间隔。"""
        from ...qzone.rules import resolve_timezone

        zone = resolve_timezone(getattr(self.config, "qzone_timezone", "Asia/Shanghai"))
        local = datetime.fromtimestamp(float(now), zone)
        hour, minute = self._qzone_anchor()
        jitter = max(0, int(getattr(self.config, "qzone_auto_comment_jitter_minutes", 10)))

        def slot_for(day: Any) -> float:
            base = datetime.combine(day, dt_time(hour, minute), zone)
            return base.timestamp() + random.uniform(-jitter, jitter) * 60.0

        today_slot = slot_for(local.date())
        if float(now) < today_slot and today_slot > float(not_before):
            return today_slot
        tomorrow_slot = slot_for(local.date() + timedelta(days=1))
        if tomorrow_slot <= float(not_before):
            tomorrow_slot = float(not_before) + random.uniform(60.0, 300.0)
        return tomorrow_slot

    async def qzone_loop(self) -> None:
        LOGGER.info("QQ 空间循环就绪：每 60 秒检查一次自动评论排期")
        while True:
            try:
                await asyncio.sleep(_QZONE_LOOP_INTERVAL_SECONDS)
                service = self.qzone_service
                if service is None:
                    continue
                if not bool(getattr(self.config, "qzone_auto_comment_enabled", True)):
                    continue
                if self._first_active_websocket() is None:
                    continue
                now = time.time()
                settings = service.store.settings()
                next_run = float(settings.get("auto_comment_next_run") or 0)
                if next_run <= 0:
                    planned = self._qzone_plan_next_run(now)
                    service.store.save_settings({"auto_comment_next_run": planned})
                    continue
                if now < next_run:
                    continue
                if now - next_run > _QZONE_CATCHUP_SECONDS:
                    # 错过太久（比如整夜没开机）：本轮不补跑，直接排明天。
                    planned = self._qzone_plan_next_run(now)
                    service.store.save_settings({"auto_comment_next_run": planned})
                    continue
                LOGGER.info("到达自动评论排期，开始执行")
                finished_at = time.time()
                result = await service.auto_comment_run(source="cron")
                planned = self._qzone_plan_next_run(finished_at, not_before=finished_at + _QZONE_MIN_RUN_GAP_SECONDS)
                service.store.save_settings({"auto_comment_next_run": planned})
                LOGGER.info(
                    "自动评论排期执行完成：commented=%d skipped=%d",
                    int(result.get("commented_count") or 0),
                    len(result.get("skipped") or []),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("QQ 空间自动评论循环异常，1 分钟后重试")
