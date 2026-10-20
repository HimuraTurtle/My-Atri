"""screen_watch 层测试：触发状态机、隐私闸、存取、watcher 闭环与指令权限。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from atri_qq_bot.config import BotConfig
from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.screen_watch import (
    FrameTrigger,
    PauseFlag,
    ScreenWatchStore,
    ScreenWatcher,
    WatchParty,
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


class TestCaptureRetry:
    def _module(self):
        from atri_qq_bot.screen_watch import capture as capture_module

        return capture_module

    def test_transient_failure_recovered_by_retry(self, monkeypatch):
        module = self._module()
        calls = {"mss": 0}

        def flaky_grab(monitor):
            calls["mss"] += 1
            if calls["mss"] == 1:
                raise RuntimeError("transient gdi failure")
            return Image.new("RGB", (1920, 1080), (10, 10, 10))

        monkeypatch.setattr(module, "_grab_via_mss", flaky_grab)
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        image = module.capture_screen()
        assert image is not None
        assert image.size == (1920, 1080)
        assert calls["mss"] == 2

    def test_imagegrab_fallback_after_mss_retries_exhausted(self, monkeypatch):
        module = self._module()

        def broken_grab(monitor):
            raise RuntimeError("gdi always fails in exclusive fullscreen")

        monkeypatch.setattr(module, "_grab_via_mss", broken_grab)
        monkeypatch.setattr(module, "_grab_via_imagegrab", lambda monitor: Image.new("RGB", (100, 50), (5, 5, 5)))
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        image = module.capture_screen()
        assert image is not None
        assert image.size == (100, 50)

    def test_all_channels_failed_returns_none(self, monkeypatch, caplog):
        import logging

        module = self._module()

        def broken_grab(monitor):
            raise OSError("screen handle invalid")

        monkeypatch.setattr(module, "_grab_via_mss", broken_grab)
        monkeypatch.setattr(module, "_grab_via_imagegrab", broken_grab)
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        with caplog.at_level(logging.WARNING, logger="atri.screen_watch"):
            assert module.capture_screen() is None
        assert any("屏幕截图失败" in record.message for record in caplog.records)
        assert any("OSError" in record.message for record in caplog.records)

    def test_success_first_try_skips_fallback(self, monkeypatch):
        module = self._module()
        calls = {"fallback": 0}

        def ok_grab(monitor):
            return Image.new("RGB", (640, 480), (1, 1, 1))

        def fallback_should_not_run(monitor):
            calls["fallback"] += 1
            return Image.new("RGB", (640, 480), (2, 2, 2))

        monkeypatch.setattr(module, "_grab_via_mss", ok_grab)
        monkeypatch.setattr(module, "_grab_via_imagegrab", fallback_should_not_run)
        image = module.capture_screen()
        assert image is not None
        assert calls["fallback"] == 0


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


class TestServerCommandNormalization:
    def _server(self, tmp_path: Path) -> OneBotServer:
        config = _config(tmp_path)
        engine = SimpleNamespace(
            memory=SimpleNamespace(
                trust_tier=lambda _p: "normal",
                recall_context=lambda _p, _t, **_k: "",
            ),
            record_bot_reply=lambda *a, **k: None,
        )
        server = OneBotServer(config, engine, None)
        server._screen_watcher = ScreenWatcher(
            config,
            store=ScreenWatchStore(tmp_path / "summaries.db"),
            data_dir=tmp_path,
        )
        return server

    def test_normalize_strips_prefix_spaces_and_particles(self):
        from atri_qq_bot.onebot.server_parts.server_screen_watch import (
            _normalize_command,
        )

        assert _normalize_command("atri看看我的屏幕") == "看看我的屏幕"
        assert _normalize_command("亚托莉 看看我的屏幕 呀") == "看看我的屏幕"
        assert _normalize_command("看看我屏幕？") == "看看我屏幕"
        assert _normalize_command("@ATRI 我在干嘛") == "我在干嘛"
        assert _normalize_command("") == ""

    def test_match_covers_owner_real_phrases(self):
        from atri_qq_bot.onebot.server_parts.server_screen_watch import (
            _match_watch_command,
        )

        assert _match_watch_command("看看我的屏幕") == "look"
        assert _match_watch_command("帮我看看我的屏幕") == "look"
        assert _match_watch_command("我现在在干嘛") == "recent"
        assert _match_watch_command("屏幕监控状态") == "status"
        assert _match_watch_command("屏幕监控停止") == "pause"
        # 开关类指令不允许 endswith：否定句不得误触发放行/暂停。
        assert _match_watch_command("不要停止屏幕监控") is None
        # 问 ATRI 本体在干嘛不属于屏幕指令。
        assert _match_watch_command("你在干嘛") is None

    def test_owner_prefixed_look_command_hits(self, tmp_path: Path, monkeypatch):
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
            watcher_module, "describe_screen", lambda jpeg, config: "用户在写代码"
        )
        event = {"message_type": "group", "user_id": 100000001, "message_id": 2}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "atri看看我的屏幕", "group:110866825"
            )
        )
        assert result is True
        assert sent and "用户在写代码" in sent[0]

    def test_recent_command_with_particle(self, tmp_path: Path, monkeypatch):
        server = self._server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 3}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "atri 我现在在干嘛呀", "private:100000001"
            )
        )
        assert result is True
        assert sent and "屏幕动态" in sent[0]

    def test_watch_failure_falls_through_to_chat(self, tmp_path: Path, monkeypatch):
        server = self._server(tmp_path)

        def boom():
            raise RuntimeError("screen capture exploded")

        monkeypatch.setattr(server._screen_watcher, "look_now", boom)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 4}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看看我的屏幕", "private:100000001"
            )
        )
        assert result is False

    def test_daddy_variants_route_to_look_and_recent(self):
        from atri_qq_bot.onebot.server_parts.server_screen_watch import (
            _match_watch_command,
        )

        # 「你爸爸」自称变体：endswith 同时覆盖带/不带「你」的形式。
        assert _match_watch_command("你爸爸现在在干嘛") == "look"
        assert _match_watch_command("你爸爸在干嘛") == "look"
        assert _match_watch_command("爸爸在干嘛") == "look"
        assert _match_watch_command("你爸爸在做什么") == "look"
        assert _match_watch_command("爸爸现在在做什么") == "look"
        # 时间词分流：刚才/最近 → recent（查历史），不得误路由成即时截图。
        assert _match_watch_command("你爸爸刚才在干嘛") == "recent"
        assert _match_watch_command("爸爸最近在做什么") == "recent"
        assert _match_watch_command("你爸爸最近在干嘛") == "recent"
        # 普通指令不受影响。
        assert _match_watch_command("看看我的屏幕") == "look"
        assert _match_watch_command("我在干嘛") == "recent"

    def _daddy_server(self, tmp_path: Path) -> OneBotServer:
        import dataclasses

        server = self._server(tmp_path)
        server.config = dataclasses.replace(
            server.config, screen_watch_party_enabled=True
        )
        return server

    def _mock_vision(self, monkeypatch, summary: str) -> None:
        from PIL import Image

        from atri_qq_bot.screen_watch import watcher as watcher_module

        monkeypatch.setattr(
            watcher_module,
            "capture_screen",
            lambda monitor=1: Image.new("RGB", (800, 600), (0, 0, 0)),
        )
        monkeypatch.setattr(watcher_module, "active_window_title", lambda: "桌面")
        monkeypatch.setattr(watcher_module, "describe_screen", lambda jpeg, config: summary)

    def test_daddy_look_uses_generated_reply(self, tmp_path: Path, monkeypatch):
        server = self._daddy_server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        self._mock_vision(monkeypatch, "用户在写代码")

        import atri_qq_bot.onebot.server_parts.server_screen_watch as screen_watch_module

        calls = {"n": 0}

        def fake_generate(config, summary, persona_system):
            calls["n"] += 1
            assert "写代码" in summary
            return "爸爸，你的电脑正在跑代码呢~"

        monkeypatch.setattr(screen_watch_module, "generate_look_reply", fake_generate)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 5}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "ATRI，你爸爸现在在干嘛", "private:100000001"
            )
        )
        assert result is True
        assert calls["n"] == 1
        assert sent and sent[0] == "爸爸，你的电脑正在跑代码呢~"

    def test_daddy_look_generation_failure_falls_back(self, tmp_path: Path, monkeypatch):
        server = self._daddy_server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        self._mock_vision(monkeypatch, "用户在看论文")

        import atri_qq_bot.onebot.server_parts.server_screen_watch as screen_watch_module

        monkeypatch.setattr(
            screen_watch_module, "generate_look_reply", lambda *args: ""
        )
        event = {"message_type": "private", "user_id": 100000001, "message_id": 6}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "你爸爸在干嘛", "private:100000001"
            )
        )
        assert result is True
        # 生成失败退回固定文案，绝不静默也不编造。
        assert sent and "我看到的画面是" in sent[0] and "用户在看论文" in sent[0]

    def test_plain_look_does_not_call_generation(self, tmp_path: Path, monkeypatch):
        server = self._daddy_server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        self._mock_vision(monkeypatch, "用户在看视频")

        import atri_qq_bot.onebot.server_parts.server_screen_watch as screen_watch_module

        calls = {"n": 0}

        def fake_generate(config, summary, persona_system):
            calls["n"] += 1
            return "不该被调用"

        monkeypatch.setattr(screen_watch_module, "generate_look_reply", fake_generate)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 7}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看看我的屏幕", "private:100000001"
            )
        )
        assert result is True
        assert calls["n"] == 0
        assert sent and "我看到的画面是" in sent[0]

    def test_daddy_recent_reads_history_not_screenshot(self, tmp_path: Path, monkeypatch):
        server = self._daddy_server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 8}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "你爸爸刚才在干嘛", "private:100000001"
            )
        )
        assert result is True
        # recent 走历史摘要：无记录时的引导文案与普通 recent 完全一致。
        assert sent and "屏幕动态" in sent[0]


class TestWatchParty:
    """围观模式：结束话语规则、宣告预筛、tick 状态机与指令集成。"""

    def test_should_end_party_phrases(self):
        from atri_qq_bot.screen_watch.party import should_end_party

        assert should_end_party("论文写完了")
        assert should_end_party("我不做毕业设计了")
        assert should_end_party("先不弄了")
        assert should_end_party("不写论文了")
        assert should_end_party("收工啦")
        assert not should_end_party("这论文写得真好")
        assert not should_end_party("你在干嘛")
        assert not should_end_party("看我做数学题")
        assert not should_end_party("")

    def test_activity_announce_precheck(self):
        from atri_qq_bot.screen_watch.party import looks_like_activity_announce

        assert looks_like_activity_announce("我要去写毕业论文了")
        assert looks_like_activity_announce("该打游戏了")
        assert looks_like_activity_announce("我打算开始学Python")
        assert not looks_like_activity_announce("我要睡了")
        assert not looks_like_activity_announce("今天天气真好")
        assert not looks_like_activity_announce("")

    def test_parse_party_activity(self):
        from atri_qq_bot.onebot.server_parts.server_screen_watch import (
            _normalize_command,
            _parse_party_activity,
        )

        assert _parse_party_activity(_normalize_command("atri看着我写论文")) == "写论文"
        assert _parse_party_activity("围观我打gal") == "打gal"
        assert _parse_party_activity("看着我") == ""
        assert _parse_party_activity("看看我的屏幕") == ""
        assert _parse_party_activity("看着我写") == "写"

    def _party(self, tmp_path: Path, **overrides) -> WatchParty:
        import dataclasses

        config = dataclasses.replace(_config(tmp_path), **overrides)
        return WatchParty(
            config, "写论文", {"user_id": 100000001}, "private:100000001"
        )

    def test_party_tick_comment_and_cooldown(self, tmp_path: Path, monkeypatch):
        from itertools import cycle

        from PIL import Image

        from atri_qq_bot.screen_watch import party as party_module

        party = self._party(tmp_path)
        frames = iter(
            cycle(
                [
                    Image.new("RGB", (160, 90), (0, 0, 0)),
                    Image.new("RGB", (160, 90), (255, 255, 255)),
                ]
            )
        )
        monkeypatch.setattr(party_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(
            party_module, "active_window_title", lambda: "记事本"
        )
        monkeypatch.setattr(
            party_module, "describe_screen", lambda jpeg, config: "主人在文档里打字"
        )
        monkeypatch.setattr(
            party_module,
            "generate_commentary",
            lambda config, activity, description, persona_system, last="": f"（盯着）{activity}好认真",
        )
        assert party.tick(now=1000.0)["event"] == "idle"
        assert party.tick(now=1001.0)["event"] == "idle"
        result = party.tick(now=1002.0)
        assert result["event"] == "comment"
        assert party.comment_count == 1
        assert party.last_comment_text
        # 冷却内画面再变化也不再评论（FrameTrigger 的 cooldown 承担最小间隔）。
        assert party.tick(now=1003.0)["event"] == "idle"

    def test_party_tick_sensitive_window(self, tmp_path: Path, monkeypatch):
        from itertools import cycle

        from PIL import Image

        from atri_qq_bot.screen_watch import party as party_module

        party = self._party(tmp_path)
        frames = iter(
            cycle(
                [
                    Image.new("RGB", (160, 90), (0, 0, 0)),
                    Image.new("RGB", (160, 90), (255, 255, 255)),
                ]
            )
        )
        monkeypatch.setattr(party_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(
            party_module, "active_window_title", lambda: "Chrome - Sign in"
        )
        assert party.tick(now=2000.0)["event"] == "idle"
        assert party.tick(now=2001.0)["event"] == "idle"
        assert party.tick(now=2002.0)["event"] == "sensitive"
        assert party.comment_count == 0

    def test_party_tick_failure_autostop(self, tmp_path: Path, monkeypatch):
        from atri_qq_bot.screen_watch import party as party_module

        party = self._party(tmp_path, screen_watch_party_max_failures=2)
        monkeypatch.setattr(party_module, "capture_screen", lambda monitor=1: None)
        assert party.tick(now=3000.0)["event"] == "skipped"
        assert party.tick(now=3001.0)["event"] == "failed_stop"

    def test_party_tick_comment_failure_does_not_stop(self, tmp_path: Path, monkeypatch):
        from itertools import cycle

        from PIL import Image

        from atri_qq_bot.screen_watch import party as party_module

        party = self._party(tmp_path)
        frames = iter(
            cycle(
                [
                    Image.new("RGB", (160, 90), (0, 0, 0)),
                    Image.new("RGB", (160, 90), (255, 255, 255)),
                ]
            )
        )
        monkeypatch.setattr(party_module, "capture_screen", lambda monitor=1: next(frames))
        monkeypatch.setattr(party_module, "active_window_title", lambda: "记事本")
        monkeypatch.setattr(
            party_module, "describe_screen", lambda jpeg, config: "有人在写东西"
        )
        monkeypatch.setattr(
            party_module,
            "generate_commentary",
            lambda config, activity, description, persona_system, last="": "",
        )
        party.tick(now=4000.0)
        party.tick(now=4001.0)
        assert party.tick(now=4002.0)["event"] == "comment_failed"
        assert party.consecutive_failures == 0
        assert party.comment_count == 0

    def _server(self, tmp_path: Path) -> OneBotServer:
        config = _config(tmp_path)
        engine = SimpleNamespace(
            memory=SimpleNamespace(
                trust_tier=lambda _p: "normal",
                recall_context=lambda _p, _t, **_k: "",
            ),
            record_bot_reply=lambda *a, **k: None,
        )
        server = OneBotServer(config, engine, None)
        server._screen_watcher = ScreenWatcher(
            config,
            store=ScreenWatchStore(tmp_path / "summaries.db"),
            data_dir=tmp_path,
        )
        return server

    def test_party_start_and_stop_via_command(self, tmp_path: Path, monkeypatch):
        server = self._server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 5}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "atri看着我写论文", "private:100000001"
            )
        )
        assert result is True
        party = server._watch_party
        assert party is not None
        assert party.activity == "写论文"
        assert party.conversation_id == "private:100000001"
        assert "写论文" in sent[-1]
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "我不写论文了", "private:100000001"
            )
        )
        assert result is True
        assert server._watch_party is None
        assert "辛苦" in sent[-1]

    def test_activity_announce_auto_opens_party(self, tmp_path: Path, monkeypatch):
        server = self._server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        from atri_qq_bot.onebot.server_parts import server_screen_watch as sw_module

        calls: list[str] = []

        def fake_classify(text, config):
            calls.append(text)
            return {"announce": True, "watchable": True, "activity": "写毕业论文"}

        monkeypatch.setattr(sw_module, "classify_activity", fake_classify)
        event = {"message_type": "group", "user_id": 100000001, "message_id": 6}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "我要去写毕业论文啦", "group:110866825"
            )
        )
        assert result is True
        assert server._watch_party is not None
        assert server._watch_party.activity == "写毕业论文"
        assert server._watch_party.conversation_id == "group:110866825"
        assert len(calls) == 1
        # 围观中再来一条结束话语：直接收工。
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "写完了", "group:110866825"
            )
        )
        assert result is True
        assert server._watch_party is None
        # 冷却内的下一条活动宣告不再调分类，放行到聊天流程。
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "我去打两把游戏", "group:110866825"
            )
        )
        assert result is False
        assert len(calls) == 1

    def test_party_disabled_blocks_commands(self, tmp_path: Path):
        import dataclasses

        config = dataclasses.replace(_config(tmp_path), screen_watch_party_enabled=False)
        engine = SimpleNamespace(
            memory=SimpleNamespace(trust_tier=lambda _p: "normal"),
            record_bot_reply=lambda *a, **k: None,
        )
        server = OneBotServer(config, engine, None)
        server._screen_watcher = ScreenWatcher(
            config,
            store=ScreenWatchStore(tmp_path / "summaries.db"),
            data_dir=tmp_path,
        )
        event = {"message_type": "private", "user_id": 100000001, "message_id": 7}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "看着我写论文", "private:100000001"
            )
        )
        assert result is False
        assert getattr(server, "_watch_party", None) is None

    def test_generated_replies_call_model_with_persona_and_address_rule(
        self, tmp_path: Path, monkeypatch
    ):
        from atri_qq_bot.screen_watch import party as party_module

        calls: list[dict] = []

        def fake_completion(config, system_texts, user_prompt, *, max_tokens):
            calls.append(
                {
                    "systems": list(system_texts),
                    "prompt": user_prompt,
                    "max_tokens": max_tokens,
                }
            )
            return "（搬来小椅子）父亲写论文，我看着呢！"

        monkeypatch.setattr(party_module, "_chat_completion", fake_completion)
        persona = ["人格提示词", "关于对方的记忆背景：用户希望被叫作父亲"]
        config = _config(tmp_path)

        opening = party_module.generate_opening_reply(config, "写论文", persona)
        assert opening == "（搬来小椅子）父亲写论文，我看着呢！"
        assert calls[-1]["systems"] == persona
        assert "写论文" in calls[-1]["prompt"]
        assert "禁止" in calls[-1]["prompt"] and "主人" in calls[-1]["prompt"]
        assert calls[-1]["max_tokens"] == party_module._SHORT_REPLY_MAX_TOKENS

        closing = party_module.generate_closing_reply(config, "写论文", 3, persona)
        assert closing == "（搬来小椅子）父亲写论文，我看着呢！"
        assert "3 句嘴" in calls[-1]["prompt"]

        farewell = party_module.generate_failure_farewell(config, 5, persona)
        assert farewell == "（搬来小椅子）父亲写论文，我看着呢！"
        assert "5 次" in calls[-1]["prompt"]

        comment = party_module.generate_commentary(
            config, "写论文", "文档里有公式推导", persona, "上一条评论"
        )
        assert comment == "（搬来小椅子）父亲写论文，我看着呢！"
        assert "公式推导" in calls[-1]["prompt"]
        assert "换个角度" in calls[-1]["prompt"]
        assert calls[-1]["max_tokens"] == party_module._COMMENT_MAX_TOKENS

    def test_generated_replies_fallback_without_owner_title(
        self, tmp_path: Path, monkeypatch
    ):
        from atri_qq_bot.screen_watch import party as party_module

        monkeypatch.setattr(
            party_module, "_chat_completion", lambda *a, **k: ""
        )
        config = _config(tmp_path)
        persona = ["人格提示词"]

        assert "主人" not in party_module.generate_opening_reply(config, "写论文", persona)
        assert "写论文" in party_module.generate_opening_reply(config, "写论文", persona)
        replaced = party_module.generate_opening_reply(
            config, "画画", persona, replaced=True
        )
        assert "收起刚才的围观" in replaced and "主人" not in replaced

        closing = party_module.generate_closing_reply(config, "写论文", 0, persona)
        assert "辛苦" in closing and "主人" not in closing

        farewell = party_module.generate_failure_farewell(config, 4, persona)
        assert "看不清屏幕" in farewell and "主人" not in farewell

        # 模型输出了「主人」时视为不合格：评论静默跳过，宣告类退回兜底。
        monkeypatch.setattr(
            party_module, "_chat_completion", lambda *a, **k: "主人好呀！"
        )
        assert party_module.generate_commentary(config, "写论文", "画面", persona) == ""

    def test_party_replies_never_use_owner_title_in_fallback_flows(
        self, tmp_path: Path, monkeypatch
    ):
        server = self._server(tmp_path)
        sent: list[str] = []

        async def fake_send(ws, event, outgoing, **kwargs):
            sent.append(outgoing[0].content)

        monkeypatch.setattr(server, "send_reply", fake_send)
        event = {"message_type": "private", "user_id": 100000001, "message_id": 8}
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "atri看着我写论文", "private:100000001"
            )
        )
        assert result is True
        assert "写论文" in sent[-1]
        assert "主人" not in sent[-1]
        result = asyncio.run(
            server._maybe_handle_screen_watch_command(
                None, event, "我不写论文了", "private:100000001"
            )
        )
        assert result is True
        assert "辛苦" in sent[-1]
        assert "主人" not in sent[-1]

    def test_party_persona_system_layers(self, tmp_path: Path):
        from atri_qq_bot.group_chat import GROUP_PROMPT
        from atri_qq_bot.persona.core import PERSONA_PROMPT

        server = self._server(tmp_path)
        server.reply_engine = SimpleNamespace(
            memory=SimpleNamespace(
                recall_context=lambda profile_id, text, **k: (
                    "用户希望被叫作父亲"
                    if str(profile_id) == "private:2578607322"
                    else "另一个账号的背景"
                ),
                trust_tier=lambda _p: "normal",
            )
        )

        # 私聊围观：称呼是账号级的，只注入该账号自己的档案。
        systems = server._party_persona_system("private:2578607322")
        assert systems[0] == PERSONA_PROMPT
        assert "用户希望被叫作父亲" in systems[1]
        assert any("禁止" in text and "「主人」" in text for text in systems)

        # 换一个账号：称呼随档案变，不继承其他账号的称呼。
        systems_other = server._party_persona_system("private:100000001")
        assert all("用户希望被叫作父亲" not in text for text in systems_other)
        assert any("另一个账号的背景" in text for text in systems_other)

        # 群聊围观：不注入任何人的私聊档案，私聊专属称呼不得带进群聊。
        systems_group = server._party_persona_system("group:110866825")
        assert systems_group[0] == PERSONA_PROMPT
        assert systems_group[1] == GROUP_PROMPT
        assert all(
            "用户希望被叫作父亲" not in text and "另一个账号的背景" not in text
            for text in systems_group
        )
        # 群聊版规则：不用私聊专属称呼 + 尾条禁「主人」。
        assert any("私聊" in text and "专属称呼" in text for text in systems_group)
        assert "禁止" in systems_group[-1] and "「主人」" in systems_group[-1]
