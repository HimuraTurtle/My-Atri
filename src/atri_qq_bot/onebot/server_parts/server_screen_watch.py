"""OneBot 层的屏幕看护指令与后台监控循环（主人限定）。

指令（仅主人 QQ 生效，在回复门控之前拦截，不要求 @）：
- 「看看我屏幕」：即时截图 + 视觉描述；
- 「我刚才在干嘛 / 我在干嘛」：读取近期屏幕摘要；
- 「屏幕监控 状态」：监控运行状态报告；
- 「屏幕监控 开启 / 停止」：切换后台实时监控（标志文件级急停）。

降级原则：截图或视觉失败时坦诚说明，绝不编造屏幕内容。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ...message_plan import OutgoingMessage
from ...screen_watch import ScreenWatcher
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_LOOK_COMMANDS = ("看看我屏幕", "看我屏幕", "看看我桌面", "看我桌面")
_RECENT_COMMANDS = ("我刚才在干嘛", "我在干嘛", "我最近在干嘛", "我刚才做了什么")
_STATUS_COMMAND = "屏幕监控 状态"
_ENABLE_COMMANDS = ("屏幕监控 开启", "屏幕监控 打开", "屏幕监控 恢复")
_PAUSE_COMMANDS = ("屏幕监控 停止", "屏幕监控 暂停", "屏幕监控 关闭")

_RECENT_LIMIT = 3
_RECENT_MAX_AGE_HOURS = 4.0


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
        text = str(plain_text or "").strip()
        if not text:
            return False
        try:
            user_id = int(event.get("user_id") or 0)
        except (TypeError, ValueError):
            return False
        if user_id not in {int(qq) for qq in getattr(self.config, "owner_qqs", ())}:
            return False

        if text in _LOOK_COMMANDS:
            await self._screen_reply(
                websocket,
                event,
                await asyncio.to_thread(watcher.look_now),
                manual=True,
            )
            return True
        if text in _RECENT_COMMANDS:
            await self._screen_recent_reply(websocket, event, watcher)
            return True
        if text == _STATUS_COMMAND:
            await self._screen_status_reply(websocket, event, watcher)
            return True
        if text in _ENABLE_COMMANDS or text in _PAUSE_COMMANDS:
            await self._screen_toggle_reply(websocket, event, watcher, enable=text in _ENABLE_COMMANDS)
            return True
        return False

    async def _send_screen_text(self, websocket: Any, event: dict[str, Any], text: str) -> None:
        await self.send_reply(
            websocket,
            event,
            [OutgoingMessage("text", text)],
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )

    async def _screen_reply(
        self, websocket: Any, event: dict[str, Any], result: dict[str, Any], *, manual: bool
    ) -> None:
        outcome = result.get("event")
        if outcome == "paused":
            await self._send_screen_text(websocket, event, "看屏功能现在是暂停状态哦。主人可以用「屏幕监控 开启」恢复我~")
        elif outcome == "sensitive":
            await self._send_screen_text(
                websocket,
                event,
                "（盯着屏幕看了一眼又移开视线）主人现在开着的窗口有点敏感，我就不细看了。",
            )
        elif outcome == "summarized":
            summary = str(result.get("summary", "")).strip()
            await self._send_screen_text(websocket, event, f"（凑近屏幕看了看）主人，我看到的画面是：{summary}")
        else:
            await self._send_screen_text(
                websocket,
                event,
                "呜……我想看主人的屏幕，但截图或识别失败了。等一下再试试吧，我可不随便编一个糊弄主人。",
            )

    async def _screen_recent_reply(
        self, websocket: Any, event: dict[str, Any], watcher: ScreenWatcher
    ) -> None:
        rows = watcher.store.recent(limit=_RECENT_LIMIT, max_age_hours=_RECENT_MAX_AGE_HOURS)
        if not rows:
            await self._send_screen_text(
                websocket,
                event,
                "唔，最近我没有记录到主人的屏幕动态。可以先用「屏幕监控 开启」让我开始看护，或者现在就对我说「看看我屏幕」。",
            )
            return
        lines = ["主人最近的屏幕动态记录："]
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
                    "主人，指令收到，但配置里 SCREEN_WATCH_MONITOR_ENABLED 还是关闭的，改完 .env 重启我之后就能开始看护啦。",
                )
                return
            watcher.pause_flag.clear()
            await self._send_screen_text(websocket, event, "（认真点头）好，我会一直看着主人的屏幕的。放心，敏感窗口我会自动回避~")
        else:
            watcher.pause_flag.set()
            await self._send_screen_text(websocket, event, "（默默移开视线）知道了主人，我先不看了。")
