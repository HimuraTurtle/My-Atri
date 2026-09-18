"""漫画订阅与运行状态的 JSON 持久化存储。

AstrBot 原插件依赖宿主的 KV 存储；这里改为独立 JSON 文件，结构上保留
subscriptions / push_defaults / chapter_timestamps / last_check 四段，
原子写入（临时文件 + os.replace）避免进程被杀时损坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class MangaStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.is_file():
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {
            "subscriptions": {},
            "push_defaults": {},
            "chapter_timestamps": {},
            "last_check": 0,
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".manga_store_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_name, str(self.path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ── 章节缓存时间戳 ─────────────────────────────────────────────

    def get_chapter_timestamp(self, manga_id: int) -> float:
        with self._lock:
            return float(self._data["chapter_timestamps"].get(str(manga_id), 0))

    def set_chapter_timestamp(self, manga_id: int, now: float | None = None) -> None:
        with self._lock:
            self._data["chapter_timestamps"][str(manga_id)] = now or time.time()
            self._save()

    @property
    def last_check(self) -> float:
        with self._lock:
            return float(self._data.get("last_check", 0))

    def set_last_check(self, now: float | None = None) -> None:
        with self._lock:
            self._data["last_check"] = now or time.time()
            self._save()

    # ── 订阅 ───────────────────────────────────────────────────────

    def subscribe(self, manga_id: int, title: str, source_id: int, conversation_id: str) -> None:
        with self._lock:
            subs = self._data["subscriptions"]
            key = str(manga_id)
            if key not in subs:
                subs[key] = {
                    "title": title,
                    "source_id": source_id,
                    "latest_chapter_id": 0,
                    "subscribers": {},
                }
            if conversation_id not in subs[key]["subscribers"]:
                subs[key]["subscribers"][conversation_id] = {
                    "push_enabled": bool(
                        self._data["push_defaults"].get(conversation_id, False)
                    )
                }
            self._save()

    def unsubscribe(self, manga_id: int, conversation_id: str) -> None:
        with self._lock:
            subs = self._data["subscriptions"]
            key = str(manga_id)
            if key in subs:
                subs[key]["subscribers"].pop(conversation_id, None)
                if not subs[key]["subscribers"]:
                    del subs[key]
                self._save()

    def get_subscriptions(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for manga_id, info in self._data["subscriptions"].items():
                if conversation_id in info.get("subscribers", {}):
                    result.append(
                        {
                            "manga_id": int(manga_id),
                            "title": info["title"],
                            "source_id": info.get("source_id", 0),
                            "latest_chapter_id": info.get("latest_chapter_id", 0),
                            "push_enabled": (
                                info.get("subscribers", {})
                                .get(conversation_id, {})
                                .get("push_enabled", False)
                            ),
                        }
                    )
            result.sort(key=lambda s: s["title"])
            return result

    def get_all_subscriptions(self) -> dict[str, Any]:
        with self._lock:
            return {
                key: {
                    "title": info.get("title", ""),
                    "source_id": info.get("source_id", 0),
                    "latest_chapter_id": info.get("latest_chapter_id", 0),
                    "subscribers": dict(info.get("subscribers", {})),
                }
                for key, info in self._data["subscriptions"].items()
            }

    def update_latest_chapter(self, manga_id: int, chapter_id: int) -> None:
        with self._lock:
            key = str(manga_id)
            if key in self._data["subscriptions"]:
                self._data["subscriptions"][key]["latest_chapter_id"] = chapter_id
                self._save()

    def update_title(self, manga_id: int, new_title: str) -> bool:
        with self._lock:
            key = str(manga_id)
            if key in self._data["subscriptions"] and self._data["subscriptions"][key].get("title") != new_title:
                self._data["subscriptions"][key]["title"] = new_title
                self._save()
                return True
            return False

    def set_auto_push(self, manga_id: int, conversation_id: str, enabled: bool) -> None:
        with self._lock:
            key = str(manga_id)
            if key in self._data["subscriptions"] and conversation_id in self._data["subscriptions"][key]["subscribers"]:
                self._data["subscriptions"][key]["subscribers"][conversation_id]["push_enabled"] = enabled
                self._save()

    def set_auto_push_all(self, conversation_id: str, enabled: bool) -> None:
        with self._lock:
            for info in self._data["subscriptions"].values():
                if conversation_id in info.get("subscribers", {}):
                    info["subscribers"][conversation_id]["push_enabled"] = enabled
            self._save()

    def is_auto_push_enabled(self, data: dict[str, Any], manga_id: int, conversation_id: str) -> bool:
        info = data.get(str(manga_id), {})
        sub = info.get("subscribers", {}).get(conversation_id, {})
        return bool(sub.get("push_enabled", False))

    # ── 推送偏好 ───────────────────────────────────────────────────

    def set_push_default(self, conversation_id: str, enabled: bool) -> None:
        with self._lock:
            self._data["push_defaults"][conversation_id] = enabled
            self._save()

    def get_push_default(self, conversation_id: str) -> bool:
        with self._lock:
            return bool(self._data["push_defaults"].get(conversation_id, False))

    def clear_push_default(self, conversation_id: str) -> None:
        with self._lock:
            self._data["push_defaults"].pop(conversation_id, None)
            self._save()
