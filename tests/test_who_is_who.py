"""Who Is Who（群聊共享上下文，移植自 astrbot_plugin_who_is_who）测试。

覆盖：消息归一化入库、称呼学习（本人/第三方+同意）、长期请求承诺
裁决、/wiw 指令权限、OneBot mixin 拦截、reply() 的 context_system 注入。
"""

import asyncio
from types import SimpleNamespace

from atri_qq_bot.config import BotConfig
from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.who_is_who import PreparedTurn, WhoIsWhoService

BOT_QQ = 100000001
OWNER_QQ = 100000001


def _wiw_config(tmp_path, **overrides):
    values = dict(
        wiw_enabled=True,
        wiw_mode="full",
        wiw_learn_nicknames=True,
        wiw_detect_commitments=True,
        wiw_topic_active_minutes=30,
        wiw_max_messages_per_group=500,
        wiw_retention_days=30,
        bot_qq=BOT_QQ,
        owner_qqs=(OWNER_QQ,),
        wiw_admin_ids=(),
        wiw_restricted_ids=(),
        wiw_enabled_groups=(),
        wiw_blocked_groups=(),
        wiw_db_path=tmp_path / "wiw.db",
        wiw_explicit_users_json="",
        wiw_default_priority=10,
        wiw_authority_enabled=True,
        wiw_self_preference_first=True,
        wiw_allow_whitelist_override=False,
        wiw_newer_rule_wins=True,
        wiw_max_related_messages=8,
        wiw_max_related_people=4,
        wiw_max_related_states=6,
        wiw_max_injection_chars=1600,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(tmp_path, **overrides):
    return WhoIsWhoService(_wiw_config(tmp_path, **overrides))


def _group_event(
    text,
    sender=10001,
    sender_name=None,
    message_id="m1",
    segments=None,
    timestamp=1700000000,
    group_id=20001,
):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": sender,
        "message_id": message_id,
        "time": timestamp,
        "sender": {"card": sender_name or f"成员{sender}", "nickname": f"昵称{sender}"},
        "message": segments
        if segments is not None
        else [{"type": "text", "data": {"text": text}}],
    }


