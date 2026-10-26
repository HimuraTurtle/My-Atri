"""server.py 与各 handler mixin 共享的常量与工具。

本模块不得反向导入 atri_webui.server（会形成循环）；handler 模块在此显式
导入所需名字，取代旧的 _sync_webui_globals 命名空间注入。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.runtime import PROJECT_ROOT

from ..page import index as _page_module

MAX_UPLOAD_BYTES = 8_000_000
MAX_MUSIC_STEM_UPLOAD_BYTES = 150_000_000
MAX_COMPLETE_MUSIC_UPLOAD_BYTES = 180_000_000
MAX_MUSIC_SOURCE_UPLOAD_BYTES = 100_000_000
AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}
MUSIC_SERVICE_URL = os.getenv(
    "ATRI_MUSIC_SERVICE_URL",
    "http://127.0.0.1:8793",
).rstrip("/")
MUSIC_SOURCE_ROOT = Path(
    os.getenv("ATRI_MUSIC_SOURCE_DIR", str(Path.home() / "Music" / "QQmusic-MP3"))
).expanduser().resolve()
MUSIC_IMPORT_ROOT = Path(
    os.getenv(
        "ATRI_MUSIC_IMPORT_DIR",
        str(PROJECT_ROOT / "data" / "music_imports"),
    )
).expanduser().resolve()
SINGING_REFERENCE_ROOT = Path(
    os.getenv(
        "ATRI_ORIGINAL_VOICE_LIBRARY",
        str(Path.home() / "Music" / "ATRI训练音频素材"),
    )
).expanduser().resolve()
SINGING_PATH_MARKERS = ("歌唱", "唱歌", "singing", "song", "vocal")
LOGGER = logging.getLogger("atri.webui")

_PAGE_MODULE_PATH = Path(__file__).resolve().parents[1] / "page" / "index.py"
_PAGE_RELOAD_LOCK = threading.Lock()
_PAGE_MODULE_MTIME_NS = 0


def _render_index_dynamic() -> str:
    """Render the WebUI with a lightweight source-mtime reload.

    The desktop WebUI is local-only and frequently edited during development.
    Reloading this presentation module on request means a browser refresh is
    enough to pick up HTML/CSS/JS changes; API/service changes still require a
    normal process restart.
    """
    global _PAGE_MODULE_MTIME_NS
    try:
        mtime_ns = _PAGE_MODULE_PATH.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    if mtime_ns and mtime_ns != _PAGE_MODULE_MTIME_NS:
        with _PAGE_RELOAD_LOCK:
            if mtime_ns != _PAGE_MODULE_MTIME_NS:
                importlib.invalidate_caches()
                importlib.reload(_page_module)
                _PAGE_MODULE_MTIME_NS = mtime_ns
    return _page_module.render_index()


def _music_bridge_request(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        f"{MUSIC_SERVICE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:
            detail = ""
        raise RuntimeError(detail or f"AI_music 返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"AI_music 本地桥接不可达：{exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("AI_music 返回了无效响应")
    if result.get("ok") is False:
        raise RuntimeError(str(result.get("error") or "AI_music 请求失败"))
    return result


def singing_reference_payload() -> list[dict[str, str]]:
    """扫描本机歌声参考目录，为歌曲工程提供参考音源列表。"""

    references: list[dict[str, str]] = []
    if not SINGING_REFERENCE_ROOT.is_dir():
        return references
    for path in sorted(SINGING_REFERENCE_ROOT.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            relative = path.resolve().relative_to(SINGING_REFERENCE_ROOT)
        except (OSError, ValueError):
            continue
        if not any(
            marker in part.casefold()
            for part in relative.parts
            for marker in SINGING_PATH_MARKERS
        ):
            continue
        resolved = path.resolve()
        references.append(
            {
                "name": path.stem,
                "path": str(resolved),
                "relative_path": str(relative),
            }
        )
    return references


def _valid_singing_job_id(value: str) -> str:
    job_id = str(value or "").strip().lower()
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("歌唱任务 ID 无效")
    return job_id


def _valid_music_artifact_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if name not in {"vocal", "harmony", "instrumental", "converted", "mix"}:
        raise ValueError("歌曲试听音轨无效")
    return name


def _valid_music_stage(value: str) -> str:
    stage = str(value or "").strip().lower()
    if stage not in {"separation", "inference", "mix"}:
        raise ValueError("歌曲处理阶段无效")
    return stage


def run_coro(coro: Any) -> Any:
    future = asyncio.run_coroutine_threadsafe(coro, _main_loop())
    return future.result(timeout=90)


_LOOP: asyncio.AbstractEventLoop | None = None


def _main_loop() -> asyncio.AbstractEventLoop:
    if _LOOP is None:
        raise RuntimeError("webui loop is not initialized")
    return _LOOP


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _LOOP
    _LOOP = loop


async def test_chat(config: BotConfig, text: str) -> dict[str, Any]:
    engine = AtriReplyEngine(
        replace(config, idle_proactive_enabled=False, group_proactive_enabled=False)
    )
    try:
        reply = await engine._reply_with_guarded_api("webui:test", text, "")
        reply = engine._finalize_reply(
            "webui:test",
            text,
            reply,
            strict_quality=not bool(config.human_reply_pipeline_enabled),
        )
        if not reply:
            raise RuntimeError("模型返回了空回复")
        return {"reply": reply, "used_ai": True, "error": ""}
    except Exception as exc:
        return {
            "reply": "",
            "used_ai": False,
            "error": str(exc),
        }
