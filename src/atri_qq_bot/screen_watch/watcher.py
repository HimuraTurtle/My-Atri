"""ScreenWatcher：一次 tick 完成采集→差分→隐私闸→视觉→入库。

tick 为同步方法且含数秒级网络调用，调用方应通过 asyncio.to_thread
执行，避免阻塞 OneBot 事件循环（QQ 消息处理与它互不影响）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..runtime.paths import DATA_DIR
from .capture import capture_screen, encode_jpeg, gray_small
from .privacy import PauseFlag, active_window_title, is_sensitive_title
from .store import ScreenWatchStore
from .trigger import FrameTrigger
from .vision import describe_screen

BUSY_WINDOW_SECONDS = 300.0
BUSY_TRIGGER_COUNT = 4


class ScreenWatcher:
    def __init__(
        self,
        config: Any,
        store: ScreenWatchStore | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.config = config
        data_dir = Path(data_dir) if data_dir is not None else Path(DATA_DIR) / "screen_watch"
        self.store = store or ScreenWatchStore(data_dir / "summaries.db")
        self.pause_flag = PauseFlag(data_dir / "PAUSED")
        self.trigger = FrameTrigger(
            threshold=float(getattr(config, "screen_watch_diff_threshold", 8.0)),
            cooldown=float(getattr(config, "screen_watch_cooldown_seconds", 120.0)),
            backoff_max=float(getattr(config, "screen_watch_backoff_max_seconds", 300.0)),
        )
        self.latest_summary: str = ""
        self.latest_at: float = 0.0
        self._fired_at: list[float] = []

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """执行一次监控周期，返回事件字典（event 见返回值）。"""
        now = time.time() if now is None else now
        if self.pause_flag.is_set():
            return {"event": "paused"}
        image = capture_screen()
        if image is None:
            return {"event": "capture_failed"}
        if not self.trigger.feed(gray_small(image), now):
            return {"event": "idle"}
        title = active_window_title()
        if is_sensitive_title(title):
            self.trigger.reset_baseline()
            return {"event": "sensitive", "window_title": title}
        text = describe_screen(encode_jpeg(image), self.config)
        if not text:
            return {"event": "vision_failed"}
        self.store.add_summary(text, kind="auto", window_title=title)
        self.latest_summary = text
        self.latest_at = now
        self.trigger.reset_baseline()
        self._record_fire(now)
        return {"event": "summarized", "summary": text, "window_title": title}

    def look_now(self) -> dict[str, Any]:
        """主人指令「看看我屏幕」：即时截图并描述（不受暂停/冷却限制）。"""
        if self.pause_flag.is_set():
            return {"event": "paused"}
        image = capture_screen()
        if image is None:
            return {"event": "capture_failed"}
        title = active_window_title()
        if is_sensitive_title(title):
            return {"event": "sensitive", "window_title": title}
        text = describe_screen(encode_jpeg(image), self.config)
        if not text:
            return {"event": "vision_failed"}
        self.store.add_summary(text, kind="manual", window_title=title)
        self.latest_summary = text
        self.latest_at = time.time()
        self.trigger.reset_baseline()
        return {"event": "summarized", "summary": text, "window_title": title}

    def _record_fire(self, now: float) -> None:
        self._fired_at.append(now)
        self._fired_at = [ts for ts in self._fired_at if now - ts <= BUSY_WINDOW_SECONDS]
        if len(self._fired_at) >= BUSY_TRIGGER_COUNT:
            self.trigger.note_busy(now)

    def status(self) -> dict[str, Any]:
        day_start = time.time() - (time.time() % 86400.0)
        return {
            "paused": self.pause_flag.is_set(),
            "current_cooldown": self.trigger.current_cooldown,
            "base_cooldown": self.trigger.base_cooldown,
            "today_count": self.store.count_since(day_start),
            "latest_summary": self.latest_summary,
            "latest_at": self.latest_at,
        }