def _private_event(text, sender=10001, message_id="m1"):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": sender,
        "message_id": message_id,
        "time": 1700000000,
        "sender": {"nickname": f"昵称{sender}"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _at_segment(qq, name):
    return {"type": "at", "data": {"qq": str(qq), "name": name}}


# ── 归一化与记录 ───────────────────────────────────────────────


def test_record_group_message_saves_message_and_identity(tmp_path):
    service = _service(tmp_path)
    event = _group_event("今天天气不错", sender=10001, sender_name="螃蟹哥")
    service.record_group_message(event, "今天天气不错")

    stored = service.db.get_message("qq:20001", "m1")
    assert stored is not None
    assert stored["sender_id"] == "10001"
    assert stored["sender_name"] == "螃蟹哥"
    identities = service.db.get_group_identities("qq:20001")
    assert any(row["user_id"] == "10001" for row in identities)


def test_record_ignores_private_messages(tmp_path):
    service = _service(tmp_path)
    service.record_group_message(_private_event("私聊内容"), "私聊内容")
    assert service.db.get_recent_messages("qq:None", 10) == []


def test_enabled_for_event_group_filtering(tmp_path):
    service = _service(tmp_path, wiw_blocked_groups=("20001",))
    assert service.enabled_for_event(_group_event("hi")) is False
    service = _service(tmp_path, wiw_enabled_groups=("20002",))
    assert service.enabled_for_event(_group_event("hi")) is False
    assert service.enabled_for_event(_group_event("hi", group_id=20002)) is True
    assert service.enabled_for_event(_private_event("hi")) is False


# ── 称呼学习 ──────────────────────────────────────────────────


def test_self_preferred_name_learned_and_injected(tmp_path):
    service = _service(tmp_path)
    service.record_group_message(
        _group_event("以后请叫我螃蟹侠", sender=10001, message_id="m1"),
        "以后请叫我螃蟹侠",
    )
    identity = service.db.get_identities("qq:20001", ["10001"])[0]
    assert identity["preferred_name"] == "螃蟹侠"

    prepared = service.prepare_turn(
        _group_event("在吗？", sender=10001, message_id="m2", timestamp=1700000060)
    )
    assert isinstance(prepared, PreparedTurn)
    assert prepared.group_key == "qq:20001"
    assert "螃蟹侠" in prepared.context_text
    assert "【Who Is Who 群聊共享上下文】" in prepared.context_text


def test_record_only_mode_skips_learning(tmp_path):
    service = _service(tmp_path, wiw_mode="record_only")
    service.record_group_message(
        _group_event("以后请叫我螃蟹侠", sender=10001), "以后请叫我螃蟹侠"
    )
    identity = service.db.get_identities("qq:20001", ["10001"])[0]
    assert identity["preferred_name"] == ""
    prepared = service.prepare_turn(_group_event("在吗", message_id="m2"))
    assert "螃蟹侠" not in prepared.context_text


def test_third_party_nickname_waits_for_consent(tmp_path):
    service = _service(tmp_path)
    event = _group_event(
        None,
        sender=10001,
        message_id="m1",
        segments=[_at_segment(10002, "小明"), {"type": "text", "data": {"text": " 以后叫他「螃蟹」"}}],
    )
    service.record_group_message(event, "以后叫他「螃蟹」")

    identity = service.db.get_identities("qq:20001", ["10002"])
    assert not identity or identity[0]["preferred_name"] == ""
    states = service.db.list_states("qq:20001")
    pending = [s for s in states if s["status"] == "pending_consent"]
    assert pending and "螃蟹" in pending[0]["content"]

    service.record_group_message(
        _group_event("我同意这个称呼", sender=10002, message_id="m2"),
        "我同意这个称呼",
    )
    identity = service.db.get_identities("qq:20001", ["10002"])[0]
    assert identity["preferred_name"] == "螃蟹"
    updated = service.db.get_state(pending[0]["id"])
    assert updated["status"] == "active"


def test_third_party_nickname_refused(tmp_path):
    service = _service(tmp_path)
    event = _group_event(
        None,
        sender=10001,
        message_id="m1",
        segments=[_at_segment(10002, "小明"), {"type": "text", "data": {"text": " 以后叫他「螃蟹」"}}],
    )
    service.record_group_message(event, "以后叫他「螃蟹」")
    service.record_group_message(
        _group_event("不要这样叫", sender=10002, message_id="m2"), "不要这样叫"
    )
    states = service.db.list_states("qq:20001")
    assert all(s["status"] != "pending_consent" for s in states)
    identity = service.db.get_identities("qq:20001", ["10002"])
    assert not identity or identity[0]["preferred_name"] == ""


def test_reply_message_resolves_sender_from_local_db(tmp_path):
    service = _service(tmp_path)
    service.record_group_message(
        _group_event("原始消息", sender=10002, sender_name="小明", message_id="m1"),
        "原始消息",
    )
    reply_event = _group_event(
        None,
        sender=10001,
        message_id="m2",
        timestamp=1700000060,
        segments=[
            {"type": "reply", "data": {"id": "m1"}},
            {"type": "text", "data": {"text": " 回复内容"}},
        ],
    )
    message = service.normalize(reply_event)
    assert message.reply_sender_id == "10002"
    assert message.reply_sender_name == "小明"
    assert "原始消息" in (message.reply_text or "")


# ── 承诺裁决 ──────────────────────────────────────────────────


def _commitment_turn(tmp_path, reply_text):
    service = _service(tmp_path)
    event = _group_event("以后每次回复都简短一点", sender=10001, message_id="m1")
    prepared = service.prepare_turn(event)
    assert prepared is not None and prepared.request_id is not None
    service.finish_turn(event, reply_text, prepared)
    return service, prepared


def test_commitment_accepted_records_active_state(tmp_path):
    service, prepared = _commitment_turn(tmp_path, "好的，我会记住的。")
    state = service.db.get_state(prepared.request_id)
    assert state["status"] == "active"
    commitments = [s for s in service.db.list_states("qq:20001") if s["kind"] == "commitment"]
    assert commitments and "AI 已答应" in commitments[0]["content"]


def test_commitment_rejected_when_ai_refuses(tmp_path):
    service, prepared = _commitment_turn(tmp_path, "抱歉，这个我做不到哦。")
    state = service.db.get_state(prepared.request_id)
    assert state["status"] == "rejected"
    commitments = [s for s in service.db.list_states("qq:20001") if s["kind"] == "commitment"]
    assert commitments == []


def test_finish_turn_records_bot_message(tmp_path):
    service = _service(tmp_path)
    event = _group_event("在吗", sender=10001, message_id="m1")
    prepared = service.prepare_turn(event)
    service.finish_turn(event, "在的在的，怎么了？", prepared)
    recent = service.db.get_recent_messages("qq:20001", 5)
    assert any(row["role"] == "bot" and row["sender_name"] == "ATRI" for row in recent)


def test_finish_turn_without_request_still_records_bot_message(tmp_path):
    service = _service(tmp_path)
    event = _group_event("今天天气不错", sender=10001, message_id="m1")
    prepared = service.prepare_turn(event)
    assert prepared.request_id is None
    service.finish_turn(event, "是呢，天气很好。", prepared)
    recent = service.db.get_recent_messages("qq:20001", 5)
    assert any(row["role"] == "bot" for row in recent)


# ── /wiw 指令 ─────────────────────────────────────────────────


def test_handle_command_ignores_non_wiw_text(tmp_path):
    service = _service(tmp_path)
    assert service.handle_command(_group_event("今天天气不错"), "今天天气不错") is None


def test_command_help_and_status(tmp_path):
    service = _service(tmp_path)
    help_text = service.handle_command(_group_event("/wiw help", sender=10001), "/wiw help")
    assert "指令" in help_text
    status = service.handle_command(_group_event("/wiw status", sender=10001), "/wiw status")
    assert "有效优先级" in status


def test_command_prefer_and_forget(tmp_path):
    service = _service(tmp_path)
    reply = service.handle_command(
        _group_event("/wiw prefer 螃蟹侠", sender=10001), "/wiw prefer 螃蟹侠"
    )
    assert "螃蟹侠" in reply
    assert service.db.get_identities("qq:20001", ["10001"])[0]["preferred_name"] == "螃蟹侠"

    reply = service.handle_command(
        _group_event("/wiw forget", sender=10001), "/wiw forget"
    )
    assert "删除" in reply
    assert service.db.get_identities("qq:20001", ["10001"])[0]["preferred_name"] == ""


def test_command_name_requires_admin(tmp_path):
    service = _service(tmp_path)
    reply = service.handle_command(
        _group_event("/wiw name 10002 小明二号", sender=10001), "/wiw name 10002 小明二号"
    )
    assert "插件管理员" in reply
    identity = service.db.get_identities("qq:20001", ["10002"])
    assert not identity or identity[0]["preferred_name"] == ""

    admin_event = _group_event(
        "/wiw name 10002 小明二号", sender=OWNER_QQ, message_id="m2"
    )
    reply = service.handle_command(admin_event, "/wiw name 10002 小明二号")
    assert "小明二号" in reply
    assert (
        service.db.get_identities("qq:20001", ["10002"])[0]["preferred_name"] == "小明二号"
    )


def test_command_rules_and_clear_require_admin(tmp_path):
    service = _service(tmp_path)
    event = _group_event("/wiw rules", sender=10001, message_id="m1")
    assert "权限" in service.handle_command(event, "/wiw rules")

    admin_event = _group_event("/wiw rules", sender=OWNER_QQ, message_id="m2")
    assert "还没有规则" in service.handle_command(admin_event, "/wiw rules")

    confirm_event = _group_event("/wiw clear confirm", sender=OWNER_QQ, message_id="m3")
    assert "清空" in service.handle_command(confirm_event, "/wiw clear confirm")
    assert service.db.list_states("qq:20001") == []
    assert service.db.get_recent_messages("qq:20001", 10) == []


def test_command_with_address_prefix(tmp_path):
    service = _service(tmp_path)
    event = _group_event(None, sender=10001, segments=[])
    event["message"] = [{"type": "text", "data": {"text": "亚托莉，/wiw status"}}]
    reply = service.handle_command(event, "亚托莉，/wiw status")
    assert "有效优先级" in reply


def test_admin_setting_blocks_self_override(tmp_path):
    service = _service(tmp_path)
    admin_event = _group_event("/wiw name 10001 官方称呼", sender=OWNER_QQ, message_id="m1")
    service.handle_command(admin_event, "/wiw name 10001 官方称呼")

    reply = service.handle_command(
        _group_event("/wiw prefer 螃蟹侠", sender=10001, message_id="m2"),
        "/wiw prefer 螃蟹侠",
    )
    assert "不能自行覆盖" in reply
    assert (
        service.db.get_identities("qq:20001", ["10001"])[0]["preferred_name"] == "官方称呼"
    )


# ── OneBot Mixin ──────────────────────────────────────────────


def _wiw_server(service, enabled=True, trust="ok"):
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(wiw_enabled=enabled)
    server.reply_engine = SimpleNamespace(
        record_bot_reply=lambda *args, **kwargs: None,
        memory=SimpleNamespace(trust_tier=lambda profile_id: trust),
    )
    server._delivery_model_name = lambda model_type: "chat-model"  # type: ignore[method-assign]
    server._mark_smart_group_reply = lambda *args, **kwargs: None  # type: ignore[method-assign]
    server._who_is_who_service = service

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        sent.append(messages[0].content)
        return messages[0].content if messages else ""

    server.send_reply = fake_send_reply  # type: ignore[method-assign]
    server._wiw_sent = sent
    return server


def _run_command(server, text, message_type="group", sender=10001):
    event = _group_event(text, sender=sender) if message_type == "group" else _private_event(text)
    return asyncio.run(
        server._maybe_handle_who_is_who_command(
            websocket=None,
            event=event,
            plain_text=text,
            conversation_id="group:20001" if message_type == "group" else "private:10001",
        )
    )


def test_mixin_intercepts_wiw_command(tmp_path):
    server = _wiw_server(_service(tmp_path))
    assert _run_command(server, "/wiw help") is True
    assert any("指令" in text for text in server._wiw_sent)


def test_mixin_ignores_private_and_non_command(tmp_path):
    server = _wiw_server(_service(tmp_path))
    assert _run_command(server, "/wiw help", message_type="private") is False
    assert server._wiw_sent == []
    assert _run_command(server, "今天天气不错") is False
    assert server._wiw_sent == []


def test_mixin_disabled_service_does_not_intercept(tmp_path):
    server = _wiw_server(_service(tmp_path), enabled=False)
    assert _run_command(server, "/wiw help") is False
    assert server._wiw_sent == []


def test_mixin_blocked_trust_does_not_intercept(tmp_path):
    server = _wiw_server(_service(tmp_path), trust="blocked")
    assert _run_command(server, "/wiw help") is False
    assert server._wiw_sent == []


def test_mixin_records_group_message(tmp_path):
    service = _service(tmp_path)
    server = _wiw_server(service)
    server._record_who_is_who_message(_group_event("你好呀"), "你好呀")
    assert service.db.get_message("qq:20001", "m1") is not None

    server._record_who_is_who_message(_private_event("私聊"), "私聊")
    assert service.db.get_message("qq:None", "m1") is None


def test_mixin_prepare_and_finish_turn(tmp_path):
    service = _service(tmp_path)
    server = _wiw_server(service)
    event = _group_event("以后请叫我螃蟹侠", sender=10001, message_id="m1")
    server._record_who_is_who_message(event, "以后请叫我螃蟹侠")

    prepared = server._prepare_who_is_who_turn(
        _group_event("在吗", sender=10001, message_id="m2", timestamp=1700000060)
    )
    assert prepared is not None
    assert "螃蟹侠" in prepared.context_text
    assert server._prepare_who_is_who_turn(_private_event("在吗")) is None

    server._finish_who_is_who_turn(_group_event("在吗", sender=10001), "在的。", prepared)
    recent = service.db.get_recent_messages("qq:20001", 5)
    assert any(row["role"] == "bot" for row in recent)


# ── persona 引擎注入 ──────────────────────────────────────────


def _engine(tmp_path):
    config = BotConfig(
        bot_qq=BOT_QQ,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.4,
        max_tokens=180,
        memory_path=tmp_path / "memory.json",
        llm_tools_enabled=False,
    )
    return AtriReplyEngine(config)


def test_reply_injects_context_system_into_guarded_api(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    captured: dict = {}

    async def fake_guarded_api(conversation_id, user_text, nickname, extra_system=None, **kwargs):
        captured["extra_system"] = extra_system
        return "好的，我记住啦。"

    monkeypatch.setattr(engine, "_reply_with_guarded_api", fake_guarded_api)
    monkeypatch.setattr(engine, "_needs_rewrite", lambda *args, **kwargs: False)

    reply = asyncio.run(
        engine.reply(
            "group:20001",
            "以后请叫我螃蟹侠",
            context_system="【Who Is Who 群聊共享上下文】测试注入",
        )
    )
    assert reply
    assert "【Who Is Who 群聊共享上下文】测试注入" in captured["extra_system"]

    reply = asyncio.run(engine.reply("group:20001", "在吗"))
    assert reply
    assert captured["extra_system"] is None


def test_bot_config_wiw_defaults():
    config = BotConfig(
        bot_qq=1,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.5,
        max_tokens=350,
    )
    assert config.wiw_enabled is True
    assert config.wiw_mode == "full"
    assert config.wiw_learn_nicknames is True
    assert config.wiw_detect_commitments is True
    assert config.wiw_max_injection_chars == 1600
    assert config.wiw_db_path.name == "who_is_who.db"
