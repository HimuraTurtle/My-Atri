"""Milky 接入层的协议翻译单测。

覆盖纯函数翻译（消息 ID、段、事件、动作、响应塑造），不涉及网络。
"""

from __future__ import annotations

from pathlib import Path

from atri_qq_bot.milky.actions import shape_response, translate_action
from atri_qq_bot.milky.protocol import (
    decode_message_id,
    encode_message_id,
    event_milky_to_onebot,
    message_milky_to_onebot,
    message_onebot_to_milky,
    segment_milky_to_onebot,
    segment_onebot_to_milky,
    to_file_uri,
)


class TestMessageId:
    def test_roundtrip(self):
        encoded = encode_message_id("group", 12345, 678)
        assert decode_message_id(encoded) == ("group", 12345, 678)

    def test_decode_rejects_garbage(self):
        assert decode_message_id("123456") is None
        assert decode_message_id("not-a-milky-id") is None


class TestFileUri:
    def test_passthrough_schemes(self):
        assert to_file_uri("file:///a.png") == "file:///a.png"
        assert to_file_uri("https://x/a.png") == "https://x/a.png"

    def test_local_path(self):
        assert to_file_uri("C:/a.png") == Path("C:/a.png").absolute().as_uri()

    def test_empty(self):
        assert to_file_uri("") is None


class TestIncomingSegment:
    def test_text(self):
        assert segment_milky_to_onebot({"type": "text", "data": {"text": "hi"}}, "friend", 1) == {
            "type": "text", "data": {"text": "hi"},
        }

    def test_mention_to_at(self):
        assert segment_milky_to_onebot({"type": "mention", "data": {"user_id": 7}}, "group", 1) == {
            "type": "at", "data": {"qq": "7"},
        }

    def test_mention_all(self):
        assert segment_milky_to_onebot({"type": "mention_all", "data": {}}, "group", 1) == {
            "type": "at", "data": {"qq": "all"},
        }

    def test_face_id(self):
        assert segment_milky_to_onebot({"type": "face", "data": {"face_id": "5"}}, "friend", 1) == {
            "type": "face", "data": {"id": "5"},
        }

    def test_reply_uses_synthetic_id(self):
        assert segment_milky_to_onebot({"type": "reply", "data": {"message_seq": 42}}, "group", 999) == {
            "type": "reply", "data": {"id": "milky:group:999:42"},
        }

    def test_market_face(self):
        assert segment_milky_to_onebot(
            {"type": "market_face", "data": {"summary": "s", "url": "u"}}, "friend", 1
        ) == {"type": "mface", "data": {"summary": "s", "url": "u"}}


class TestEvent:
    def test_private_message(self):
        evt = event_milky_to_onebot({
            "time": 1, "self_id": 100, "event_type": "message_receive",
            "data": {
                "message_scene": "friend", "peer_id": 100, "message_seq": 5,
                "sender_id": 200, "time": 1,
                "segments": [{"type": "text", "data": {"text": "hi"}}],
            },
        })
        assert evt["post_type"] == "message"
        assert evt["message_type"] == "private"
        assert evt["user_id"] == 200
        assert evt["message_id"] == "milky:friend:100:5"
        assert evt["message"][0] == {"type": "text", "data": {"text": "hi"}}

    def test_group_message(self):
        evt = event_milky_to_onebot({
            "time": 1, "self_id": 100, "event_type": "message_receive",
            "data": {
                "message_scene": "group", "peer_id": 888, "message_seq": 5,
                "sender_id": 200, "time": 1,
                "segments": [{"type": "text", "data": {"text": "hi"}}],
            },
        })
        assert evt["message_type"] == "group"
        assert evt["group_id"] == 888
        assert evt["user_id"] == 200

    def test_group_nudge(self):
        evt = event_milky_to_onebot({
            "time": 1, "self_id": 100, "event_type": "group_nudge",
            "data": {"group_id": 888, "sender_id": 200, "receiver_id": 100},
        })
        assert evt["post_type"] == "notice"
        assert evt["notice_type"] == "poke"
        assert evt["user_id"] == 200
        assert evt["target_id"] == 100

    def test_unrelated_event_ignored(self):
        assert event_milky_to_onebot({"time": 1, "self_id": 1, "event_type": "bot_offline", "data": {}}) is None


