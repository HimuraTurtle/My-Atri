"""屏幕摘要存储：独立 SQLite，只存文本摘要，永不存原图。

与 qzone_automation 的存取层同一风格：线程锁 + 原子建表，
check_same_thread=False 供 asyncio.to_thread 的多线程调用。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class ScreenWatchStore:
    # 摘要只通过 recent(max_age_hours) 近期读取，保留 30 天绰绰有余；
    # 无删除策略会让围观模式的长期运行无限累积行。
    _RETENTION_DAYS = 30.0
    _PRUNE_INTERVAL_SECONDS = 86400.0

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._last_pruned_at = 0.0
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screen_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'auto',
                    window_title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_screen_summaries_time"
                " ON screen_summaries(created_at DESC)"
            )

    def add_summary(self, summary: str, kind: str = "auto", window_title: str = "") -> int:
        text = str(summary or "").strip()
        if not text:
            return 0
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO screen_summaries(created_at, kind, window_title, summary)"
                " VALUES (?, ?, ?, ?)",
                (time.time(), kind, str(window_title or "")[:200], text),
            )
            inserted = int(cursor.lastrowid or 0)
        self._maybe_prune()
        return inserted

    def _maybe_prune(self) -> None:
        """每 24 小时至多清一次过期摘要（节流写，不额外起定时器）。"""
        now = time.monotonic()
        if now - self._last_pruned_at < self._PRUNE_INTERVAL_SECONDS:
            return
        self._last_pruned_at = now
        cutoff = time.time() - self._RETENTION_DAYS * 86400.0
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM screen_summaries WHERE created_at < ?", (cutoff,)
                )
        except sqlite3.Error:
            pass

    def recent(self, limit: int = 3, max_age_hours: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if max_age_hours is None:
                rows = self._conn.execute(
                    "SELECT * FROM screen_summaries ORDER BY created_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM screen_summaries WHERE created_at >= ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (time.time() - float(max_age_hours) * 3600.0, max(1, int(limit))),
                ).fetchall()
        return [dict(row) for row in rows]

    def count_since(self, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM screen_summaries WHERE created_at >= ?",
                (float(since_ts),),
            ).fetchone()
        return int(row[0] if row else 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
