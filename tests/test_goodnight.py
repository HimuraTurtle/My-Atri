import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.proactive.goodnight import (
    GOODNIGHT_GREETINGS,
    goodnight_due,
    goodnight_greeting_text,
    mark_goodnight_sent,
)

CHINA = timezone(timedelta(hours=8))


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
        memory_path=tmp_path / "users.json",
        goodnight_image=tmp_path / "goodnight.jpg",
        goodnight_state_path=tmp_path / "goodnight-state.json",
    )


def test_greeting_text_comes_from_pool() -> None:
    for _ in range(20):
        assert goodnight_greeting_text() in GOODNIGHT_GREETINGS


def test_due_window_semantics(tmp_path: Path) -> None:
    state = tmp_path / "goodnight-state.json"
    common = {
        "scheduled_time": "23:30",
        "timezone_name": "Asia/Shanghai",
        "catchup_minutes": 30,
    }

    assert not goodnight_due(state, now=datetime(2026, 9, 19, 23, 0, tzinfo=CHINA), **common)
    assert goodnight_due(state, now=datetime(2026, 9, 19, 23, 30, tzinfo=CHINA), **common)
    assert goodnight_due(state, now=datetime(2026, 9, 19, 23, 59, tzinfo=CHINA), **common)
    # 过了午夜就是新的一天，今天的晚安时间还未到，不再补发昨晚的。
    assert not goodnight_due(state, now=datetime(2026, 9, 20, 0, 0, tzinfo=CHINA), **common)


def test_due_respects_sent_state_across_midnight(tmp_path: Path) -> None:
    state = tmp_path / "goodnight-state.json"
    common = {
        "scheduled_time": "23:30",
        "timezone_name": "Asia/Shanghai",
        "catchup_minutes": 30,
    }
    mark_goodnight_sent(state, timezone_name="Asia/Shanghai", now=datetime(2026, 9, 19, 23, 31, tzinfo=CHINA))

    assert not goodnight_due(state, now=datetime(2026, 9, 19, 23, 35, tzinfo=CHINA), **common)
    assert not goodnight_due(state, now=datetime(2026, 9, 20, 0, 0, tzinfo=CHINA), **common)
    assert not goodnight_due(state, now=datetime(2026, 9, 20, 23, 0, tzinfo=CHINA), **common)
    assert goodnight_due(state, now=datetime(2026, 9, 20, 23, 30, tzinfo=CHINA), **common)


def test_mark_sent_writes_atomic_state(tmp_path: Path) -> None:
    state = tmp_path / "nested" / "goodnight-state.json"
    mark_goodnight_sent(state, timezone_name="Asia/Shanghai", now=datetime(2026, 9, 19, 23, 31, tzinfo=CHINA))

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload == {"last_sent_date": "2026-09-19"}
    assert not state.with_suffix(".tmp").exists()


def test_invalid_time_format_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        goodnight_due(
            tmp_path / "goodnight-state.json",
            scheduled_time="24:30",
            timezone_name="Asia/Shanghai",
            catchup_minutes=30,
        )


def test_delivery_sends_image_and_text_to_all_groups(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.goodnight_image.write_bytes(b"fake-jpeg-bytes")

    server = object.__new__(OneBotServer)
    server.config = config

    recorded_replies: list[tuple[str, str]] = []

    class FakeEngine:
        def record_bot_reply(self, conversation_id: str, text: str) -> None:
            recorded_replies.append((conversation_id, text))

    server.reply_engine = FakeEngine()

    async def fake_fetch_group_list():
        return [
            {"group_id": "111", "group_name": "群A"},
            {"group_id": "222", "group_name": "群B"},
            {"group_id": "", "group_name": "无效"},
        ]

    sent_actions: list[tuple[str, dict]] = []

    async def fake_call_action(websocket, action, params):
        sent_actions.append((action, params))

    server.fetch_group_list = fake_fetch_group_list
    server.call_action = fake_call_action

    asyncio.run(server._send_goodnight_greetings(object()))

    assert [action for action, _ in sent_actions] == ["send_group_msg", "send_group_msg"]
    assert [params["group_id"] for _, params in sent_actions] == [111, 222]
    for _, params in sent_actions:
        segments = params["message"]
        assert segments[0]["type"] == "image"
        assert str(config.goodnight_image) in segments[0]["data"]["file"]
        assert segments[1]["type"] == "text"
        assert segments[1]["data"]["text"] in GOODNIGHT_GREETINGS
    assert recorded_replies == [
        ("group:111", recorded_replies[0][1]),
        ("group:222", recorded_replies[1][1]),
    ]
    assert json.loads(config.goodnight_state_path.read_text(encoding="utf-8")) == {
        "last_sent_date": datetime.now(CHINA).date().isoformat()
    }
    assert not goodnight_due(
        config.goodnight_state_path,
        scheduled_time=config.goodnight_time,
        timezone_name=config.goodnight_timezone,
        catchup_minutes=config.goodnight_catchup_minutes,
    )


def test_delivery_without_image_still_sends_text(tmp_path: Path) -> None:
    config = _config(tmp_path)

    server = object.__new__(OneBotServer)
    server.config = config

    class FakeEngine:
        def record_bot_reply(self, conversation_id: str, text: str) -> None:
            pass

    server.reply_engine = FakeEngine()

    async def fake_fetch_group_list():
        return [{"group_id": "111", "group_name": "群A"}]

    sent_actions: list[tuple[str, dict]] = []

    async def fake_call_action(websocket, action, params):
        sent_actions.append((action, params))

    server.fetch_group_list = fake_fetch_group_list
    server.call_action = fake_call_action

    asyncio.run(server._send_goodnight_greetings(object()))

    segments = sent_actions[0][1]["message"]
    assert [segment["type"] for segment in segments] == ["text"]
    assert segments[0]["data"]["text"] in GOODNIGHT_GREETINGS


def test_delivery_marks_state_only_after_success(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.goodnight_image.write_bytes(b"fake-jpeg-bytes")

    server = object.__new__(OneBotServer)
    server.config = config

    class FakeEngine:
        def record_bot_reply(self, conversation_id: str, text: str) -> None:
            pass

    server.reply_engine = FakeEngine()

    async def fake_fetch_group_list():
        return [{"group_id": "111", "group_name": "群A"}]

    async def failing_call_action(websocket, action, params):
        raise RuntimeError("send failed")

    server.fetch_group_list = fake_fetch_group_list
    server.call_action = failing_call_action

    asyncio.run(server._send_goodnight_greetings(object()))

    assert not config.goodnight_state_path.exists()
    assert goodnight_due(
        config.goodnight_state_path,
        scheduled_time=config.goodnight_time,
        timezone_name=config.goodnight_timezone,
        catchup_minutes=config.goodnight_catchup_minutes,
        now=datetime(2026, 9, 19, 23, 45, tzinfo=CHINA),
    )
