"""「是啊，吃什么」接梗测试（移植自 astrbot_plugin_what_to_eat）。

覆盖：关键词匹配、推荐/复读/兜底三路输出、复读冷却与窗口超限强制
推荐（多 Bot 防循环）、Mixin 拦截（群聊触发、私聊不触发、确定性命令
让位、开关与信任黑名单）。全部离线。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.what_to_eat import (
    ECHO_TEMPLATES,
    RECOMMEND_TEMPLATES,
    WhatToEatService,
)


def _service(**overrides: Any) -> WhatToEatService:
    config = dict(
        what_to_eat_trigger_keywords=("吃什么", "吃啥"),
        what_to_eat_recommend_probability=0.3,
        what_to_eat_builtin_foods=("黄焖鸡米饭", "螺蛳粉"),
        what_to_eat_custom_foods=(),
        what_to_eat_rate_limit_max=3,
        what_to_eat_rate_limit_window_seconds=60,
        what_to_eat_echo_cooldown_seconds=15,
    )
    config.update(overrides)
    return WhatToEatService(SimpleNamespace(**config))


def test_matches_keywords() -> None:
    service = _service()
    assert service.matches("中午吃什么啊")
    assert service.matches("吃啥吃啥吃啥")
    assert service.matches("  吃什么")
    assert not service.matches("早上吃包子")
    assert not service.matches("")
    assert not service.matches("吃饭")


def test_empty_keywords_never_match() -> None:
    service = _service(what_to_eat_trigger_keywords=())
    assert not service.matches("中午吃什么")


def test_decide_recommends_with_template() -> None:
    service = _service(what_to_eat_recommend_probability=1.0)
    formatted = [t.format(food=f) for f in service.foods for t in RECOMMEND_TEMPLATES]
    texts = []
    for _ in range(16):
        decision = service.decide("group:20001")
        assert decision.kind == "recommend"
        assert decision.text in formatted
        texts.append(decision.text)
    # 16 轮内至少出现一次带具体食物的推荐（纯螃蟹执念模板无 {food}）。
    assert any(("黄焖鸡米饭" in t) or ("螺蛳粉" in t) for t in texts)


def test_recommend_templates_pitch_crab_first() -> None:
    # 人设规定：螃蟹兴奋可短暂压过话题，但要很快回来——所有推荐模板
    # 都带螃蟹戏，且文案不含触发关键词防止接龙自己；恰好保留一条无
    # {food} 的纯螃蟹执念模板，其余都落到实际推荐。
    assert all("螃蟹" in t for t in RECOMMEND_TEMPLATES)
    assert all("吃什么" not in t and "吃啥" not in t for t in RECOMMEND_TEMPLATES)
    assert sum("{food}" in t for t in RECOMMEND_TEMPLATES) == len(RECOMMEND_TEMPLATES) - 1


def test_decide_recommend_reply_carries_crab_pitch() -> None:
    service = _service(what_to_eat_recommend_probability=1.0)
    texts = [service.decide("group:20001").text for _ in range(16)]
    assert all("螃蟹" in t for t in texts)
    assert any(any(food in t for food in service.foods) for t in texts)


def test_no_adjacent_recommend_repeat() -> None:
    service = _service(what_to_eat_recommend_probability=1.0)
    texts = [service.decide("group:20001").text for _ in range(12)]
    for previous, current in zip(texts, texts[1:]):
        assert previous != current


def test_food_avoids_immediate_repeat(monkeypatch) -> None:
    import atri_qq_bot.what_to_eat as what_to_eat_module

    monkeypatch.setattr(
        what_to_eat_module,
        "RECOMMEND_TEMPLATES",
        ("{food}！", "{food}怎么样？"),
    )
    service = _service(
        what_to_eat_recommend_probability=1.0,
        what_to_eat_builtin_foods=("黄焖鸡米饭", "螺蛳粉"),
    )
    foods = [
        "黄焖鸡米饭" if "黄焖鸡米饭" in service.decide("group:20001").text else "螺蛳粉"
        for _ in range(6)
    ]
    assert foods in (
        ["黄焖鸡米饭", "螺蛳粉"] * 3,
        ["螺蛳粉", "黄焖鸡米饭"] * 3,
    )


def test_decide_echoes_and_keeps_meme_core() -> None:
    service = _service(what_to_eat_recommend_probability=0.0)
    decision = service.decide("group:20001")
    assert decision.kind == "echo"
    assert decision.text in ECHO_TEMPLATES
    assert "吃什么" in decision.text


def test_echo_cooldown_forces_recommend() -> None:
    service = _service(what_to_eat_recommend_probability=0.0)
    first = service.decide("group:20001")
    assert first.kind == "echo"
    second = service.decide("group:20001")
    assert second.kind == "recommend"


def test_rate_limit_forces_recommend_after_window_quota() -> None:
    service = _service(
        what_to_eat_recommend_probability=0.0,
        what_to_eat_echo_cooldown_seconds=0,
    )
    assert service.decide("group:20001").kind == "echo"
    assert service.decide("group:20001").kind == "echo"
    assert service.decide("group:20001").kind == "echo"
    # 第 4 次触发已超窗口配额，强制推荐打断接龙。
    assert service.decide("group:20001").kind == "recommend"


def test_rate_limit_is_per_conversation() -> None:
    service = _service(what_to_eat_recommend_probability=0.0)
    service.decide("group:20001")
    service.decide("group:20001")
    service.decide("group:20001")
    assert service.decide("group:20002").kind == "echo"


def test_no_foods_falls_back() -> None:
    service = _service(
        what_to_eat_recommend_probability=1.0,
        what_to_eat_builtin_foods=(),
        what_to_eat_custom_foods=(),
    )
    decision = service.decide("group:20001")
    assert decision.kind == "fallback"
    assert "高性能" in decision.text


def test_custom_foods_merge_and_dedupe() -> None:
    service = _service(
        what_to_eat_builtin_foods=("黄焖鸡米饭",),
        what_to_eat_custom_foods=("螺蛳粉", "黄焖鸡米饭", "关东煮"),
    )
    assert service.foods == ("黄焖鸡米饭", "螺蛳粉", "关东煮")


# ── OneBot Mixin 拦截 ────────────────────────────────────────


def _eat_server(enabled: bool = True, trust: str = "ok") -> OneBotServer:
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(
        what_to_eat_enabled=enabled,
    )
    server.reply_engine = SimpleNamespace(
        record_bot_reply=lambda *args, **kwargs: None,
        memory=SimpleNamespace(trust_tier=lambda profile_id: trust),
    )
    server._delivery_model_name = lambda model_type: "chat-model"  # type: ignore[method-assign]
    server._mark_smart_group_reply = lambda *args, **kwargs: None  # type: ignore[method-assign]
    server._what_to_eat_service = _service()

    recorded: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        recorded.append(messages[0].content)
        return messages[0].content if messages else ""

    server.send_reply = fake_send_reply  # type: ignore[method-assign]
    server._eat_sent = recorded
    return server


def _eat_event(text: str, message_type: str = "group") -> dict:
    return {
        "post_type": "message",
        "message_type": message_type,
        "group_id": 20001,
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _run_what_to_eat(server: OneBotServer, text: str, message_type: str = "group") -> bool:
    return asyncio.run(
        server._maybe_handle_what_to_eat(
            websocket=None,
            event=_eat_event(text, message_type),
            plain_text=text,
            conversation_id="group:20001" if message_type == "group" else "private:10001",
        )
    )


def test_mixin_group_message_triggers() -> None:
    server = _eat_server()
    handled = _run_what_to_eat(server, "中午吃什么啊")
    assert handled is True
    assert server._eat_sent


def test_mixin_private_message_does_not_trigger() -> None:
    server = _eat_server()
    assert _run_what_to_eat(server, "中午吃什么", message_type="private") is False
    assert server._eat_sent == []


def test_mixin_plain_message_without_keyword_passes() -> None:
    server = _eat_server()
    assert _run_what_to_eat(server, "中午吃面啊") is False
    assert server._eat_sent == []


def test_mixin_yields_to_deterministic_commands() -> None:
    server = _eat_server()
    assert _run_what_to_eat(server, "漫画 搜索 今天吃什么") is False
    assert _run_what_to_eat(server, "亚托莉，漫画 搜索 吃什么食堂") is False
    assert _run_what_to_eat(server, "B站总结 <https://吃什么.bilibili.com>") is False
    assert server._eat_sent == []
    # 无命令前缀的普通消息照常接梗。
    assert _run_what_to_eat(server, "中午吃什么") is True


def test_mixin_disabled_returns_false() -> None:
    server = _eat_server(enabled=False)
    assert _run_what_to_eat(server, "中午吃什么") is False
    assert server._eat_sent == []


def test_mixin_blocked_user_skipped() -> None:
    server = _eat_server(trust="blocked")
    assert _run_what_to_eat(server, "中午吃什么") is False
    assert server._eat_sent == []


def test_mixin_records_reply_in_engine() -> None:
    server = _eat_server()
    recorded: list[Any] = []
    server.reply_engine.record_bot_reply = (  # type: ignore[method-assign]
        lambda conversation_id, text, sticker, **kwargs: recorded.append((conversation_id, text))
    )
    handled = _run_what_to_eat(server, "中午吃什么")
    assert handled is True
    assert recorded and recorded[0][0] == "group:20001"
    assert recorded[0][1] == server._eat_sent[-1]
