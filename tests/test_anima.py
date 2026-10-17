"""Anima 绘画助手移植的回归测试。

不触网、不依赖 ComfyUI：配置开关与工具 schema 注册、AstrBot 配置
一次性导入、投递队列往返、tool_generate 门面（fake llm_bridge）、
persona 工具执行入口与 OneBot 层图片投递。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atri_qq_bot.anima import (
    AnimaService,
    get_anima_service,
    reset_anima_service,
)
from atri_qq_bot.anima.compat import Comp
from atri_qq_bot.config import BotConfig, load_config
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(tmp_path: Path, **overrides: Any) -> BotConfig:
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
    }
    defaults.update(overrides)
    return BotConfig(**defaults)


@pytest.fixture(autouse=True)
def _reset_anima_singleton():
    reset_anima_service()
    yield
    reset_anima_service()


def _service(tmp_path: Path, monkeypatch, **overrides: Any) -> AnimaService:
    data_dir = tmp_path / "anima"
    monkeypatch.setattr("atri_qq_bot.anima._DATA_DIR", data_dir)
    monkeypatch.setattr("atri_qq_bot.anima._RUNTIME_CONFIG", data_dir / "config.json")
    monkeypatch.setattr(
        "atri_qq_bot.anima._ASTRBOT_SOURCE_CONFIG", tmp_path / "missing.json"
    )
    config = _config(tmp_path, anima_enabled=True, **overrides)
    service = get_anima_service(config)
    assert service is not None
    return service


# ── 配置与工具注册 ─────────────────────────────────────────────────


def test_disabled_by_default_returns_no_service(tmp_path: Path) -> None:
    assert get_anima_service(_config(tmp_path)) is None


def test_disabled_config_registers_no_anima_tools(tmp_path: Path) -> None:
    names = {tool["function"]["name"] for tool in available_tool_schemas(_config(tmp_path))}
    assert not any(name.startswith("anima_") for name in names)


def test_enabled_config_registers_anima_tools(tmp_path: Path) -> None:
    names = {
        tool["function"]["name"]
        for tool in available_tool_schemas(_config(tmp_path, anima_enabled=True))
    }
    assert {"anima_draw", "anima_status"} <= names


def test_loader_reads_anima_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ANIMA_ENABLED=true",
                "ANIMA_PROMPT_MODEL=glm-4.7",
                "ANIMA_PROMPT_BASE_URL=https://example.com/v1/",
                "ANIMA_TOOL_TIMEOUT_SECONDS=600",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(env_file)
    assert config.anima_enabled is True
    assert config.anima_prompt_model == "glm-4.7"
    assert config.anima_prompt_base_url == "https://example.com/v1"
    assert config.anima_tool_timeout_seconds == 600.0


# ── 运行配置载入 ────────────────────────────────────────────────────


def test_runtime_config_defaults_written_once(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    config_path = tmp_path / "anima" / "config.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text("utf-8"))
    assert data["comfyui_base_url"] == "http://127.0.0.1:8188"


def test_runtime_config_imports_astrbot_source_once(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "anima"
    source = tmp_path / "astrbot-config.json"
    source.write_text(
        json.dumps({"comfyui_base_url": "http://192.168.1.5:8188", "steps": 24}),
        encoding="utf-8",
    )
    monkeypatch.setattr("atri_qq_bot.anima._DATA_DIR", data_dir)
    monkeypatch.setattr("atri_qq_bot.anima._RUNTIME_CONFIG", data_dir / "config.json")
    monkeypatch.setattr("atri_qq_bot.anima._ASTRBOT_SOURCE_CONFIG", source)

    service = get_anima_service(_config(tmp_path, anima_enabled=True))
    assert service is not None
    assert service.config["comfyui_base_url"] == "http://192.168.1.5:8188"
    assert service.config["steps"] == 24
    # 导入一次后落盘，后续启动读自身配置，不再看 AstrBot 侧。
    data = json.loads((data_dir / "config.json").read_text("utf-8"))
    assert data["comfyui_base_url"] == "http://192.168.1.5:8188"


# ── 投递队列与门面 ──────────────────────────────────────────────────


def test_delivery_queue_roundtrip(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    event = service._make_event("group:123")

    async def run() -> None:
        image = Comp.Image.fromFileSystem(str(tmp_path / "img.png"))
        await event.send(event.chain_result([image]))
        await event.send(event.plain_result("提示词优化降级"))

    asyncio.run(run())
    assert event.delivered_images == [str(tmp_path / "img.png")]
    assert service.consume_deliveries("group:123") == [str(tmp_path / "img.png")]
    assert service.consume_deliveries("group:123") == []
    # 其他会话的队列互不串扰。
    assert service.consume_deliveries("private:456") == []


class _FakeBridge:
    def __init__(self, image_path: str) -> None:
        self._image_path = image_path
        self.calls: list[dict[str, Any]] = []

    async def generate(self, event: Any, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        await event.send(event.chain_result([Comp.Image.fromFileSystem(self._image_path)]))
        await event.send(event.plain_result("已生成"))
        return "生成完成：img.png"


def test_tool_generate_enqueues_image(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    image_path = str(tmp_path / "outputs" / "img.png")
    service._services = SimpleNamespace()
    bridge = _FakeBridge(image_path)
    service._llm_bridge = bridge

    result = asyncio.run(service.tool_generate("group:123", "画一只螃蟹"))

    assert result["ok"] is True
    assert result["image_count"] == 1
    assert result["notices"] == ["已生成"]
    assert bridge.calls[0]["prompt"] == "画一只螃蟹"
    assert service.consume_deliveries("group:123") == [image_path]


def test_tool_generate_swallows_bridge_error(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    service._services = SimpleNamespace()

    class _Boom:
        async def generate(self, event: Any, prompt: str, **kwargs: Any) -> str:
            raise RuntimeError("comfyui down")

    service._llm_bridge = _Boom()
    result = asyncio.run(service.tool_generate("group:123", "画一只螃蟹"))
    assert result["ok"] is False
    assert "comfyui down" in result["message"]
    assert service.consume_deliveries("group:123") == []


# ── persona 工具执行入口 ─────────────────────────────────────────────


async def _run_anima_tool(
    config: Any, name: str, arguments: dict[str, Any]
) -> str:
    return await EngineToolsMixin._execute_anima_tool(
        SimpleNamespace(), "group:123", name, arguments, config
    )


def test_persona_tool_disabled_message(tmp_path: Path) -> None:
    result = asyncio.run(
        _run_anima_tool(_config(tmp_path), "anima_draw", {"prompt": "画一只螃蟹"})
    )
    assert "未启用" in result


def test_persona_tool_draw_dispatches_to_service(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, anima_enabled=True)
    monkeypatch.setattr("atri_qq_bot.anima._DATA_DIR", tmp_path / "anima")
    monkeypatch.setattr(
        "atri_qq_bot.anima._RUNTIME_CONFIG", tmp_path / "anima" / "config.json"
    )
    monkeypatch.setattr(
        "atri_qq_bot.anima._ASTRBOT_SOURCE_CONFIG", tmp_path / "missing.json"
    )
    reset_anima_service()
    service = get_anima_service(config)
    assert service is not None
    image_path = str(tmp_path / "outputs" / "img.png")
    service._services = SimpleNamespace()
    service._llm_bridge = _FakeBridge(image_path)

    result = asyncio.run(
        _run_anima_tool(config, "anima_draw", {"prompt": "画一只螃蟹"})
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert service.consume_deliveries("group:123") == [image_path]


def test_persona_tool_rejects_empty_prompt(tmp_path: Path, monkeypatch) -> None:
    _service(tmp_path, monkeypatch)
    config = _config(tmp_path, anima_enabled=True)
    result = asyncio.run(_run_anima_tool(config, "anima_draw", {"prompt": "  "}))
    assert "prompt 为空" in result


def test_persona_tool_unknown_name(tmp_path: Path, monkeypatch) -> None:
    _service(tmp_path, monkeypatch)
    config = _config(tmp_path, anima_enabled=True)
    result = asyncio.run(_run_anima_tool(config, "anima_bogus", {}))
    assert "未知绘画工具" in result


# ── OneBot 层图片投递 ───────────────────────────────────────────────


def test_deliver_pending_anima_sends_images(tmp_path: Path, monkeypatch) -> None:
    from atri_qq_bot.onebot.server import OneBotServer

    monkeypatch.setattr(
        "atri_qq_bot.onebot.server_parts.server_anima._ANIMA_IMAGE_SEND_GAP_SECONDS",
        0.0,
    )
    service = _service(tmp_path, monkeypatch)
    image_path = str(tmp_path / "outputs" / "img.png")
    event = service._make_event("group:123")

    async def enqueue() -> None:
        await event.send(event.chain_result([Comp.Image.fromFileSystem(image_path)]))

    asyncio.run(enqueue())

    server = object.__new__(OneBotServer)
    server.config = _config(tmp_path, anima_enabled=True)
    server._anima_service = service
    sent: list[Any] = []

    async def fake_send_reply(websocket: Any, event: Any, messages: Any, **kwargs: Any) -> str:
        sent.extend(messages)
        return "ok"

    server.send_reply = fake_send_reply  # type: ignore[method-assign]
    server._delivery_model_name = lambda model_type: "chat-model"  # type: ignore[method-assign]

    asyncio.run(
        server._deliver_pending_anima(None, {"group_id": "123"}, "group:123")
    )
    assert [message.kind for message in sent] == ["image"]
    assert sent[0].content == image_path
    # 消费后队列清空，重复调用不再发送。
    asyncio.run(
        server._deliver_pending_anima(None, {"group_id": "123"}, "group:123")
    )
    assert len(sent) == 1


def test_deliver_pending_anima_noop_without_service(
    tmp_path: Path, monkeypatch
) -> None:
    from atri_qq_bot.onebot.server import OneBotServer

    monkeypatch.setattr(
        "atri_qq_bot.onebot.server_parts.server_anima._ANIMA_IMAGE_SEND_GAP_SECONDS",
        0.0,
    )
    server = object.__new__(OneBotServer)
    server.config = _config(tmp_path, anima_enabled=False)
    sent: list[Any] = []

    async def fake_send_reply(websocket: Any, event: Any, messages: Any, **kwargs: Any) -> str:
        sent.extend(messages)
        return "ok"

    server.send_reply = fake_send_reply  # type: ignore[method-assign]

    asyncio.run(
        server._deliver_pending_anima(None, {"group_id": "123"}, "group:123")
    )
    assert sent == []
