"""screen_watch 层测试：触发状态机、隐私闸、存取、watcher 闭环与指令权限。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.screen_watch import (
    FrameTrigger,
    PauseFlag,
    ScreenWatchStore,
    ScreenWatcher,
    encode_jpeg,
    gray_small,
    is_sensitive_title,
)
from atri_qq_bot.screen_watch.vision import describe_screen


def _config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key=None,
        openai_base_url="https://example.invalid/v1",
        openai_model="test-model",
        temperature=0.7,
        max_tokens=180,
        memory_path=tmp_path / "memory",
        owner_qqs=(100000001,),
        screen_watch_enabled=True,
        screen_watch_monitor_enabled=True,
        screen_watch_interval_seconds=1.0,
        screen_watch_diff_threshold=8.0,
        screen_watch_cooldown_seconds=120.0,
        screen_watch_backoff_max_seconds=300.0,
        toolbox_vision_model="deepseek-flash",
        toolbox_vision_base_url="https://example.invalid/v1",
        toolbox_vision_api_key="test-key",
    )


def _flat(value: int) -> np.ndarray:
    return np.full((90, 160), value, dtype=np.uint8)


class TestFrameTrigger:
    def test_first_frame_never_fires(self):
        trigger = FrameTrigger()
        assert trigger.feed(_flat(10), now=1000.0) is False

    def test_static_scene_does_not_fire(self):
        trigger = FrameTrigger()
        trigger.feed(_flat(10), now=1000.0)
        assert trigger.feed(_flat(10), now=1001.0) is False
        assert trigger.feed(_flat(10), now=1002.0) is False

    def test_scene_change_needs_confirm_frames(self):
        trigger = FrameTrigger()
        trigger.feed(_flat(10), now=1000.0)
        assert trigger.feed(_flat(200), now=1001.0) is False
        assert trigger.feed(_flat(180), now=1002.0) is True

    def test_cooldown_blocks_immediate_refire(self):
        trigger = FrameTrigger()
        trigger.feed(_flat(10), now=1000.0)
        assert trigger.feed(_flat(200), now=1001.0) is False
        assert trigger.feed(_flat(180), now=1002.0) is True
        assert trigger.feed(_flat(210), now=1003.0) is False
        assert trigger.feed(_flat(190), now=1004.0) is False

    def test_busy_backoff_extends_cooldown(self):
        trigger = FrameTrigger(cooldown=10.0, backoff_max=60.0)
        base = trigger.current_cooldown
        trigger.note_busy(now=1000.0)
        trigger.note_busy(now=1000.0)
        assert trigger.current_cooldown > base
        assert trigger.current_cooldown <= 60.0

    def test_baseline_reset_after_fire(self):
        trigger = FrameTrigger()
        trigger.feed(_flat(10), now=1000.0)
        trigger.feed(_flat(200), now=1001.0)
        assert trigger.feed(_flat(180), now=1002.0) is True
        trigger.reset_baseline()
        assert trigger.feed(_flat(180), now=1203.0) is False


class TestPrivacy:
    @pytest.mark.parametrize(
        "title",
        ["招商银行", "支付宝 - 付款", "InPrivate 浏览", "Chrome - Sign in", "网银登录"],
    )
    def test_sensitive_titles(self, title):
        assert is_sensitive_title(title) is True

    @pytest.mark.parametrize("title", ["VS Code - main.py", "哔哩哔哩 - 看视频", ""])
    def test_normal_titles(self, title):
        assert is_sensitive_title(title) is False

    def test_pause_flag_toggle(self, tmp_path: Path):
        flag = PauseFlag(tmp_path / "PAUSED")
        assert flag.is_set() is False
        flag.set()
        assert flag.is_set() is True
        flag.clear()
        assert flag.is_set() is False


class TestStore:
    def test_add_and_recent(self, tmp_path: Path):
        store = ScreenWatchStore(tmp_path / "summaries.db")
        assert store.add_summary("主人在写代码", kind="auto", window_title="VS Code") > 0
        assert store.add_summary("", kind="auto") == 0
        rows = store.recent(limit=3, max_age_hours=4)
        assert len(rows) == 1
        assert rows[0]["summary"] == "主人在写代码"
        assert rows[0]["window_title"] == "VS Code"
        assert store.count_since(time.time() - 60) == 1

    def test_recent_respects_age(self, tmp_path: Path):
        store = ScreenWatchStore(tmp_path / "summaries.db")
        store.add_summary("旧摘要")
        rows = store.recent(limit=3, max_age_hours=-0.01)
        assert rows == []


class TestVisionGuard:
    def test_missing_config_returns_empty(self, tmp_path: Path):
        config = SimpleNamespace(
            toolbox_vision_base_url="", toolbox_vision_model="", toolbox_vision_api_key=""
        )
        assert describe_screen(b"jpeg", config) == ""


class TestWatcher:
    def _watcher(self, tmp_path: Path) -> ScreenWatcher:
        watcher = ScreenWatcher(
            _config(tmp_path),
            store=ScreenWatchStore(tmp_path / "summaries.db"),
            data_dir=tmp_path,
        )
        return watcher

    def test_tick_paused(self, tmp_path: Path):
        watcher = self._watcher(tmp_path)
        watcher.pause_flag.set()
        assert watcher.tick()["event"] == "paused"

    def test_tick_idle_on_static_screen(self, tmp_path: Path, monkeypatch):
        from atri_qq_bot.screen_watch import watcher as watcher_module
        from PIL import Image

        monkeypatch.setattr(
            watcher_module, "capture_screen", lambda monitor=1: Image.new("RGB", (1920, 1080), (30, 30, 30))
        )
        watcher = self._watcher(tmp_path)
        assert watcher.tick(now=1000.0)["event"] == "idle"
        assert watcher.tick(now=1001.0)["event"] == "idle"

    def test_tick_sensitive_window_skips_vision(self, tmp_path: Path, monkeypatch):
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        frames = iter(
            [
                Image.new("RGB", (1920, 1080), (10, 10, 10)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (240, 240, 240)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (235, 235, 235)),
            ]
        )
        monkeypatch.setattr(watcher_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "招商银行 - 转账")
        monkeypatch.setattr(watcher_module, "describe_screen", lambda jpeg, config: "绝不能被调用")
        watcher = self._watcher(tmp_path)
        watcher.tick(now=1000.0)
        watcher.tick(now=1004.0)
        result = watcher.tick(now=1006.0)
        assert result["event"] == "sensitive"
        assert watcher.store.recent(limit=5) == []

    def test_tick_summarizes_on_scene_change(self, tmp_path: Path, monkeypatch):
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        frames = iter(
            [
                Image.new("RGB", (1920, 1080), (10, 10, 10)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (240, 240, 240)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (235, 235, 235)),
            ]
        )
        monkeypatch.setattr(watcher_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "VS Code")
        monkeypatch.setattr(
            watcher_module, "describe_screen", lambda jpeg, config: "主人正在 VS Code 里写代码"
        )
        watcher = self._watcher(tmp_path)
        watcher.tick(now=1000.0)
        watcher.tick(now=1004.0)
        result = watcher.tick(now=1006.0)
        assert result["event"] == "summarized"
        assert result["summary"] == "主人正在 VS Code 里写代码"
        assert watcher.latest_summary == "主人正在 VS Code 里写代码"
        rows = watcher.store.recent(limit=5)
        assert len(rows) == 1 and rows[0]["kind"] == "auto"

    def test_look_now_manual_summary(self, tmp_path: Path, monkeypatch):
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        monkeypatch.setattr(
            watcher_module, "capture_screen", lambda monitor=1: Image.new("RGB", (800, 600), (0, 0, 0))
        )
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "浏览器")
        monkeypatch.setattr(
            watcher_module, "describe_screen", lambda jpeg, config: "主人在看网页"
        )
        watcher = self._watcher(tmp_path)
        result = watcher.look_now()
        assert result["event"] == "summarized"
        rows = watcher.store.recent(limit=5)
        assert rows[0]["kind"] == "manual"

    def test_vision_failure_does_not_fabricate(self, tmp_path: Path, monkeypatch):
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        frames = iter(
            [
                Image.new("RGB", (1920, 1080), (10, 10, 10)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (240, 240, 240)),
                Image.new("RGB", (1920, 1080), (250, 250, 250)),
                Image.new("RGB", (1920, 1080), (235, 235, 235)),
            ]
        )
        monkeypatch.setattr(watcher_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "游戏")
        monkeypatch.setattr(watcher_module, "describe_screen", lambda jpeg, config: "")
        watcher = self._watcher(tmp_path)
        watcher.tick(now=1000.0)
        watcher.tick(now=1004.0)
        assert watcher.tick(now=1006.0)["event"] == "vision_failed"
        assert watcher.store.recent(limit=5) == []


class TestCapture:
    def test_encode_jpeg_shrinks_wide_image(self):
        from PIL import Image

        image = Image.new("RGB", (3840, 2160), (50, 50, 50))
        data = encode_jpeg(image)
        assert data[:2] == b"\xff\xd8"
        assert len(data) < 800_000

    def test_gray_small_shape(self):
        from PIL import Image

        small = gray_small(Image.new("RGB", (1920, 1080), (128, 128, 128)))
        assert small.shape == (90, 160)


class TestServerCommandAuth:
    def _server(self, tmp_path: Path) -> OneBotServer:
        config = _config(tmp_path)
        engine = SimpleNamespace(
            memory=SimpleNamespace(trust_tier=lambda _p: "normal"),
            record_bot_reply=lambda *a, **k: None,
        )
        server = OneBotServer(config, engine, None)
        # 预置隔离 watcher，避免测试触碰生产 data/screen_watch/
        server._screen_watcher = ScreenWatcher(
            config,
            store=ScreenWatchStore(tmp_path / "summaries.db"),
            data_dir=tmp_path,
        )
        return server

    def test_non_owner_cannot_trigger(self, tmp_path: Path):
        server = self._server(tmp_path)
        event = {
            "message_type": "private",
            "user_id": 999999,
            "message_id": 1,
        }
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看看我屏幕", "private:999999"
            )
        )
        assert result is False

    def test_owner_command_dispatches(self, tmp_path: Path, monkeypatch):
        server = self._server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        monkeypatch.setattr(
            watcher_module,
            "capture_screen",
            lambda monitor=1: Image.new("RGB", (800, 600), (0, 0, 0)),
        )
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "桌面")
        monkeypatch.setattr(
            watcher_module, "describe_screen", lambda jpeg, config: "主人的桌面空空的"
        )
        event = {
            "message_type": "private",
            "user_id": 100000001,
            "message_id": 1,
        }
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看看我屏幕", "private:100000001"
            )
        )
        assert result is True
        assert sent and "主人的桌面空空的" in sent[0]

    def test_disabled_config_blocks_commands(self, tmp_path: Path):
        import dataclasses

        config = dataclasses.replace(_config(tmp_path), screen_watch_enabled=False)
        engine = SimpleNamespace(
            memory=SimpleNamespace(trust_tier=lambda _p: "normal"),
            record_bot_reply=lambda *a, **k: None,
        )
        server = OneBotServer(config, engine, None)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 1}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看看我屏幕", "private:100000001"
            )
        )
        assert result is False
