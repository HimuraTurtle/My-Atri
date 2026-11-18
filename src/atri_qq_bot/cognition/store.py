from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterator, TypeVar

from .contracts import (
    AffectiveState,
    CognitionSnapshot,
    GoalState,
    SelfState,
    WorldState,
)


T = TypeVar("T")


class CognitionStateStore:
    """Durable working-memory store, separate from long-term user memory.

    A turn checkpoint and a durable user fact are different concepts.  This
    store owns only current thought, mood and goal continuity, so failures or
    experiments cannot pollute L1/L2/L3 memory.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._setup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=3.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=3000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """提交 + 显式关闭：``with conn`` 只管事务不管关闭，泄漏的连接会让
        WAL 无法收缩、库文件在 Windows 上无法被备份或删除（与 retrieval
        repository 同一教训）。"""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _setup(self) -> None:
        with self._lock, self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cognition_threads (
                    thread_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS cognition_experiences (
                    experience_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cognition_experience_thread
                ON cognition_experiences(thread_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS cognition_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def load(self, thread_id: str) -> CognitionSnapshot:
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT state_json FROM cognition_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if not row:
            return CognitionSnapshot(thread_id=thread_id)
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return CognitionSnapshot(thread_id=thread_id)
        return _snapshot_from_dict(thread_id, payload)

    def save(self, snapshot: CognitionSnapshot) -> None:
        now = snapshot.updated_at or time.time()
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._session() as connection:
            connection.execute(
                """
                INSERT INTO cognition_threads(thread_id, state_json, updated_at, version)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at,
                    version=excluded.version
                """,
                (snapshot.thread_id, payload, now, snapshot.version),
            )

    def add_experience(
        self,
        thread_id: str,
        turn_id: str,
        summary: str,
        *,
        importance: float = 0.5,
    ) -> None:
        cleaned = " ".join(str(summary or "").split())[:300]
        if not cleaned:
            return
        with self._lock, self._session() as connection:
            connection.execute(
                """
                INSERT INTO cognition_experiences(thread_id, turn_id, summary, importance, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (thread_id, turn_id, cleaned, max(0.0, min(1.0, importance)), time.time()),
            )
            connection.execute(
                """
                DELETE FROM cognition_experiences
                WHERE experience_id IN (
                    SELECT experience_id FROM cognition_experiences
                    WHERE thread_id = ? ORDER BY created_at DESC LIMIT -1 OFFSET 40
                )
                """,
                (thread_id,),
            )

    def status(self) -> dict[str, Any]:
        try:
            with self._lock, self._session() as connection:
                threads = int(connection.execute("SELECT COUNT(*) FROM cognition_threads").fetchone()[0])
                experiences = int(connection.execute("SELECT COUNT(*) FROM cognition_experiences").fetchone()[0])
                latest = connection.execute(
                    "SELECT MAX(updated_at) FROM cognition_threads"
                ).fetchone()[0]
            return {
                "available": True,
                "threads": threads,
                "experiences": experiences,
                "last_updated_at": float(latest or 0.0),
                "path": str(self.path),
            }
        except sqlite3.Error as exc:
            return {"available": False, "error": str(exc)[:180], "path": str(self.path)}


def _dataclass_from_dict(cls: type[T], value: Any) -> T:
    source = value if isinstance(value, dict) else {}
    accepted = {item.name for item in fields(cls)}
    payload = {key: source[key] for key in accepted if key in source}
    for key, item in list(payload.items()):
        if isinstance(item, list):
            payload[key] = tuple(item)
    return cls(**payload)


def _snapshot_from_dict(thread_id: str, payload: dict[str, Any]) -> CognitionSnapshot:
    return CognitionSnapshot(
        thread_id=thread_id,
        world=_dataclass_from_dict(WorldState, payload.get("world")),
        goal=_dataclass_from_dict(GoalState, payload.get("goal")),
        affect=_dataclass_from_dict(AffectiveState, payload.get("affect")),
        self_state=_dataclass_from_dict(SelfState, payload.get("self_state")),
        updated_at=float(payload.get("updated_at") or 0.0),
        version=int(payload.get("version") or 1),
    )


__all__ = ["CognitionStateStore"]
