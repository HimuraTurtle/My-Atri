from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from atri_qq_bot.bilibili.parsing import (
    extract_bvid,
    extract_page_param,
    extract_short_url,
    is_bilibili_domain,
    is_short_bili_url,
    message_is_pure_link,
    url_from_event,
    url_from_json_card_text,
)
from atri_qq_bot.bilibili.service import BilibiliService, _fmt_count
from atri_qq_bot.bilibili.summarize import (
    build_prompt,
    format_time,
    smart_truncate,
    strip_markdown,
)


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "bilibili_enabled": True,
        "bilibili_auto_summary": False,
        "bilibili_note_style": "professional",
        "bilibili_cookie": "",
        "bilibili_asr_enabled": True,
        "bilibili_cooldown_seconds": 20,
        "bilibili_max_note_length": 3500,
        "bilibili_timeout_seconds": 300.0,
        "bilibili_audio_dir": "",
        "openai_base_url": "https://api.deepseek.com/v1",
        "openai_api_key": "sk-test",
        "openai_model": "deepseek-flash",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _video_info(**overrides) -> SimpleNamespace:
    defaults = dict(
        bvid="BV1GJ411x7h7", cid=1, pages=1, title="测试视频", owner_name="UP",
        owner_mid=1, duration=125, view=23456, danmaku=100, like=9,
        url="https://www.bilibili.com/video/BV1GJ411x7h7", raw={"tname": "科技"},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_TRANSCRIPT = SimpleNamespace(
    segments=[SimpleNamespace(start=0.0, end=1.0, text="开场白")],
    source="subtitle",
)


# ── parsing ─────────────────────────────────────────────────────────


def test_extract_bvid_variants() -> None:
    assert extract_bvid("BV1GJ411x7h7") == "BV1GJ411x7h7"
    assert extract_bvid("看看这个 https://www.bilibili.com/video/BV1xx411c7mD?spm=x") == "BV1xx411c7mD"
    assert extract_bvid("没有链接") is None


def test_domain_checks() -> None:
    assert is_bilibili_domain("https://www.bilibili.com/video/BV1xx411c7mD")
    assert is_bilibili_domain("https://b23.tv/abc123")
    assert not is_bilibili_domain("https://www.youtube.com/watch?v=1")
    assert is_short_bili_url("https://b23.tv/abc123")
    assert not is_short_bili_url("https://www.bilibili.com/video/BV1xx411c7mD")
    assert extract_short_url("看这个 b23.tv/abc123 谢谢") is None
    assert extract_short_url("https://b23.tv/abc123") == "https://b23.tv/abc123"


def test_extract_page_param() -> None:
    assert extract_page_param("https://www.bilibili.com/video/BV1xx411c7mD?p=3") == 3
    assert extract_page_param("https://www.bilibili.com/video/BV1xx411c7mD") == 1


def test_url_from_json_card() -> None:
    card = '{"meta":{"detail_1":{"qqdocurl":"https://b23.tv/abc123"}}}'
    assert url_from_json_card_text(card) == "https://b23.tv/abc123"


def test_url_from_event_prefers_json_card() -> None:
    event = {
        "message": [
            {"type": "json", "data": {"data": '{"meta":{"detail_1":{"qqdocurl":"https://www.bilibili.com/video/BV1GJ411x7h7"}}}'}},
        ]
    }
    assert url_from_event(event) == "https://www.bilibili.com/video/BV1GJ411x7h7"


def test_url_from_event_falls_back_to_text() -> None:
    event = {
        "message": [
            {"type": "text", "data": {"text": "https://www.bilibili.com/video/BV1GJ411x7h7"}},
        ]
    }
    assert url_from_event(event) == "https://www.bilibili.com/video/BV1GJ411x7h7"


def test_message_is_pure_link() -> None:
    assert message_is_pure_link("https://www.bilibili.com/video/BV1GJ411x7h7", {})
    assert message_is_pure_link("https://b23.tv/abc123", {})
    assert message_is_pure_link("", {"message": [{"type": "json", "data": {}}]})
    assert not message_is_pure_link("我觉得这个视频 https://www.bilibili.com/video/BV1GJ411x7h7 讲得还行", {})
    assert not message_is_pure_link("", {"message": [{"type": "text", "data": {"text": "早"}}]})


# ── 命令识别 ───────────────────────────────────────────────────────


def test_is_command() -> None:
    assert BilibiliService.is_command("B站总结 BV1GJ411x7h7")
    assert BilibiliService.is_command("B站总结 https://b23.tv/abc123")
    assert BilibiliService.is_command("B站解析 https://www.bilibili.com/video/BV1xx411c7mD")
    assert BilibiliService.is_command("B站帮助")
    assert BilibiliService.is_command("bv总结 BV1GJ411x7h7")
    # 无链接的闲聊不应被拦截
    assert not BilibiliService.is_command("B站总结一下这个视频呗")
    assert not BilibiliService.is_command("怎么视频总结啊")
    assert not BilibiliService.is_command("https://www.bilibili.com/video/BV1GJ411x7h7")


def test_fmt_count() -> None:
    assert _fmt_count(9_999) == "9999"
    assert _fmt_count(23_456) == "2.3万"
    assert _fmt_count(123_456_789) == "1.2亿"


# ── prompt / 后处理 ────────────────────────────────────────────────


def test_format_time() -> None:
    assert format_time(0) == "00:00"
    assert format_time(65) == "01:05"
    assert format_time(3671) == "1:01:11"


def test_build_prompt_contains_segments() -> None:
    segments = [SimpleNamespace(start=1.0, text="你好"), SimpleNamespace(start=62.0, text="世界")]
    prompt = build_prompt(title="T", uploader="U", segments=segments, style="concise")
    assert "01:00 - 世界" in prompt
    assert "00:01 - 你好" in prompt
    assert "【简洁模式】" in prompt
    assert "禁止使用任何 Markdown 语法" in prompt


def test_strip_markdown() -> None:
    assert strip_markdown("# 标题\n**加粗** *斜体*\n> 引用\n- 列表") == "标题\n加粗 斜体\n引用\n· 列表"


def test_smart_truncate_keeps_paragraph_boundary() -> None:
    body = "A" * 100 + "\n\n" + "B" * 200
    result = smart_truncate(body, 150)
    assert result.startswith("A" * 100)
    assert "内容过长" in result


# ── service 行为（mock 网络） ──────────────────────────────────────


def test_handle_command_help() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config())
        result = await service.handle_command("B站帮助", "group:1")
        assert result.messages[0].kind == "text"
        assert "B站总结" in result.messages[0].content

    asyncio.run(scenario())


