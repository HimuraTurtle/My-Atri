"""热梗学习：用聊天模型把原始热搜词精选成带释义与用法的梗库条目。

模型只负责"编辑"：从当天热搜里挑出真正在流行、适合聊天的梗，并给出
口语化用法；输出必须是 JSON 数组，解析失败时整轮放弃，不会污染库。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from ..prompting import load_prompt
from .fetcher import fetch_raw_trends
from .observe import collect_group_chat_words
from .store import save_meme_store, touch_known_terms


def _match_sources(term_key: str, source_by_term: dict[str, set[str]]) -> list[str]:
    """模型可能改写词条，先精确匹配再子串匹配；都失败标"模型补充"。"""

    sources = set(source_by_term.get(term_key, set()))
    if not sources:
        for word, word_sources in source_by_term.items():
            if term_key and word and (term_key in word or word in term_key):
                sources.update(word_sources)
    return sorted(sources) or ["模型补充"]


LOGGER = logging.getLogger("atri.memes.learning")

_MAX_LLM_TOKENS = 1200
_TIMEOUT_SECONDS = 30.0


async def refresh_meme_library(config: Any) -> str:
    """完整刷新流程：抓取热搜与群聊热词 -> 模型精选 -> 合并落盘。"""

    raw_trends = await fetch_raw_trends()
    memory_path = Path(str(getattr(config, "memory_path", "") or ""))
    group_words = collect_group_chat_words(memory_path if memory_path.is_file() else None)
    for item in group_words:
        raw_trends.append(
            {
                "source": "群聊",
                "word": str(item.get("word") or ""),
                "sample": str(item.get("sample") or ""),
            }
        )
    if not raw_trends:
        return "本轮没有任何热搜来源可用，跳过"
    # 群友还在用的已入库词先续期，保证它们始终留在注入窗口前排。
    touched = touch_known_terms([str(item.get("word") or "") for item in group_words])
    curated = await curate_memes_with_llm(config, raw_trends)
    source_by_term: dict[str, set[str]] = {}
    for item in raw_trends:
        word = str(item.get("word") or "").strip().casefold()
        source = str(item.get("source") or "").strip()
        if word and source:
            source_by_term.setdefault(word, set()).add(source)
    for entry in curated:
        entry_key = str(entry.get("term") or "").strip().casefold()
        entry["sources"] = _match_sources(entry_key, source_by_term)
    if curated:
        save_meme_store(curated)
        return (
            f"抓到 {len(raw_trends) - len(group_words)} 条热搜词和 "
            f"{len(group_words)} 个群聊高频词，精选 {len(curated)} 条入库"
        )
    save_meme_store([])
    return f"抓到 {len(raw_trends)} 条热词，但模型没有选出可用的梗（已保留旧库）"


async def curate_memes_with_llm(
    config: Any,
    raw_trends: list[dict[str, str]],
) -> list[dict[str, Any]]:
    api_key = getattr(config, "openai_api_key", "") or ""
    if not api_key:
        LOGGER.warning("未配置聊天 API key，跳过热梗精选")
        return []
    base_url = str(getattr(config, "openai_base_url", "") or "").rstrip("/")
    endpoint = (
        base_url + "/chat/completions"
        if base_url.endswith("/v1")
        else base_url + "/v1/chat/completions"
    )
    now = datetime.now(timezone(timedelta(hours=8)))
    raw_lines = []
    for index, item in enumerate(raw_trends[:70], start=1):
        line = f"{index}. [{item.get('source')}] {item.get('word')}"
        sample = str(item.get("sample") or "").strip()
        if sample:
            line += f"（出现于：{sample}）"
        raw_lines.append(line)
    user_content = (
        f"今天是 {now:%Y年%m月%d日 %A}。以下是刚抓取的实时热搜词与群聊高频表达：\n"
        + "\n".join(raw_lines)
    )
    payload = {
        "model": getattr(config, "openai_model", "") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": load_prompt("meme_curation")},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.4,
        "max_tokens": _MAX_LLM_TOKENS,
        "stream": False,
    }
    model_name = str(payload["model"]).casefold()
    if "api.deepseek.com" in base_url.casefold() and model_name in {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }:
        # v4 系列默认先走思考链；不关掉的话 max_tokens 会被推理耗尽，
        # 正文 content 为空（与记忆提取模块遇到的问题相同）。
        payload["thinking"] = {"type": "disabled"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        LOGGER.warning("热梗精选模型请求失败：%s", exc)
        return []
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return _parse_entries(content)


def _parse_entries(content: str) -> list[dict[str, Any]]:
    """从模型输出中提取 JSON 数组；容忍 markdown 代码块包裹。"""

    text = str(content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    candidate = fenced.group(1) if fenced else _outermost_json_array(text)
    if not candidate:
        LOGGER.warning("热梗精选输出里没有 JSON 数组：%s", text[:200])
        return []
    try:
        payload = json.loads(candidate)
    except ValueError as exc:
        LOGGER.warning("热梗精选 JSON 解析失败：%s", exc)
        return []
    entries: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term or len(term) > 24:
            continue
        entries.append(
            {
                "term": term,
                "meaning": str(item.get("meaning") or "").strip()[:120],
                "usage": str(item.get("usage") or "").strip()[:120],
                "kind": "meme" if str(item.get("kind") or "").strip() == "meme" else "topic",
            }
        )
    return entries


def _outermost_json_array(text: str) -> str | None:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]