class TestOutgoingSegment:
    def test_text_string(self):
        assert segment_onebot_to_milky("hello") == {"type": "text", "data": {"text": "hello"}}

    def test_at_all(self):
        assert segment_onebot_to_milky({"type": "at", "data": {"qq": "all"}}) == {
            "type": "mention_all", "data": {},
        }

    def test_at_user(self):
        assert segment_onebot_to_milky({"type": "at", "data": {"qq": "7"}}) == {
            "type": "mention", "data": {"user_id": 7},
        }

    def test_image_to_uri(self):
        out = segment_onebot_to_milky({"type": "image", "data": {"file": "C:/a.png"}})
        assert out["type"] == "image"
        assert out["data"]["uri"] == Path("C:/a.png").absolute().as_uri()
        assert out["data"]["sub_type"] == "normal"

    def test_sticker_subtype(self):
        out = segment_onebot_to_milky(
            {"type": "image", "data": {"file": "C:/a.png", "sub_type": 1, "summary": "s"}}
        )
        assert out["data"]["sub_type"] == "sticker"
        assert out["data"]["summary"] == "s"


class TestActions:
    def test_get_status(self):
        assert translate_action("get_status", {}) == ("get_login_info", {}, {})

    def test_get_cookies(self):
        assert translate_action("get_cookies", {"domain": "x"}) == (
            "get_cookies", {"domain": "x"}, {},
        )

    def test_get_msg_decodes_id(self):
        api, body, ctx = translate_action("get_msg", {"message_id": "milky:group:888:42"})
        assert api == "get_message"
        assert body == {"message_scene": "group", "peer_id": 888, "message_seq": 42}
        assert ctx == {"scene": "group", "peer_id": 888}

    def test_get_msg_bad_id(self):
        assert translate_action("get_msg", {"message_id": "123"}) is None

    def test_send_group_msg(self):
        api, body, _ = translate_action("send_group_msg", {"group_id": 1, "message": "hi"})
        assert api == "send_group_message"
        assert body["group_id"] == 1
        assert body["message"] == [{"type": "text", "data": {"text": "hi"}}]

    def test_upload_group_file(self):
        api, body, _ = translate_action("upload_group_file", {"group_id": 1, "file": "C:/a.docx"})
        assert api == "upload_group_file"
        assert body["file_uri"] == Path("C:/a.docx").absolute().as_uri()
        assert body["file_name"] == "a.docx"


class TestShapeResponse:
    def test_get_status_synthesizes_online(self):
        resp = shape_response("get_status", "e", {"status": "ok", "retcode": 0, "data": {"uin": 1, "nickname": "n"}}, {})
        assert resp["echo"] == "e"
        assert resp["data"]["online"] is True
        assert resp["data"]["good"] is True

    def test_get_group_list_flattens(self):
        resp = shape_response("get_group_list", "e", {"status": "ok", "retcode": 0, "data": {"groups": [{"group_id": 1, "group_name": "g"}]}}, {})
        assert resp["data"] == [{"group_id": "1", "group_name": "g"}]

    def test_get_msg_shapes_segments(self):
        resp = shape_response(
            "get_msg", "e",
            {"status": "ok", "retcode": 0, "data": {"message": {"segments": [{"type": "text", "data": {"text": "x"}}]}}},
            {"scene": "friend", "peer_id": 1},
        )
        assert resp["data"] == {"message": [{"type": "text", "data": {"text": "x"}}]}

    def test_failed_response_passthrough(self):
        resp = shape_response("send_group_msg", "e", {"status": "failed", "retcode": -403, "message": "未登录"}, {})
        assert resp["retcode"] == -403
        assert resp["status"] == "failed"
        assert resp["message"] == "未登录"
