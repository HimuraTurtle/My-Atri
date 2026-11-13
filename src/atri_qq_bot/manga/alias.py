"""漫画标题译名扩展：让聊天模型把搜索目标加工成多译名再查询。

实测（2026-11-12）「学生会也有洞」在 GoDa 源上的条目名是「脑洞学生会」
——同一部作品的另一译名。单一标题查询 + 严格互验会把正确结果拒掉。
这里先用聊天模型列出标题可能的其它译名/原名/别名（带缓存），查询时
逐个候选尝试、互验对整个译名集合判定；模型失败只降级为单标题查询，
不影响搜索主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.manga")

_LLM_TIMEOUT_SECONDS = 30.0
_LLM_ATTEMPTS = 2
# 推理模型（deepseek-flash 等）会先输出思维链再输出正文，token 给足
# 才不至于 finish=length 时 content 仍为空（实测 300 会全被思考吃掉）。
_LLM_MAX_TOKENS = 2000
_MAX_ALIASES = 4
_ALIAS_MIN_LEN = 2
_ALIAS_MAX_LEN = 40
_CACHE_TTL_SECONDS = 3600.0
_CACHE_MAX_ENTRIES = 64

_alias_cache: dict[str, tuple[float, list[str]]] = {}

_SYSTEM_PROMPT = (
    "你是漫画检索助手。用户给你一部漫画的标题，请列出这部作品在中文漫画"
    "网站上可能出现的其它标题写法：常见不同译名、日文原名、英文名、简称。"
    "只列你确信是同一部作品的，不确定的不要编。"
    "输出 JSON 字符串数组，最多 4 个，没有就输出 []。"
    '示例：输入「学生会也有洞」→ ["脑洞学生会", "生徒会にも穴はある"]'
)


def _strip_fences(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def _normalize_alias(text: str) -> str:
    return "".join(
        ch
        for ch in str(text or "").casefold()
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


def _sanitize_aliases(raw: Any, original: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen = {_normalize_alias(original)}
    aliases: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not (_ALIAS_MIN_LEN <= len(item) <= _ALIAS_MAX_LEN):
            continue
        key = _normalize_alias(item)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(item)
        if len(aliases) >= _MAX_ALIASES:
            break
    return aliases


async def call_chat_model(
    config: Any, system: str, user: str, temperature: float = 0.2
) -> str:
    """漫画模块共用的轻量 LLM 文本调用（复用 isittrue 的模式）。

    推理模型（deepseek-flash 等）会先输出思维链再输出正文，token 给足
    才不至于 finish=length 时 content 仍为空（实测 300 会全被思考吃掉）。
    """
    model = str(getattr(config, "openai_model", "") or "").strip()
    base_url = str(getattr(config, "openai_base_url", "") or "").rstrip("/")
    api_key = str(getattr(config, "openai_api_key", "") or "")
    if not model or not base_url:
        raise RuntimeError("LLM endpoint not configured")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": _LLM_MAX_TOKENS,
        "temperature": temperature,
    }
    last_exc: Exception | None = None
    for attempt in range(_LLM_ATTEMPTS):
        if attempt:
            await asyncio.sleep(1.0)
        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{base_url}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
                body = response.json()
            choices = body.get("choices") or []
            content = str((choices[0].get("message") or {}).get("content") or "").strip()
            if content:
                return content
            last_exc = RuntimeError("LLM 返回空 content")
        except Exception as exc:
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("LLM 调用失败")


async def _call_llm(config: Any, title: str) -> str:
    return await call_chat_model(config, _SYSTEM_PROMPT, title)


def expand_title_aliases_sync_parse(raw_text: str, original: str) -> list[str]:
    """解析模型输出为合法别名列表（独立出来便于单测）。"""
    text = _strip_fences(raw_text)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        return _sanitize_aliases(json.loads(text[start : end + 1]), original)
    except json.JSONDecodeError:
        return []


async def expand_title_aliases(config: Any, title: str) -> list[str]:
    """让聊天模型列出标题的其它译名/别名；失败或无结果返回 []。"""
    title = str(title or "").strip()
    if len(title) < _ALIAS_MIN_LEN:
        return []

    now = time.monotonic()
    cached = _alias_cache.get(title)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        raw = await _call_llm(config, title)
        aliases = expand_title_aliases_sync_parse(raw, title)
    except Exception as exc:
        LOGGER.warning("译名扩展失败（按单标题继续）：%s -> %s", title, exc)
        aliases = []

    if len(_alias_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_alias_cache, key=lambda k: _alias_cache[k][0])
        _alias_cache.pop(oldest, None)
    _alias_cache[title] = (now, aliases)
    if aliases:
        LOGGER.info("译名扩展：%s -> %r", title, aliases)
    return aliases
