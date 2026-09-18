"""群聊长时间静默后的断线话题回填。

机器人断线重启、或整夜无人说话后，会话的 80 条滚动窗口会被新消息填满，
断线前的话题从此退出注入范围。这里在"该群第一条新消息"到达时检测静默
缺口，把缺口前一段时间（归档库）的话题压缩成 L2 事件记忆，让模型仍然
接得上之前的讨论。归档查询失败或为空时静默跳过，绝不影响消息主链路。
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GAP_BACKFILL_KEY_PREFIX = "gap_backfill:"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH_CACHE: Path | None = None

_BOT_OUTPUT_HINTS = ("ref_id:", "tip:", "登录成功", "使用 /", "更新时间:", "数据更新")
_AT_PREFIX = re.compile(r"^\s*@\S+\s*")
_URL = re.compile(r"https?://\S+")
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")
_STICKER = re.compile(r"\[表情包/图片[^\]]*\]")
_QQ_FACE = re.compile(r"\[QQ表情[^\]]*\]")
_MEDIA_PLACEHOLDER = re.compile(r"\[(?:分享|用户|flash|戳一戳)[^\]]*\]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_SAME_CHAR = re.compile(r"(.)\1+")


def resolve_insights_db_path() -> Path:
    """与 insights/config.py 相同的解析规则，但绝不创建文件。"""
    global _DB_PATH_CACHE
    if _DB_PATH_CACHE is None:
        raw = str(os.getenv("INSIGHTS_DB_PATH", "") or "").strip()
        path = Path(raw).expanduser() if raw else _PROJECT_ROOT / "data" / "insights" / "insights.sqlite3"
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        _DB_PATH_CACHE = path
    return _DB_PATH_CACHE


def detect_gap_last_at(
    item: dict[str, Any], now: float, threshold_seconds: float
) -> float | None:
    """返回会话最后活动时间；距今不足阈值时返回 None。"""
    last_at = 0.0
    for entry in item.get("history") or []:
        if not isinstance(entry, dict):
            continue
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if at > last_at:
            last_at = at
    if last_at <= 0 or now - last_at < threshold_seconds:
        return None
    return last_at


def load_archive_window(
    db_path: Path | str | None,
    group_id: int | str,
    start_at: float,
    end_at: float,
) -> list[dict[str, Any]]:
    """读取归档库 [start_at, end_at] 内该群的文本消息；库不存在时返回空。"""
    if not db_path or not Path(db_path).exists():
        return []
    start_iso = datetime.fromtimestamp(start_at, timezone.utc).isoformat(timespec="seconds")
    end_iso = datetime.fromtimestamp(end_at, timezone.utc).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(db_path), timeout=8.0)
    try:
        rows = connection.execute(
            """SELECT user_id, nickname, content, created_at FROM group_messages
            WHERE group_id = ? AND created_at >= ? AND created_at <= ? AND message_type = 'text'
            ORDER BY created_at""",
            (str(group_id), start_iso, end_iso),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "user_id": str(row[0]),
            "nickname": str(row[1] or ""),
            "content": str(row[2] or ""),
            "created_at": str(row[3] or ""),
        }
        for row in rows
    ]


def clean_message_text(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = _CQ_CODE.sub("", text)
    text = _STICKER.sub("", text)
    text = _QQ_FACE.sub("", text)
    text = _MEDIA_PLACEHOLDER.sub("", text)
    text = _AT_PREFIX.sub("", text)
    text = _URL.sub("", text)
    return " ".join(text.split()).strip()


def is_backfill_useful(text: str) -> bool:
    if not 6 <= len(text) <= 120:
        return False
    if len(_CJK.findall(text)) < 3:
        return False
    if any(hint in text for hint in _BOT_OUTPUT_HINTS):
        return False
    if _SAME_CHAR.fullmatch(text):
        return False
    if text.startswith(("/", "#")):
        return False
    return True


def _pick_representative(messages: list[str], count: int = 2) -> list[str]:
    scored = sorted(
        messages,
        key=lambda message: -(len(_CJK.findall(message)) + (3 if 10 <= len(message) <= 60 else 0)),
    )
    out: list[str] = []
    for message in scored:
        if message not in out:
            out.append(message)
        if len(out) >= count:
            break
    return out


def _local_time_phrase(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    return f"{moment.month}月{moment.day}日{moment.hour}点"


def _window_time_phrase(rows: list[dict[str, Any]]) -> str:
    start = _local_time_phrase(rows[0].get("created_at") or "")
    end = _local_time_phrase(rows[-1].get("created_at") or "")
    if start and end and start != end:
        return f"{start}到{end}"
    return start or end


def _l2_event_entry(value: str, key: str, now: float) -> dict[str, Any]:
    return {
        "layer": "L2",
        "category": "event",
        "key": key,
        "value": value,
        "activity": 1.0,
        "confidence": 0.7,
        "source": "user",
        "created_at": now,
        "updated_at": now,
        "last_mentioned_at": now,
        "state": "active",
        "associations": [],
    }


def build_gap_summaries(
    group_id: int | str,
    rows: list[dict[str, Any]],
    last_at: float,
    now: float,
) -> list[tuple[str, dict[str, Any]]]:
    """把归档窗口压缩成 (conversation_id, L2 entry) 列表。"""
    if not rows:
        return []
    by_user: dict[str, dict[str, Any]] = {}
    for row in rows:
        text = clean_message_text(row.get("content"))
        if not is_backfill_useful(text):
            continue
        entry = by_user.setdefault(row["user_id"], {"nickname": "", "messages": []})
        if not entry["nickname"] and row.get("nickname"):
            entry["nickname"] = str(row["nickname"]).strip()
        entry["messages"].append(text)
    if not by_user:
        return []

    key = f"{GAP_BACKFILL_KEY_PREFIX}{int(last_at)}"
    phrase = _window_time_phrase(rows)
    summaries: list[tuple[str, dict[str, Any]]] = []
    group_parts: list[str] = []
    for user_id, entry in by_user.items():
        representatives = _pick_representative(entry["messages"], 2)
        if not representatives:
            continue
        nickname = entry["nickname"] or f"群友{user_id[-4:]}"
        value = f"{nickname}在{phrase}聊过：「{representatives[0]}」"
        if len(representatives) > 1:
            value += f"，还聊过「{representatives[1]}」"
        summaries.append(
            (f"group:{group_id}:user:{user_id}", _l2_event_entry(value[:120], key, now))
        )
        group_parts.append(f"{nickname}：「{representatives[0]}」")
    if group_parts:
        value = f"{phrase}群里在聊：" + "；".join(group_parts[:5])
        summaries.append((f"group:{group_id}", _l2_event_entry(value[:200], key, now)))
    return summaries