def test_handle_command_missing_arg() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config())
        result = await service.handle_command("B站总结", "group:1")
        assert "缺少视频链接" in result.messages[0].content

    asyncio.run(scenario())


def test_handle_command_unsupported_platform() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config())
        result = await service.handle_command(
            "B站总结 https://www.youtube.com/watch?v=x", "group:1"
        )
        assert "仅支持" in result.messages[0].content

    asyncio.run(scenario())


def test_handle_command_summary_flow() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config())
        with patch.object(
            service.client, "get_video_info", AsyncMock(return_value=_video_info())
        ), patch(
            "atri_qq_bot.bilibili.service.acquire_transcript",
            AsyncMock(return_value=(_TRANSCRIPT, None)),
        ), patch.object(
            service.summarizer, "chat", AsyncMock(return_value="## 总结\n这是要点")
        ):
            result = await service.handle_command("B站总结 BV1GJ411x7h7", "group:1")
        content = result.messages[0].content
        assert "《测试视频》" in content
        assert "UP：UP" in content
        # Markdown 兜底剥离
        assert "总结\n这是要点" in content
        assert "##" not in content

        # 缓存生效：第二次调用不再请求网络
        with patch.object(
            service.client,
            "get_video_info",
            AsyncMock(side_effect=AssertionError("should hit cache")),
        ):
            result2 = await service.handle_command("B站总结 BV1GJ411x7h7", "group:1")
        assert result2.messages[0].content == content

    asyncio.run(scenario())


def test_handle_command_cooldown() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config(bilibili_cooldown_seconds=60))
        service._punch_cooldown("cmd:10001")
        result = await service.handle_command("B站解析 BV1GJ411x7h7", "group:1", user_id=10001)
        assert "频繁" in result.messages[0].content

    asyncio.run(scenario())


def test_auto_summary_disabled_by_default() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config())
        event = {"message": [{"type": "text", "data": {"text": "https://b23.tv/abc123"}}]}
        assert await service.auto_summarize(event, "https://b23.tv/abc123", "group:1") is None

    asyncio.run(scenario())


def test_auto_summary_rejects_conversational_text() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config(bilibili_auto_summary=True))
        text = "这个视频真不错 https://www.bilibili.com/video/BV1GJ411x7h7 大家看看"
        event = {"message": [{"type": "text", "data": {"text": text}}]}
        with patch.object(service.client, "get_video_info", AsyncMock()) as fetch:
            assert await service.auto_summarize(event, text, "group:1") is None
            fetch.assert_not_called()

    asyncio.run(scenario())


def test_auto_summary_pure_link_triggers() -> None:
    async def scenario() -> None:
        service = BilibiliService(_config(bilibili_auto_summary=True))
        text = "https://www.bilibili.com/video/BV1GJ411x7h7"
        event = {"message": [{"type": "text", "data": {"text": text}}]}
        with patch.object(
            service.client, "get_video_info", AsyncMock(return_value=_video_info())
        ), patch(
            "atri_qq_bot.bilibili.service.acquire_transcript",
            AsyncMock(return_value=(_TRANSCRIPT, None)),
        ), patch.object(
            service.summarizer, "chat", AsyncMock(return_value="总结内容")
        ):
            result = await service.auto_summarize(event, text, "group:1")
        assert result is not None
        assert "自动总结" in result.messages[0].content

    asyncio.run(scenario())


def test_auto_summary_per_conversation_cooldown() -> None:
    async def scenario() -> None:
        service = BilibiliService(
            _config(bilibili_auto_summary=True, bilibili_cooldown_seconds=300)
        )
        text = "https://www.bilibili.com/video/BV1GJ411x7h7"
        event = {"message": [{"type": "text", "data": {"text": text}}]}
        service._punch_cooldown("auto:group:1")
        with patch.object(service.client, "get_video_info", AsyncMock()) as fetch:
            assert await service.auto_summarize(event, text, "group:1") is None
            fetch.assert_not_called()

    asyncio.run(scenario())
