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

from ..model_profiles import load_model_profiles
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
        reply = await engine._reply_with_guarded_api("webui:test", text, "主人")
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


def _qzone_generation_config(config: BotConfig, profile_id: str) -> tuple[BotConfig, str]:
    wanted = str(profile_id or "").strip()
    if not wanted:
        return config, "当前聊天模型"
    profile = next((item for item in load_model_profiles() if str(item.get("id") or "") == wanted), None)
    if not profile:
        raise ValueError("没有找到所选模型档案")
    if str(profile.get("model_type") or "chat") != "chat":
        raise ValueError("QQ 动态生成只能选择聊天模型")
    base_url = str(profile.get("base_url") or "").strip().rstrip("/")
    model = str(profile.get("model") or "").strip()
    if not base_url or not model:
        raise ValueError("所选模型档案缺少接口地址或模型名")
    try:
        tuned = replace(
            config,
            openai_api_key=str(profile.get("api_key") or config.openai_api_key or ""),
            openai_base_url=base_url,
            openai_model=model,
            temperature=float(profile.get("temperature") or config.temperature),
            frequency_penalty=float(profile.get("frequency_penalty") or config.frequency_penalty),
            max_tokens=max(120, int(profile.get("max_tokens") or config.max_tokens)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"模型档案参数无效：{exc}") from exc
    return tuned, str(profile.get("name") or model)


async def generate_qzone_text(config: BotConfig, prompt: str, kind: str, profile_id: str = "") -> dict[str, Any]:
    instruction = str(prompt or "").strip()
    if not instruction:
        raise ValueError("请输入生成提示词")
    if len(instruction) > 2000:
        raise ValueError("提示词最多 2000 字")
    mode = "动态" if kind == "post" else "评论"
    if kind not in {"post", "comment"}:
        raise ValueError("生成类型无效")
    selected_config, profile_name = _qzone_generation_config(config, profile_id)
    engine = AtriReplyEngine(
        replace(
            selected_config,
            idle_proactive_enabled=False,
            group_proactive_enabled=False,
            chat_quality_retries=0,
        )
    )
    system = (
        "你现在在为 QQ 空间自动互动生成文案。你是亚托莉，必须保持当前项目已配置的人格、记忆和情绪状态。"
        f"请根据用户提示生成一条{mode}，只输出可以直接发布的{mode}正文，不要解释、不要加引号、不要写‘提示词’或‘生成结果’。"
        + ("动态要结合当前时间和提示中的主题方向，避免模板化，可在生活趣事、思考感悟、原作剧情、热点观点、天气季节、学习发现之间自然变化。" if kind == "post" else "")
        + ("动态必须优先采用提示中明确提供的一条事实或最新消息，并保留必要的来源/据报道表述；只能依据原文，不得补写新闻细节。没有可靠事实时不要编造新闻、日期、人物行动或亲身经历，也不要用‘今天看到/窗边发现’等虚构亲历替代事实。" if kind == "post" else "")
        + ("正文控制在 20-120 字，适合 QQ 空间动态。" if kind == "post" else "正文控制在 1-80 字，自然、像真人评论。")
    )
    reply = await engine._reply_with_guarded_api(
        "webui:qzone:generate",
        instruction,
        "主人",
        extra_system=system,
    )
    reply = engine._finalize_reply("webui:qzone:generate", instruction, reply, strict_quality=False).strip()
    limit = 1000 if kind == "post" else 200
    return {"ok": True, "kind": kind, "content": reply[:limit], "model_name": profile_name, "model_profile_id": str(profile_id or "")}
