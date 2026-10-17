"""AnimeTrace 识图链路测试（移植自 astrbot_plugin_shitu）。

覆盖：供应商结果解析与限流码处理、trace.moe 并行链路合并、身份裁决
（animetrace 单独 / 与 trace.moe 叠加 / saucenao 兜底）、头像识别命令
（目标解析、冷却、开关、回复格式）。全部离线：外部接口一律 mock。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from atri_qq_bot.toolbox import (
    ReverseSearchHit,
    _adjudicate_reverse_identity,
    reverse_image_search as _reverse_image_search_impl,
)
from atri_qq_bot.toolbox import helpers as toolbox_helpers
from atri_qq_bot.onebot.server import OneBotServer

# conftest 的 _hermetic_vision_environment 会把 helpers._animetrace_hits
# patch 成空结果（防止链路测试触碰真实外网）；直接测供应商实现的用例在
# 导入期先捕获真实函数，绕过该 mock（fixture 只在测试运行期生效）。
_real_animetrace_hits = toolbox_helpers._animetrace_hits


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAnimetraceClient:
    """模拟 httpx.AsyncClient，同时服务 model/list 与 search 两个端点。"""

    payload: dict = {}
    last_request: dict[str, Any] = {}

    def __init__(self, timeout: float = 0.0, **kwargs: Any) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(
            {
                "code": 0,
                "data": [
                    {"id": "animetrace-yuri-4.2", "enabled": True, "default": True},
                    {"id": "animetrace-chino-2.3", "enabled": False},
                ],
            }
        )

    async def post(self, url: str, data: dict = None, files: dict = None) -> _FakeResponse:
        _FakeAnimetraceClient.last_request = {"url": url, "data": data, "files": files}
        return _FakeResponse(_FakeAnimetraceClient.payload)


def _fresh_model_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        toolbox_helpers, "_animetrace_model_cache", {"id": "", "ts": 0.0}
    )


def _install_fake_http(monkeypatch, payload: dict) -> None:
    import httpx

    _FakeAnimetraceClient.payload = payload
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAnimetraceClient)
    _fresh_model_cache(monkeypatch)


def _animetrace_hit(name: str = "アトリ", work: str = "ATRI -My Dear Moments-") -> ReverseSearchHit:
    return ReverseSearchHit(
        provider="animetrace",
        title=work,
        characters=name,
        similarity=100.0,
        confident=True,
        detail="Top-1 候选，共 1 项",
    )


# ── 供应商解析 ────────────────────────────────────────────────


def test_animetrace_hits_parses_multi_face_top_candidates(monkeypatch) -> None:
    _install_fake_http(
        monkeypatch,
        {
            "code": 0,
            "data": [
                {
                    "box": [0.1, 0.1, 0.4, 0.5],
                    "character": [
                        {"character": "アトリ", "work": "ATRI -My Dear Moments-"},
                        {"character": "其他角色", "work": "别的作品"},
                    ],
                },
                {
                    "box": [0.5, 0.2, 0.8, 0.6],
                    "character": [{"character": "ヨル", "work": "SPY×FAMILY"}],
                },
            ],
        },
    )
    hits = asyncio.run(_real_animetrace_hits(b"image-bytes"))
    assert [hit.provider for hit in hits] == ["animetrace", "animetrace"]
    assert hits[0].characters == "アトリ"
    assert hits[0].title == "ATRI -My Dear Moments-"
    assert hits[0].confident is True
    assert "共 2 项" in hits[0].detail
    assert hits[1].characters == "ヨル"
    assert hits[0].similarity > hits[1].similarity
    # 请求参数：多角色检测开启、使用默认模型
    request = _FakeAnimetraceClient.last_request
    assert request["data"]["is_multi"] == "1"
    assert request["data"]["model"] == "animetrace-yuri-4.2"


def test_animetrace_hits_skips_rate_limit_and_error_codes(monkeypatch) -> None:
    for code in (17722, 17728, 17731, 17702, 17707):
        _install_fake_http(monkeypatch, {"code": code, "zh_message": "err"})
        assert asyncio.run(_real_animetrace_hits(b"x")) == []


def test_animetrace_hits_empty_data_returns_empty(monkeypatch) -> None:
    _install_fake_http(monkeypatch, {"code": 0, "data": []})
    assert asyncio.run(_real_animetrace_hits(b"plain-photo")) == []


def test_animetrace_jpeg_converts_png_and_resizes() -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2048, 1024), "red").save(buffer, format="PNG")
    converted = toolbox_helpers._animetrace_jpeg(buffer.getvalue())
    with Image.open(io.BytesIO(converted)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 1024


# ── 反搜链路 ────────────────────────────────────────────────


def test_reverse_search_merges_tracemoe_and_animetrace(monkeypatch) -> None:
    async def fake_tracemoe(data: bytes) -> list:
        return [
            ReverseSearchHit(
                provider="trace.moe",
                title="葬送的芙莉莲",
                episode="第1话",
                similarity=93.0,
                confident=True,
            )
        ]

    async def fake_animetrace(data: bytes) -> list:
        return [_animetrace_hit("フリーレン", "葬送的芙莉莲")]

    async def fail_saucenao(data: bytes, api_key: str) -> list:
        raise AssertionError("已有命中时不应再查 SauceNAO")

    monkeypatch.setattr(toolbox_helpers, "_tracemoe_hits", fake_tracemoe)
    monkeypatch.setattr(toolbox_helpers, "_animetrace_hits", fake_animetrace)
    monkeypatch.setattr(toolbox_helpers, "_saucenao_hits", fail_saucenao)

    hits = asyncio.run(_reverse_image_search_impl(b"merge-image", "", True))
    assert {hit.provider for hit in hits} == {"trace.moe", "animetrace"}


def test_reverse_search_can_disable_animetrace(monkeypatch) -> None:
    async def fail_animetrace(data: bytes) -> list:
        raise AssertionError("开关关闭时不应调用 AnimeTrace")

    async def fake_tracemoe(data: bytes) -> list:
        return []

    monkeypatch.setattr(toolbox_helpers, "_animetrace_hits", fail_animetrace)
    monkeypatch.setattr(toolbox_helpers, "_tracemoe_hits", fake_tracemoe)

    hits = asyncio.run(_reverse_image_search_impl(b"disabled-animetrace", "", False))
    assert hits == []


# ── 身份裁决 ─────────────────────────────────────────────────


def test_adjudicate_animetrace_confirms_identity() -> None:
    vision_text = (
        "直接回答：一个白发少女；身份/出处：未确认；置信度：低；"
        "需要联网核验：是；检索关键词：白发少女"
    )
    finding, revised = _adjudicate_reverse_identity(vision_text, [_animetrace_hit()])
    assert "身份裁决" in finding
    assert "「アトリ」" in finding
    assert "AnimeTrace 角色库人脸匹配" in finding
    assert "身份/出处：画面角色为「アトリ」（出自《ATRI -My Dear Moments-》）" in revised
    assert "置信度：高" in revised
    assert "需要联网核验：否" in revised


def test_adjudicate_combines_tracemoe_and_animetrace() -> None:
    hits = [
        ReverseSearchHit(
            provider="trace.moe",
            title="葬送的芙莉莲",
            episode="第28话",
            similarity=94.0,
            confident=True,
        ),
        _animetrace_hit("フリーレン", "葬送的芙莉莲"),
    ]
    finding, _ = _adjudicate_reverse_identity("身份/出处：未确认；", hits)
    assert "《葬送的芙莉莲》第28话画面" in finding
    assert "画面角色为「フリーレン」（出自《葬送的芙莉莲》）" in finding
    assert "trace.moe 帧级匹配" in finding


def test_adjudicate_animetrace_lists_multiple_faces() -> None:
    hits = [
        _animetrace_hit("アトリ", "ATRI -My Dear Moments-"),
        ReverseSearchHit(
            provider="animetrace",
            title="SPY×FAMILY",
            characters="アーニャ",
            similarity=95.0,
            confident=True,
        ),
    ]
    finding, _ = _adjudicate_reverse_identity("", hits)
    assert "「アトリ」（出自《ATRI -My Dear Moments-》）" in finding
    assert "「アーニャ」（出自《SPY×FAMILY》）" in finding
    assert "检测到 2 张人脸" in finding


def test_adjudicate_keeps_saucenao_fallback() -> None:
    hits = [
        ReverseSearchHit(
            provider="saucenao",
            title="pixiv:123",
            characters="亚托莉",
            similarity=88.0,
            confident=True,
        )
    ]
    finding, _ = _adjudicate_reverse_identity("", hits)
    assert "SauceNAO" in finding
    assert "亚托莉" in finding


def test_animetrace_finding_text_format() -> None:
    text = _animetrace_hit().finding_text()
    assert "反向搜图（AnimeTrace）" in text
    assert "「アトリ」" in text
    assert "《ATRI -My Dear Moments-》" in text
    assert "直接作为角色身份依据" in text
    assert "共 1 项" in text


# ── 头像识别命令 ─────────────────────────────────────────────


def _avatar_server(animetrace_enabled: bool = True) -> OneBotServer:
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(
        animetrace_enabled=animetrace_enabled,
        saucenao_api_key="",
        bot_qq=10000,
    )
    server.reply_engine = SimpleNamespace(
        record_bot_reply=lambda *args, **kwargs: None
    )
    server._delivery_model_name = lambda model_type: "chat-model"  # type: ignore[method-assign]
    server._mark_smart_group_reply = lambda *args, **kwargs: None  # type: ignore[method-assign]

    sent: list[list] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        sent.append(messages)
        return messages[0].content if messages else ""

    server.send_reply = fake_send_reply  # type: ignore[method-assign]
    server._avatar_sent = sent
    return server


def _avatar_event(text: str, segments: list | None = None, user_id: int = 10001) -> dict:
    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 20001,
        "user_id": user_id,
        "message": segments,
    }


def test_avatar_command_recognizes_self_by_default(monkeypatch) -> None:
    server = _avatar_server()
    targets: list[str] = []

    async def fake_fetch(qq: str) -> bytes:
        targets.append(qq)
        return b"avatar-bytes"

    async def fake_reverse(data: bytes, saucenao_key: str = "", animetrace_enabled: bool = True) -> list:
        assert data == b"avatar-bytes"
        return [_animetrace_hit()]

    monkeypatch.setattr(server, "_fetch_qq_avatar", fake_fetch)
    import atri_qq_bot.toolbox as toolbox

    monkeypatch.setattr(toolbox, "reverse_image_search", fake_reverse)

    handled = asyncio.run(
        server._maybe_handle_avatar_command(
            websocket=None,
            event=_avatar_event("atri，头像识别"),
            plain_text="atri，头像识别",
            conversation_id="group:20001",
        )
    )
    assert handled is True
    assert targets == ["10001"]
    reply = server._avatar_sent[-1][0].content
    assert "アトリ" in reply
    assert "《ATRI -My Dear Moments-》" in reply


def test_avatar_command_resolves_at_and_qq_number_targets(monkeypatch) -> None:
    server = _avatar_server()
    segments = [
        {"type": "at", "data": {"qq": "10000"}},
        {"type": "text", "data": {"text": " 头像识别 "}},
        {"type": "at", "data": {"qq": "12345"}},
    ]
    assert server._avatar_resolve_target(_avatar_event("x", segments), "头像识别") == "12345"

    assert server._avatar_resolve_target(_avatar_event("头像识别 888999"), "头像识别 888999") == "888999"

    plain = [{"type": "text", "data": {"text": "头像识别"}}]
    assert server._avatar_resolve_target(_avatar_event("头像识别", plain), "头像识别") == "10001"


def test_avatar_command_ignores_non_command_and_disabled(monkeypatch) -> None:
    server = _avatar_server()
    handled = asyncio.run(
        server._maybe_handle_avatar_command(
            websocket=None,
            event=_avatar_event("今天天气不错"),
            plain_text="今天天气不错",
            conversation_id="group:20001",
        )
    )
    assert handled is False
    assert server._avatar_sent == []

    disabled = _avatar_server(animetrace_enabled=False)
    handled = asyncio.run(
        disabled._maybe_handle_avatar_command(
            websocket=None,
            event=_avatar_event("头像识别"),
            plain_text="头像识别",
            conversation_id="group:20001",
        )
    )
    assert handled is False


def test_avatar_command_cooldown_blocks_repeats(monkeypatch) -> None:
    server = _avatar_server()

    async def fake_recognize(qq: str) -> str:
        return "识别结果"

    monkeypatch.setattr(server, "_recognize_avatar", fake_recognize)
    first = asyncio.run(
        server._maybe_handle_avatar_command(
            None, _avatar_event("头像识别"), "头像识别", "group:20001"
        )
    )
    second = asyncio.run(
        server._maybe_handle_avatar_command(
            None, _avatar_event("头像识别"), "头像识别", "group:20001"
        )
    )
    assert first is True
    assert second is True
    assert "太频繁" in server._avatar_sent[-1][0].content


def test_avatar_command_reports_no_hit(monkeypatch) -> None:
    server = _avatar_server()

    async def fake_recognize(qq: str) -> str:
        return "这张头像没认出来……角色库里没有检测到二次元角色，可能是真人照片、风景图，或者是还没收录的角色～"

    monkeypatch.setattr(server, "_recognize_avatar", fake_recognize)
    asyncio.run(
        server._maybe_handle_avatar_command(
            None, _avatar_event("头像识别"), "头像识别", "group:20001"
        )
    )
    assert "没认出来" in server._avatar_sent[-1][0].content
