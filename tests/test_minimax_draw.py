"""MiniMax 绘图模块的回归测试。

不触网：配置开关与工具 schema 注册、draw_image 工具执行入口
（fake generate）、投递队列往返、OneBot 层图片投递、错误分支。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from atri_qq_bot import minimax_draw as minimax_draw_pkg
from atri_qq_bot.config import BotConfig
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.minimax_draw import (
    MinimaxDrawError,
    MinimaxDrawService,
    STYLE_PRESETS,
    SUPPORTED_ASPECT_RATIOS,
    get_minimax_draw_service,
)
from atri_qq_bot.minimax_draw import prompt_refiner
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(**overrides: Any) -> BotConfig:
    defaults: dict[str, Any] = {
        "bot_qq": 100000001,
        "host": "127.0.0.1",
        "port": 8765,
        "reply_mode": "mention",
        "openai_api_key": "test-key",
        "openai_base_url": "https://example.com/v1",
        "openai_model": "test-model",
        "temperature": 0.8,
        "max_tokens": 350,
        "minimax_draw_enabled": True,
        "minimax_api_key": "test-minimax-key",
    }
    defaults.update(overrides)
    return BotConfig(**defaults)


@pytest.fixture(autouse=True)
def _reset_singleton():
    minimax_draw_pkg._SERVICE = None
    yield
    minimax_draw_pkg._SERVICE = None


def _fake_image_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0fake-jpeg-payload"


def _success_payload(n: int = 1) -> dict[str, Any]:
    encoded = base64.b64encode(_fake_image_bytes()).decode()
    return {
        "id": "img-1",
        "data": {"image_base64": [encoded] * n},
        "metadata": {"success_count": str(n), "failed_count": "0"},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def _monkeypatch_api(monkeypatch, payload: dict[str, Any], *, status: int = 200) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    class _FakeResponse:
        def __init__(self, body: dict[str, Any]) -> None:
            self.status_code = status
            self._body = body

        def json(self) -> dict[str, Any]:
            return self._body

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _FakeResponse:
            captured.append({"url": url, "headers": headers, "json": json})
            return _FakeResponse(payload)

    def _factory(timeout: float = 0.0) -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(minimax_draw_pkg.service.httpx, "AsyncClient", _factory)
    return captured


# ── 配置与工具注册 ─────────────────────────────────────────────────


def test_disabled_by_default_returns_no_service() -> None:
    assert get_minimax_draw_service(_config(minimax_draw_enabled=False)) is None


def test_disabled_config_registers_no_draw_tools() -> None:
    names = {
        tool["function"]["name"]
        for tool in available_tool_schemas(_config(minimax_draw_enabled=False))
    }
    assert "draw_image" not in names


def test_enabled_config_registers_draw_image() -> None:
    tools = available_tool_schemas(_config())
    names = {tool["function"]["name"] for tool in tools}
    assert "draw_image" in names
    schema = next(t for t in tools if t["function"]["name"] == "draw_image")
    props = schema["function"]["parameters"]["properties"]
    assert set(props["style"]["enum"]) == set(STYLE_PRESETS)
    assert set(props["aspect_ratio"]["enum"]) == set(SUPPORTED_ASPECT_RATIOS)


def test_minimax_and_anima_can_coexist_in_tools() -> None:
    tools = available_tool_schemas(_config(anima_enabled=True))
    names = {tool["function"]["name"] for tool in tools}
    assert "draw_image" in names and "anima_draw" in names


# ── 服务与 API 载荷 ────────────────────────────────────────────────


def test_generate_builds_payload_and_saves_image(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    paths = asyncio.run(
        service.generate(prompt="a crab girl on the beach", aspect_ratio="2:3", style="manga")
    )
    assert len(paths) == 1
    assert paths[0].read_bytes() == _fake_image_bytes()
    request = captured[0]
    assert request["url"] == "https://api.minimax.cn/v1/image_generation"
    assert request["headers"]["Authorization"] == "Bearer test-minimax-key"
    assert request["json"]["model"] == "image-01-live"
    assert request["json"]["aspect_ratio"] == "2:3"
    assert request["json"]["style"]["style_type"] == "漫画"
    assert request["json"]["response_format"] == "base64"


def test_generate_realistic_style_uses_image01(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    asyncio.run(service.generate(prompt="city night photo", style="realistic"))
    assert captured[0]["json"]["model"] == "image-01"
    assert "style" not in captured[0]["json"]


def test_generate_ignores_invalid_ratio(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    asyncio.run(service.generate(prompt="cat", aspect_ratio="21:9", style="auto"))
    assert "aspect_ratio" not in captured[0]["json"]


def test_generate_missing_api_key_raises(tmp_path: Path) -> None:
    service = MinimaxDrawService(_config(minimax_api_key=None))
    with pytest.raises(MinimaxDrawError, match="MINIMAX_API_KEY"):
        asyncio.run(service.generate(prompt="cat"))


def test_generate_balance_error_surfaces_message(monkeypatch, tmp_path: Path) -> None:
    payload = {"id": "x", "data": None, "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}}
    _monkeypatch_api(monkeypatch, payload)
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    with pytest.raises(MinimaxDrawError, match="insufficient balance"):
        asyncio.run(service.generate(prompt="cat"))


# ── 工具入口与投递队列 ────────────────────────────────────────────


def test_tool_draw_queues_delivery(monkeypatch, tmp_path: Path) -> None:
    _monkeypatch_api(monkeypatch, _success_payload())
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    result = asyncio.run(
        service.tool_draw(
            conversation_id="conv-1",
            prompt="a crab girl",
            aspect_ratio="2:3",
            style="auto",
        )
    )
    assert result["ok"] is True
    assert result["image_count"] == 1
    queued = service.consume_deliveries("conv-1")
    assert len(queued) == 1
    assert queued[0].exists()
    assert service.consume_deliveries("conv-1") == []


def test_tool_draw_empty_prompt(monkeypatch, tmp_path: Path) -> None:
    service = MinimaxDrawService(_config())
    result = asyncio.run(service.tool_draw(conversation_id="conv-1", prompt="  "))
    assert result["ok"] is False
    assert not result.get("image_count")


def test_tool_draw_error_returns_failure(monkeypatch, tmp_path: Path) -> None:
    payload = {"id": "x", "data": None, "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}}
    _monkeypatch_api(monkeypatch, payload)
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    result = asyncio.run(service.tool_draw(conversation_id="conv-1", prompt="cat"))
    assert result["ok"] is False
    assert "insufficient balance" in result["message"]
    assert service.consume_deliveries("conv-1") == []


# ── 提示词精炼（prompt_refiner）───────────────────────────────────


def _patch_llm_chat(monkeypatch, outputs: list[str]) -> list[str]:
    """按顺序返回 outputs；记录调用参数。"""
    calls: list[str] = []

    async def _fake_llm_chat(config, system, user, max_tokens=900):
        calls.append(user)
        if not outputs:
            raise RuntimeError("llm exhausted")
        return outputs.pop(0)

    monkeypatch.setattr(prompt_refiner, "_llm_chat", _fake_llm_chat)
    return calls


def _patch_search_web(monkeypatch, result: str = "搜索结果：某角色 银发 红瞳 黑色哥特裙") -> list[str]:
    queries: list[str] = []

    async def _fake_search_web(arguments, config=None):
        queries.append(str(arguments.get("query")))
        return result

    monkeypatch.setattr(
        "atri_qq_bot.llm_tools.web_search_tool.search_web", _fake_search_web
    )
    return queries


def test_refine_prompt_no_search_needed(monkeypatch) -> None:
    _patch_llm_chat(
        monkeypatch,
        ['{"search_queries": [], "refined_prompt": "a crab girl with claws"}'],
    )
    queries = _patch_search_web(monkeypatch)
    prompt, used_search = asyncio.run(prompt_refiner.refine_prompt(_config(), "螃蟹娘"))
    assert prompt == "a crab girl with claws"
    assert used_search is False
    assert queries == []


def test_refine_prompt_with_character_search(monkeypatch) -> None:
    _patch_llm_chat(
        monkeypatch,
        [
            '{"search_queries": ["某角色 外貌 特征 appearance"], "refined_prompt": "a character, details"}',
            "final prompt with silver hair and red eyes",
        ],
    )
    queries = _patch_search_web(monkeypatch)
    prompt, used_search = asyncio.run(prompt_refiner.refine_prompt(_config(), "画某角色"))
    assert prompt == "final prompt with silver hair and red eyes"
    assert used_search is True
    assert queries == ["某角色 外貌 特征 appearance"]


def test_refine_prompt_json_with_fences(monkeypatch) -> None:
    _patch_llm_chat(
        monkeypatch,
        ['```json\n{"search_queries": [], "refined_prompt": "fenced prompt"}\n```'],
    )
    _patch_search_web(monkeypatch)
    prompt, _ = asyncio.run(prompt_refiner.refine_prompt(_config(), "cat"))
    assert prompt == "fenced prompt"


def test_refine_prompt_llm_failure_falls_back(monkeypatch) -> None:
    async def _failing_llm(config, system, user, max_tokens=900):
        raise RuntimeError("llm down")

    monkeypatch.setattr(prompt_refiner, "_llm_chat", _failing_llm)
    prompt, used_search = asyncio.run(prompt_refiner.refine_prompt(_config(), "螃蟹娘"))
    assert prompt == "螃蟹娘"
    assert used_search is False


def test_parse_step1_truncated_json_salvages_draft() -> None:
    truncated = (
        '{"search_queries": ["文芳乃爱 外貌 特征 appearance"], '
        '"refined_prompt": "silver-haired girl on beach at suns'
    )
    queries, draft = prompt_refiner._parse_step1(truncated)
    assert queries == ["文芳乃爱 外貌 特征 appearance"]
    assert draft == "silver-haired girl on beach at suns"


def test_parse_step1_json_wrapped_in_prose() -> None:
    wrapped = '好的，结果：\n{"search_queries": ["Q"], "refined_prompt": "a cat"}\n以上'
    queries, draft = prompt_refiner._parse_step1(wrapped)
    assert queries == ["Q"]
    assert draft == "a cat"


def test_llm_chat_retries_on_transient_failure(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            }

    class _FakeClient:
        attempts = {"n": 0}

        def __init__(self, timeout=None):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            _FakeClient.attempts["n"] += 1
            if _FakeClient.attempts["n"] == 1:
                raise RuntimeError("timeout")

            return _FakeResponse()

    async def _no_sleep(seconds):
        return None

    monkeypatch.setattr(prompt_refiner.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(prompt_refiner.asyncio, "sleep", _no_sleep)
    result = asyncio.run(
        prompt_refiner._llm_chat(_config(), "sys", "user")
    )
    assert result == "ok"
    assert _FakeClient.attempts["n"] == 2


def test_refine_prompt_search_failure_keeps_draft(monkeypatch) -> None:
    _patch_llm_chat(
        monkeypatch,
        ['{"search_queries": ["x 外貌"], "refined_prompt": "draft prompt"}'],
    )

    async def _failing_search(arguments, config=None):
        return "搜索失败：网络不可达。不要编造实时信息。"

    monkeypatch.setattr(
        "atri_qq_bot.llm_tools.web_search_tool.search_web", _failing_search
    )
    prompt, used_search = asyncio.run(prompt_refiner.refine_prompt(_config(), "画某人"))
    assert prompt == "draft prompt"
    assert used_search is False


def test_tool_draw_refine_disabled(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())
    llm_calls = _patch_llm_chat(monkeypatch, [])
    config = _config(minimax_prompt_refine_enabled=False)
    service = MinimaxDrawService(config)
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    result = asyncio.run(
        service.tool_draw(conversation_id="conv-1", prompt="a crab girl", style="auto")
    )
    assert result["ok"] is True
    assert result["prompt_used"] == "a crab girl"
    assert llm_calls == []
    assert captured[0]["json"]["prompt"] == "a crab girl"


def test_tool_draw_refine_failure_still_draws(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())

    async def _failing_llm(config, system, user, max_tokens=900):
        raise RuntimeError("llm down")

    monkeypatch.setattr(prompt_refiner, "_llm_chat", _failing_llm)
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    result = asyncio.run(
        service.tool_draw(conversation_id="conv-1", prompt="a crab girl", style="auto")
    )
    assert result["ok"] is True
    assert result["prompt_used"] == "a crab girl"
    assert captured[0]["json"]["prompt"] == "a crab girl"


def test_tool_draw_uses_refined_prompt(monkeypatch, tmp_path: Path) -> None:
    captured = _monkeypatch_api(monkeypatch, _success_payload())
    _patch_llm_chat(
        monkeypatch,
        ['{"search_queries": [], "refined_prompt": "refined english prompt"}'],
    )
    _patch_search_web(monkeypatch)
    service = MinimaxDrawService(_config())
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    result = asyncio.run(
        service.tool_draw(conversation_id="conv-1", prompt="螃蟹娘", style="auto")
    )
    assert result["ok"] is True
    assert result["prompt_used"] == "refined english prompt"
    assert result.get("prompt_refined_with_search") is False
    assert captured[0]["json"]["prompt"] == "refined english prompt"


# ── persona 工具执行入口 ───────────────────────────────────────────


def _engine() -> EngineToolsMixin:
    return EngineToolsMixin()


def test_engine_tool_draw_image_returns_json(monkeypatch, tmp_path: Path) -> None:
    _monkeypatch_api(monkeypatch, _success_payload())
    minimax_draw_pkg._SERVICE = None
    config = _config()
    service = get_minimax_draw_service(config)
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    engine = _engine()
    result = asyncio.run(
        engine._execute_minimax_draw_tool(
            "conv-1",
            {"prompt": "a crab girl", "aspect_ratio": "2:3", "style": "auto"},
            config,
        )
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert service.consume_deliveries("conv-1")


def test_engine_tool_disabled(monkeypatch) -> None:
    engine = _engine()
    result = asyncio.run(
        engine._execute_minimax_draw_tool("conv-1", {"prompt": "cat"}, _config(minimax_draw_enabled=False))
    )
    assert "未启用" in result


def test_engine_tool_empty_prompt(monkeypatch) -> None:
    engine = _engine()
    result = asyncio.run(
        engine._execute_minimax_draw_tool("conv-1", {"prompt": ""}, _config())
    )
    assert "prompt 为空" in result


# ── OneBot 投递层 ──────────────────────────────────────────────────


def test_onebot_delivers_pending_minimax_images(monkeypatch, tmp_path: Path) -> None:
    from atri_qq_bot.onebot.server import OneBotServer

    _monkeypatch_api(monkeypatch, _success_payload())
    minimax_draw_pkg._SERVICE = None
    config = _config()
    service = get_minimax_draw_service(config)
    monkeypatch.setattr(service, "_output_dir", tmp_path)
    data = asyncio.run(
        service.tool_draw(conversation_id="conv-1", prompt="a crab girl", style="auto")
    )
    assert data["ok"] is True

    sent: list[dict[str, Any]] = []

    async def _fake_send_reply(websocket, event, messages, model_type, model_name):
        sent.append({"messages": messages, "model_type": model_type})

    server = OneBotServer.__new__(OneBotServer)
    server.config = config
    server.send_reply = _fake_send_reply
    server._delivery_model_name = lambda model_type: "test-model"
    asyncio.run(
        server._deliver_pending_minimax_draw(None, {"post_type": "message"}, "conv-1")
    )
    assert len(sent) == 1
    assert sent[0]["messages"][0].kind == "image"
    assert Path(sent[0]["messages"][0].content).exists()


def test_onebot_no_pending_is_noop(monkeypatch) -> None:
    from atri_qq_bot.onebot.server import OneBotServer

    minimax_draw_pkg._SERVICE = None
    server = OneBotServer.__new__(OneBotServer)
    server.config = _config()
    sent: list[Any] = []

    async def _fake_send_reply(websocket, event, messages, model_type, model_name):
        sent.append(messages)

    server.send_reply = _fake_send_reply
    server._delivery_model_name = lambda model_type: "test-model"
    asyncio.run(
        server._deliver_pending_minimax_draw(None, {"post_type": "message"}, "conv-empty")
    )
    assert sent == []
