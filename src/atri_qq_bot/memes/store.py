"""热梗库：磁盘 JSON 存储、合并去重、过期淘汰与聊天提示词渲染。

数据文件 ``data/memes/recent.json`` 是唯一事实源；聊天链路只读，刷新循环
负责写入。渲染上下文时按 ``refreshed_at`` 取最新若干条，控制注入 token。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from ..runtime.paths import PROJECT_ROOT


LOGGER = logging.getLogger("atri.memes.store")

MEME_STORE_PATH = PROJECT_ROOT / "data" / "memes" / "recent.json"

_MAX_ENTRIES = 80
_STALE_AFTER_SECONDS = 10 * 24 * 3600
_LOCK = threading.Lock()


def _normalise_term(term: str) -> str:
    return re.sub(r"\s+", "", str(term or "")).casefold()


def load_meme_store() -> dict:
    """读取梗库快照；文件缺失或损坏时返回空库。"""

    try:
        payload = json.loads(MEME_STORE_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"updated_at": 0.0, "entries": []}
    except (OSError, ValueError) as exc:
        LOGGER.warning("热梗库读取失败，按空库处理：%s", exc)
        return {"updated_at": 0.0, "entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {"updated_at": 0.0, "entries": []}
    return {"updated_at": float(payload.get("updated_at") or 0.0), "entries": entries}


def save_meme_store(entries: list[dict], now: float | None = None) -> None:
    """合并写入并落盘；调用方传入的是本轮 LLM 精选出的新条目。"""

    moment = time.time() if now is None else now
    with _LOCK:
        merged = {
            _normalise_term(str(entry.get("term"))): entry
            for entry in _merged_entries(entries, moment)
        }
        ordered = sorted(
            merged.values(), key=lambda entry: entry.get("refreshed_at") or 0.0, reverse=True
        )
        payload = {"updated_at": moment, "entries": ordered[:_MAX_ENTRIES]}
        MEME_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = MEME_STORE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(MEME_STORE_PATH)
    LOGGER.info("热梗库已更新：%d 条", len(ordered[:_MAX_ENTRIES]))


def _merged_entries(fresh_entries: list[dict], moment: float) -> list[dict]:
    existing = {
        str(entry.get("term") or "").strip(): entry for entry in load_meme_store().get("entries", [])
    }
    merged: dict[str, dict] = {}
    for entry in existing.values():
        age = moment - (entry.get("refreshed_at") or 0.0)
        if age <= _STALE_AFTER_SECONDS:
            merged[_normalise_term(str(entry.get("term")))] = entry
    for raw in fresh_entries:
        term = str(raw.get("term") or "").strip()
        if not term:
            continue
        key = _normalise_term(term)
        previous = merged.get(key)
        entry = {
            "term": term,
            "meaning": str(raw.get("meaning") or "").strip(),
            "usage": str(raw.get("usage") or "").strip(),
            "kind": "meme" if str(raw.get("kind")) == "meme" else "topic",
            "sources": list(raw.get("sources") or [])[:4],
            "added_at": (previous or {}).get("added_at") or moment,
            "refreshed_at": moment,
        }
        merged[key] = entry
    return list(merged.values())


def touch_known_terms(terms: list[str], now: float | None = None) -> int:
    """群友还在用的已入库词刷新活跃时间，避免被热搜新词挤出注入窗口。"""

    moment = time.time() if now is None else now
    keys = {_normalise_term(term) for term in terms if str(term or "").strip()}
    if not keys:
        return 0
    with _LOCK:
        payload = load_meme_store()
        entries = payload.get("entries", [])
        touched = 0
        for entry in entries:
            if _normalise_term(str(entry.get("term") or "")) in keys:
                entry["refreshed_at"] = moment
                touched += 1
        if touched:
            payload["updated_at"] = moment
            _write_store(payload)
        return touched


def _write_store(payload: dict) -> None:
    MEME_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MEME_STORE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(MEME_STORE_PATH)


def meme_context_prompt(max_items: int = 12) -> str:
    """渲染注入聊天 system prompt 的热梗上下文；空库返回空串。

    本群群友在用的词（来源含"群聊"）优先于通用热搜：群聊语境里它们
    才是最该被想起的表达，热搜词只是背景谈资。
    """

    if max_items <= 0:
        return ""
    entries = [
        entry
        for entry in load_meme_store().get("entries", [])
        if isinstance(entry, dict) and str(entry.get("term") or "").strip()
    ]
    if not entries:
        return ""

    def _rank(entry: dict) -> tuple[int, float]:
        sources = [str(source) for source in entry.get("sources") or []]
        is_group_word = any("群聊" in source for source in sources)
        return (0 if is_group_word else 1, -(entry.get("refreshed_at") or 0.0))

    entries.sort(key=_rank)
    lines: list[str] = []
    for entry in entries[:max_items]:
        term = str(entry.get("term")).strip()
        meaning = str(entry.get("meaning") or "").strip()
        usage = str(entry.get("usage") or "").strip()
        part = f"「{term}」"
        if meaning:
            part += f"：{meaning}"
        if usage:
            part += f"；例：{usage}"
        lines.append(f"- {part}")
    updated_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(load_meme_store().get("updated_at") or 0.0))
    return (
        "以下是刚刚从网上热搜和群聊里学到的、当前真实流行的网络梗和热点"
        f"（库更新于 {updated_at}）：\n"
        + "\n".join(lines)
        + "\n使用规则：只在语境合适时自然用一两个，像你本来就懂一样；"
        "不要堆梗，不要解释梗的出处，不要每条消息都带梗；"
        "绝绝子、栓Q、yyds、尊嘟假嘟这类已经过气的老梗不要用。"
        "这个清单不是全部：遇到清单之外、你又不认识的新梗或流行语，"
        "先用 search_web 查明白含义再自然接话，不要装懂。"
    )
