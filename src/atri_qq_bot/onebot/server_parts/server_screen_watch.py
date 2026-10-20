"""OneBot 层的屏幕看护指令与后台监控循环（主人限定）。

指令（仅主人 QQ 生效，在回复门控之前拦截，不要求 @）：
- 「看看我屏幕」：即时截图 + 视觉描述；
- 「我刚才在干嘛 / 我在干嘛」：读取近期屏幕摘要；
- 「你爸爸在干嘛 / 爸爸现在在干嘛」等：即时截图 + 亚托莉口吻回答电脑
  在做什么（模型生成，账号称呼规则注入；「你爸爸」是 owner 专属自称）；
- 「你爸爸刚才/最近在干嘛」：读取近期屏幕摘要；
- 「屏幕监控 状态」：监控运行状态报告；
- 「屏幕监控 开启 / 停止」：切换后台实时监控（标志文件级急停）；
- 「看着我xx / 看我做xx / 围观我xx」：开启围观会话——画面显著变化
  时向当前会话发送 ATRI 口吻短评，直至主人说出结束话语；
- 主人宣告活动（如「我要去写论文了」）且 ATRI 判定可围观时，
  也会主动开启围观（LLM 分类 + 冷却控制）。

降级原则：截图或视觉失败时坦诚说明，绝不编造屏幕内容。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ...bilibili.parsing import strip_address_prefix
from ...group_chat import GROUP_PROMPT
from ...message_plan import OutgoingMessage
from ...persona.core import PERSONA_PROMPT
from ...screen_watch import ScreenWatcher, WatchParty
from ...screen_watch.party import (
    classify_activity,
    generate_closing_reply,
    generate_failure_farewell,
    generate_look_reply,
    generate_opening_reply,
    looks_like_activity_announce,
    should_end_party,
)
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

# 指令统一按「剥离称呼前缀 + 去空白 + 剥尾部语气词」后的规范化形态匹配：
# 群里主人常用「atri看看我的屏幕」这类前缀，QQ 打字常不带空格、带语气词。
_LOOK_COMMANDS = (
    "看看我屏幕",
    "看看我的屏幕",
    "看着我屏幕",
    "看着我的屏幕",
    "看我屏幕",
    "看我的屏幕",
    "看看我桌面",
    "看看我的桌面",
    "看着我桌面",
    "看着我的桌面",
    "看我桌面",
    "看我的桌面",
    "瞅瞅我屏幕",
    "瞅瞅我的屏幕",
)
_RECENT_COMMANDS = (
    "我刚才在干嘛",
    "我在干嘛",
    "我现在在干嘛",
    "我最近在干嘛",
    "我刚才做了什么",
    "我刚才在做什么",
    "你现在在干嘛",
    "你刚才在干嘛",
    "刚才在干嘛",
)
# 「你爸爸」是 owner 的专属自称变体（endswith 匹配下「爸爸在干嘛」
# 同时命中「你爸爸在干嘛」），owner 门控保证其他账号说的话不会被拦。
# 爸爸变体必须先于普通 look/recent 匹配：「爸爸刚才在干嘛」会 endswith
# 命中 look 变体「爸爸在干嘛」，先按时间词分流避免误路由成即时截图。
_DADDY_RECENT_COMMANDS = (
    "爸爸刚才在干嘛",
    "爸爸刚才在做什么",
    "爸爸最近在干嘛",
    "爸爸最近在做什么",
)
_DADDY_LOOK_COMMANDS = (
    "爸爸在干嘛",
    "爸爸在做什么",
    "爸爸现在在干嘛",
    "爸爸现在在做什么",
)
_STATUS_COMMANDS = ("屏幕监控状态", "屏幕监护状态")
_ENABLE_COMMANDS = ("屏幕监控开启", "屏幕监控打开", "屏幕监控恢复", "开启屏幕监控")
_PAUSE_COMMANDS = ("屏幕监控停止", "屏幕监控暂停", "屏幕监控关闭", "停止屏幕监控")

# 尾部语气词与标点：指令识别时剥掉，让「看看我的屏幕呀」也能命中。
# 注意「嘛」不能列入：全部「在干嘛」类指令以实义「嘛」结尾。
_TAIL_TRIM_RE = re.compile(r"[?？!！。.，,、~～呀呢啊吧哦啦呗\s]+$")
# 群聊手打的「@亚托莉」「@ATRI」等 @昵称 前缀：共享剥离函数只认 CQ 式
# @QQ号 与纯称呼，@+昵称 在这里先剥一层。
_AT_NICK_RE = re.compile(r"^@\S{1,24}[\s，。,、；;！!？?~～]+")

_RECENT_LIMIT = 3
_RECENT_MAX_AGE_HOURS = 4.0


def _normalize_command(text: str) -> str:
    """剥离 @昵称/称呼前缀、尾部语气词/标点，并移除全部空白后的指令形态。"""
    stripped = _AT_NICK_RE.sub("", (text or "").strip(), count=1)
    stripped = strip_address_prefix(stripped)
    stripped = _TAIL_TRIM_RE.sub("", stripped)
    return "".join(stripped.split())


def _match_watch_command(normalized: str) -> str | None:
    """把规范化文本映射到指令类别：look/recent/status/enable/pause。

    只读指令允许 endswith 命中，容忍「帮我看看我的屏幕」这类口语前缀；
    enable/pause 会改变监控状态，保持全等匹配，避免「不要停止屏幕监控」
    这类否定句误触发。爸爸变体先于普通指令匹配（时间词分流）。
    """
    if any(
        normalized == cmd or normalized.endswith(cmd)
        for cmd in _DADDY_RECENT_COMMANDS
    ):
        return "recent"
    if any(
        normalized == cmd or normalized.endswith(cmd)
        for cmd in _LOOK_COMMANDS + _DADDY_LOOK_COMMANDS
    ):
        return "look"
    if any(normalized == cmd or normalized.endswith(cmd) for cmd in _RECENT_COMMANDS):
        return "recent"
    if any(normalized == cmd or normalized.endswith(cmd) for cmd in _STATUS_COMMANDS):
        return "status"
    if normalized in _ENABLE_COMMANDS:
        return "enable"
    if normalized in _PAUSE_COMMANDS:
        return "pause"
    return None


# 围观开启前缀：剩余部分即活动名（「看着我写论文」→「写论文」）。
_PARTY_START_PREFIXES = ("看着我", "看我做", "围观我")


def _parse_party_activity(normalized: str) -> str:
    """从规范化文本解析围观活动名；无法解析或超长时返回空串。"""
    if not normalized or len(normalized) > 30:
        return ""
    for prefix in _PARTY_START_PREFIXES:
        if normalized.startswith(prefix):
            activity = normalized[len(prefix) :].strip()
            if activity and len(activity) <= 24:
                return activity
            return ""
    return ""


def _time_text(ts: float) -> str:
    if not ts:
        return "时间未知"
    minutes = max(0, int((time.time() - float(ts)) / 60.0))
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    return f"{minutes // 60} 小时前"


class OneBotScreenWatchMixin(OneBotServerMixin):
    @property
    def screen_watch(self) -> ScreenWatcher | None:
        if not bool(getattr(self.config, "screen_watch_enabled", False)):
            return None
        watcher = getattr(self, "_screen_watcher", None)
        if watcher is None:
            watcher = ScreenWatcher(self.config)
            self._screen_watcher = watcher
        return watcher

    async def screen_watch_loop(self) -> None:
        """后台实时监控循环：1fps 差分采样，触发时调用视觉模型。"""
        watcher = self.screen_watch
        if watcher is None:
            return
        if not bool(getattr(self.config, "screen_watch_monitor_enabled", False)):
            LOGGER.info("屏幕监控未启用（SCREEN_WATCH_MONITOR_ENABLED=false），仅保留指令能力")
            return
        interval = max(
            1.0, float(getattr(self.config, "screen_watch_interval_seconds", 1.0) or 1.0)
        )
        LOGGER.info("屏幕实时监控启动：采样间隔 %.1fs", interval)
        while True:
            try:
                await asyncio.sleep(interval)
                result = await asyncio.to_thread(watcher.tick)
                event = result.get("event")
                if event == "summarized":
                    LOGGER.info(
                        "屏幕摘要已入库：%s", str(result.get("summary", ""))[:80]
                    )
                elif event == "sensitive":
                    LOGGER.info("敏感窗口，跳过屏幕摘要：%s", result.get("window_title", ""))
                elif event == "vision_failed":
                    LOGGER.warning("屏幕视觉描述失败，本轮丢弃（不编造内容）")
                elif event == "capture_failed":
                    LOGGER.warning("屏幕截图失败（多显示器/权限环境可能不支持）")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("屏幕监控循环异常，10 秒后重试")
                await asyncio.sleep(10.0)

    async def _maybe_handle_screen_watch_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        watcher = self.screen_watch
        if watcher is None:
            return False
        try:
            user_id = int(event.get("user_id") or 0)
        except (TypeError, ValueError):
            return False
        if user_id not in {int(qq) for qq in getattr(self.config, "owner_qqs", ())}:
            return False

        try:
            normalized = _normalize_command(str(plain_text or ""))
            category = _match_watch_command(normalized)
            if category is None:
                return await self._maybe_handle_watch_party(
                    websocket,
                    event,
                    normalized,
                    conversation_id,
                    str(plain_text or ""),
                )
            if category == "look":
                await self._screen_reply(
                    websocket,
                    event,
                    await asyncio.to_thread(watcher.look_now),
                    manual=True,
                    plain_text=str(plain_text or ""),
                    conversation_id=conversation_id,
                )
            elif category == "recent":
                await self._screen_recent_reply(websocket, event, watcher)
            elif category == "status":
                await self._screen_status_reply(websocket, event, watcher)
            else:
                await self._screen_toggle_reply(
                    websocket, event, watcher, enable=category == "enable"
                )
            return True
        except Exception:
            # 看护链路自身故障不能拖垮整条消息：记录后放行到聊天回复流程。
            LOGGER.exception("屏幕看护指令处理异常，放行到聊天回复流程")
            return False

    async def _maybe_handle_watch_party(
        self,
        websocket: Any,
        event: dict[str, Any],
        normalized: str,
        conversation_id: str,
        plain_text: str,
    ) -> bool:
        """围观分支：结束话语 / 显式开启前缀 / 主人活动宣告的主动围观。"""
        if not bool(getattr(self.config, "screen_watch_party_enabled", False)):
            return False
        party = getattr(self, "_watch_party", None)
        # 围观活跃时优先识别结束话语：宽松规则，误停成本远低于漏停。
        if party is not None and should_end_party(normalized):
            self._watch_party = None
            text = await asyncio.to_thread(
                generate_closing_reply,
                self.config,
                party.activity,
                party.comment_count,
                party.persona_system,
            )
            await self._send_screen_text(websocket, event, text)
            return True
        activity = _parse_party_activity(normalized)
        if activity:
            await self._watch_party_start_reply(websocket, event, activity, conversation_id)
            return True
        # 主动围观：主人宣告活动且当前没有围观，LLM 分类（冷却控频）。
        if party is None and looks_like_activity_announce(normalized):
            cooldown = float(
                getattr(self.config, "screen_watch_party_announce_cooldown_seconds", 600.0)
                or 600.0
            )
            now = time.time()
            if now >= getattr(self, "_party_classify_after", 0.0):
                self._party_classify_after = now + cooldown
                verdict = await asyncio.to_thread(classify_activity, plain_text, self.config)
                if (
                    verdict
                    and verdict.get("announce")
                    and verdict.get("watchable")
                    and verdict.get("activity")
                ):
                    await self._watch_party_start_reply(
                        websocket, event, str(verdict["activity"]), conversation_id
                    )
                    return True
        return False

    def _party_persona_system(self, conversation_id: str) -> list[str]:
        """围观回应生成所需的 system 消息：人格 + 场景规则 + 记忆背景 + 称呼规则。

        称呼是账号级的：私聊围观只注入该账号自己会话的记忆档案
        （含其专属称呼），换一个账号就换一个称呼，不存在全局称呼；
        群聊围观按人格契约不注入任何人的私聊档案——私聊专属称呼
        不得带进群聊，用平等群友口吻。渲染失败时静默退化为仅人格
        提示词，绝不让围观链路断掉。
        """
        systems = [PERSONA_PROMPT]
        if str(conversation_id or "").startswith("group:"):
            systems.append(GROUP_PROMPT)
            systems.append(
                "称呼规则：这是群聊围观，不要使用对方在私聊里设定的专属称呼，"
                "不要暴露你们私聊里的私人关系细节；按群聊平等口吻直接用「你」。"
                "绝对禁止在输出中出现「主人」这个词。"
            )
            return systems
        memory = getattr(self.reply_engine, "memory", None)
        if memory is not None:
            try:
                recall = memory.recall_context(conversation_id, "")
            except Exception:
                recall = ""
            if recall:
                systems.append(f"关于对方的记忆背景：\n{recall}")
        systems.append(
            "称呼规则：称呼对方时，使用上面记忆背景里对方设定的称呼"
            "（记忆里写着对方希望被叫作什么，就那样称呼）；"
            "没有明确称呼就直接用「你」，不要编造新称呼。"
            "绝对禁止在输出中出现「主人」这个词。"
        )
        return systems

    async def _watch_party_start_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        activity: str,
        conversation_id: str,
    ) -> None:
        """开启围观会话；已有会话时直接顶掉（同一时间只围观一件事）。"""
        previous = getattr(self, "_watch_party", None)
        persona_system = self._party_persona_system(conversation_id)
        self._watch_party = WatchParty(
            self.config,
            activity,
            event,
            conversation_id,
            persona_system=persona_system,
        )
        text = await asyncio.to_thread(
            generate_opening_reply,
            self.config,
            activity,
            persona_system,
            replaced=previous is not None,
        )
        LOGGER.info(
            "围观开启：activity=%s conversation=%s replaced=%s",
            activity,
            conversation_id,
            previous is not None,
        )
        await self._send_screen_text(websocket, event, text)

    async def _send_party_text(
        self, websocket: Any, party: WatchParty, text: str
    ) -> None:
        try:
            await self.send_reply(
                websocket,
                party.event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("围观评论发送失败：%s", party.conversation_id)

    async def watch_party_loop(self) -> None:
        """围观评论循环：独立于入库监控，慢采样 + 帧差分确认后才发短评。"""
        if not bool(getattr(self.config, "screen_watch_party_enabled", False)):
            return
        sample = max(
            5.0,
            float(getattr(self.config, "screen_watch_party_sample_seconds", 10.0) or 10.0),
        )
        LOGGER.info(
            "围观评论循环就绪：采样 %.0fs，最小评论间隔 %.0fs",
            sample,
            float(
                getattr(self.config, "screen_watch_party_min_interval_seconds", 180.0)
                or 180.0
            ),
        )
        while True:
            try:
                await asyncio.sleep(sample)
                party = getattr(self, "_watch_party", None)
                if party is None:
                    continue
                watcher = self.screen_watch
                if watcher is not None and watcher.pause_flag.is_set():
                    continue  # 文件级急停：完全不看屏，但围观会话保留。
                result = await asyncio.to_thread(party.tick)
                event_name = str(result.get("event") or "")
                if event_name == "comment":
                    websocket = self._first_active_websocket()
                    if websocket is None:
                        LOGGER.info("围观评论就绪但没有活跃连接，本轮丢弃：%s", party.conversation_id)
                        continue
                    LOGGER.info(
                        "围观评论 #%d：%s", party.comment_count, str(result.get("text") or "")[:60]
                    )
                    await self._send_party_text(
                        websocket, party, str(result.get("text") or "")
                    )
                elif event_name == "failed_stop":
                    websocket = self._first_active_websocket()
                    failures = int(result.get("failures") or 0)
                    self._watch_party = None
                    LOGGER.warning("围观连续失败 %s 次，自动收工", failures)
                    if websocket is not None:
                        text = await asyncio.to_thread(
                            generate_failure_farewell,
                            self.config,
                            failures,
                            party.persona_system,
                        )
                        await self._send_party_text(websocket, party, text)
                elif event_name == "sensitive":
                    LOGGER.info("围观遇到敏感窗口，本轮跳过：%s", result.get("window_title", ""))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("围观评论循环异常，30 秒后重试")
                await asyncio.sleep(30.0)

    async def _send_screen_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        await self.send_reply(
            websocket,
            event,
            [OutgoingMessage("text", text)],
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )

    async def _screen_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        result: dict[str, Any],
        *,
        manual: bool,
        plain_text: str = "",
        conversation_id: str = "",
    ) -> None:
        outcome = result.get("event")
        if outcome == "paused":
            await self._send_screen_text(websocket, event, "看屏功能现在是暂停状态哦。说「屏幕监控 开启」就能恢复我~")
        elif outcome == "sensitive":
            await self._send_screen_text(
                websocket,
                event,
                "（盯着屏幕看了一眼又移开视线）你现在开着的窗口有点敏感，我就不细看了。",
            )
        elif outcome == "summarized":
            summary = str(result.get("summary", "")).strip()
            text = f"（凑近屏幕看了看）我看到的画面是：{summary}"
            if "爸爸" in str(plain_text or "") and bool(
                getattr(self.config, "screen_watch_party_enabled", False)
            ):
                # 「你爸爸在干嘛」类指令：回答交给模型生成（与围观评论同构，
                # 注入账号称呼规则），失败退回固定文案，绝不让指令断掉。
                try:
                    generated = await asyncio.to_thread(
                        generate_look_reply,
                        self.config,
                        summary,
                        self._party_persona_system(conversation_id),
                    )
                    if generated:
                        text = generated
                except Exception:
                    LOGGER.exception("「爸爸在干嘛」回答生成失败，退回固定文案")
            await self._send_screen_text(websocket, event, text)
        else:
            await self._send_screen_text(
                websocket,
                event,
                "呜……我想看你的屏幕，但截图或识别失败了。等一下再试试吧，我可不随便编一个糊弄你。",
            )

    async def _screen_recent_reply(
        self, websocket: Any, event: dict[str, Any], watcher: ScreenWatcher
    ) -> None:
        rows = watcher.store.recent(limit=_RECENT_LIMIT, max_age_hours=_RECENT_MAX_AGE_HOURS)
        if not rows:
            await self._send_screen_text(
                websocket,
                event,
                "唔，最近我没有记录到你的屏幕动态。可以先用「屏幕监控 开启」让我开始看护，或者现在就对我说「看看我屏幕」。",
            )
            return
        lines = ["最近的屏幕动态记录："]
        for row in rows:
            lines.append(f"· {_time_text(float(row.get('created_at') or 0))}：{row.get('summary', '')}")
        lines.append("（摘要只在我注意到画面切换时记录，中间的细节我就没偷看啦）")
        await self._send_screen_text(websocket, event, "\n".join(lines))

    async def _screen_status_reply(
        self, websocket: Any, event: dict[str, Any], watcher: ScreenWatcher
    ) -> None:
        status = watcher.status()
        paused = bool(status["paused"])
        lines = [
            f"屏幕看护状态：{'已暂停' if paused else '监控中'}",
            f"· 今日摘要 {int(status['today_count'])} 条",
            f"· 触发冷却 {int(status['current_cooldown'])} 秒（基础 {int(status['base_cooldown'])} 秒）",
        ]
        if status["latest_summary"]:
            lines.append(f"· 最近一次：{_time_text(status['latest_at'])} {str(status['latest_summary'])[:60]}")
        await self._send_screen_text(websocket, event, "\n".join(lines))

    async def _screen_toggle_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        watcher: ScreenWatcher,
        *,
        enable: bool,
    ) -> None:
        if enable:
            if not bool(getattr(self.config, "screen_watch_monitor_enabled", False)):
                await self._send_screen_text(
                    websocket,
                    event,
                    "指令收到，但配置里 SCREEN_WATCH_MONITOR_ENABLED 还是关闭的，改完 .env 重启我之后就能开始看护啦。",
                )
                return
            watcher.pause_flag.clear()
            await self._send_screen_text(websocket, event, "（认真点头）好，我会一直看着屏幕的。放心，敏感窗口我会自动回避~")
        else:
            watcher.pause_flag.set()
            await self._send_screen_text(websocket, event, "（默默移开视线）知道了，我先不看了。")
