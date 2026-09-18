"""LivingMemory SQLite 存储。

独立于 users.json 与 semantic_memory.sqlite3：本模块只存"对话总结出的
情节记忆"，与画像记忆互不干扰。FTS5 表存的是 jieba 预分词后的空格
分隔文本（unicode61 只认空格边界，直接存中文原文等于没有索引——这是
原检索链路中文失效的根因）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .atoms import MemoryAtom
from .retrieval import tokenize


class LivingMemoryStore:
    """短事务、WAL 模式的同步存储；线程安全靠每操作新连接 + busy_timeout。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=8.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=8000")
        return connection

    @contextmanager
    def _session(self):
        """提交 + 显式关闭：``with conn`` 只管事务不管关闭，连接对象一旦
        进入引用环就只能等 gc 回收，期间 WAL 文件无法收缩、库文件被占用。"""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lm_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lm_messages_session
                    ON lm_messages(session_id, message_id);

                CREATE TABLE IF NOT EXISTS lm_memories (
                    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    topics_json TEXT NOT NULL DEFAULT '[]',
                    key_facts_json TEXT NOT NULL DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    sentiment TEXT NOT NULL DEFAULT 'neutral',
                    atom_type TEXT NOT NULL DEFAULT 'episodic',
                    ttl_days REAL NOT NULL DEFAULT 7.0,
                    decay_type TEXT NOT NULL DEFAULT 'exponential',
                    status TEXT NOT NULL DEFAULT 'active',
                    reinforcement_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at REAL NOT NULL,
                    last_reinforced_at REAL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    source_start REAL NOT NULL DEFAULT 0,
                    source_end REAL NOT NULL DEFAULT 0,
                    source_label TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_lm_memories_session
                    ON lm_memories(session_id, status, created_at DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS lm_memories_fts USING fts5(
                    content_tokens,
                    memory_id UNINDEXED,
                    session_id UNINDEXED,
                    tokenize='unicode61'
                );

                CREATE TABLE IF NOT EXISTS lm_embeddings (
                    memory_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dims INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    # ---------------- 消息 buffer ----------------

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
        created_at: float | None = None,
    ) -> int:
        now = created_at if created_at is not None else time.time()
        with self._write_lock, self._session() as db:
            cursor = db.execute(
                "INSERT INTO lm_messages(session_id, role, sender_id, sender_name, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, str(sender_id), str(sender_name), str(content), now),
            )
            return int(cursor.lastrowid or 0)

    def pending_message_count(self, session_id: str, since_message_id: int = 0) -> int:
        with self._session() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM lm_messages WHERE session_id = ? AND message_id > ?",
                (session_id, int(since_message_id)),
            ).fetchone()
        return int(row[0] if row else 0)

    def load_messages(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._session() as db:
            rows = db.execute(
                "SELECT message_id, role, sender_id, sender_name, content, created_at "
                "FROM lm_messages WHERE session_id = ? ORDER BY message_id LIMIT ?",
                (session_id, max(1, min(200, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_messages(self, session_id: str, up_to_message_id: int) -> int:
        with self._write_lock, self._session() as db:
            cursor = db.execute(
                "DELETE FROM lm_messages WHERE session_id = ? AND message_id <= ?",
                (session_id, int(up_to_message_id)),
            )
            return int(cursor.rowcount or 0)

    def trim_sessions(self, keep_per_session: int = 200) -> int:
        """防止长静默会话无限堆积未总结消息。"""
        with self._write_lock, self._session() as db:
            cursor = db.execute(
                """
                DELETE FROM lm_messages WHERE message_id IN (
                    SELECT message_id FROM (
                        SELECT message_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY session_id ORDER BY message_id DESC
                               ) AS rn
                        FROM lm_messages
                    ) WHERE rn > ?
                )
                """,
                (max(10, int(keep_per_session)),),
            )
            return int(cursor.rowcount or 0)

    # ---------------- 记忆写入 ----------------

    def add_memory(self, atom: MemoryAtom, embedding: list[float] | None, embedding_model: str = "") -> int:
        tokens = tokenize(atom.content)
        token_text = " ".join(tokens)
        with self._write_lock, self._session() as db:
            cursor = db.execute(
                """INSERT INTO lm_memories
                (session_id, content, topics_json, key_facts_json, importance, sentiment,
                 atom_type, ttl_days, decay_type, status, reinforcement_count,
                 last_accessed_at, last_reinforced_at, expires_at, created_at,
                 source_start, source_end, source_label)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    atom.session_id, atom.content,
                    json.dumps(atom.topics, ensure_ascii=False),
                    json.dumps(atom.key_facts, ensure_ascii=False),
                    atom.importance, atom.sentiment, atom.atom_type,
                    atom.ttl_days, atom.decay_type, atom.status,
                    atom.reinforcement_count, atom.last_accessed_at,
                    atom.last_reinforced_at, atom.expires_at, atom.created_at,
                    atom.source_start, atom.source_end, atom.source_label,
                ),
            )
            memory_id = int(cursor.lastrowid or 0)
            if token_text:
                db.execute(
                    "INSERT INTO lm_memories_fts(content_tokens, memory_id, session_id) VALUES (?,?,?)",
                    (token_text, memory_id, atom.session_id),
                )
            if embedding and memory_id:
                db.execute(
                    "INSERT OR REPLACE INTO lm_embeddings(memory_id, model, dims, vector, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        memory_id, str(embedding_model or "unknown"), len(embedding),
                        _pack_vector(embedding), time.time(),
                    ),
                )
        return memory_id

    def reinforce_memory(self, memory_id: int, confidence: float, now: float) -> bool:
        row = self.get_memory(memory_id)
        if row is None:
            return False
        atom = MemoryAtom.from_row(row)
        atom.importance = max(atom.importance, min(1.0, confidence))
        atom.reinforce(now)
        with self._write_lock, self._session() as db:
            db.execute(
                """UPDATE lm_memories SET importance=?, ttl_days=?, decay_type=?, status=?,
                reinforcement_count=?, last_accessed_at=?, last_reinforced_at=?, expires_at=?,
                content=content WHERE memory_id=?""",
                (
                    atom.importance, atom.ttl_days, atom.decay_type, atom.status,
                    atom.reinforcement_count, atom.last_accessed_at, atom.last_reinforced_at,
                    atom.expires_at, memory_id,
                ),
            )
        return True

    def touch_memory(self, memory_id: int, now: float) -> None:
        with self._write_lock, self._session() as db:
            db.execute(
                "UPDATE lm_memories SET last_accessed_at=? WHERE memory_id=?",
                (now, memory_id),
            )

    # ---------------- 记忆读取 ----------------

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute(
                "SELECT * FROM lm_memories WHERE memory_id = ?", (int(memory_id),)
            ).fetchone()
        return _decode_row(dict(row)) if row else None

    def search_fts(self, query_tokens: list[str], session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        match_query = _fts_match_query(query_tokens)
        if not match_query:
            return []
        with self._session() as db:
            rows = db.execute(
                """SELECT e.memory_id, e.session_id, e.content, e.importance, e.status,
                          e.created_at, e.last_accessed_at
                FROM lm_memories_fts
                JOIN lm_memories AS e ON e.memory_id = lm_memories_fts.memory_id
                WHERE lm_memories_fts MATCH ? AND lm_memories_fts.session_id = ?
                  AND e.status = 'active'
                ORDER BY bm25(lm_memories_fts)
                LIMIT ?""",
                (match_query, session_id, max(1, min(30, int(limit)))),
            ).fetchall()
        return [_decode_row(dict(row)) for row in rows]

    def load_embeddings(self, session_id: str, model: str) -> list[tuple[int, list[float]]]:
        with self._session() as db:
            rows = db.execute(
                """SELECT m.memory_id, m.status, m.importance, m.created_at, m.last_accessed_at,
                          m.content, m.ttl_days, m.decay_type, e.vector
                FROM lm_memories m
                JOIN lm_embeddings e ON e.memory_id = m.memory_id
                WHERE m.session_id = ? AND e.model = ? AND m.status = 'active'
                LIMIT 2000""",
                (session_id, str(model)),
            ).fetchall()
        return [(int(row["memory_id"]), _unpack_vector(row["vector"])) for row in rows]

    def embedding_exists(self, model: str) -> bool:
        with self._session() as db:
            row = db.execute(
                "SELECT 1 FROM lm_embeddings WHERE model = ? LIMIT 1", (str(model),)
            ).fetchone()
        return row is not None

    def recent_memories(self, session_id: str, max_age_hours: float, limit: int = 2) -> list[dict[str, Any]]:
        threshold = time.time() - max(1.0, float(max_age_hours)) * 3600.0
        with self._session() as db:
            rows = db.execute(
                """SELECT * FROM lm_memories
                WHERE session_id = ? AND status = 'active' AND created_at >= ?
                ORDER BY created_at DESC LIMIT ?""",
                (session_id, threshold, max(1, int(limit))),
            ).fetchall()
        return [_decode_row(dict(row)) for row in rows]

    # ---------------- 生命周期维护 ----------------

    def run_maintenance(self, forget_delay_days: float, purge_delay_days: float) -> dict[str, int]:
        now = time.time()
        forget_at = now - max(1.0, forget_delay_days) * 86400.0
        purge_at = now - max(2.0, purge_delay_days) * 86400.0
        result: dict[str, int] = {}
        with self._write_lock, self._session() as db:
            cursor = db.execute(
                "UPDATE lm_memories SET status='dormant' WHERE status='active' AND expires_at < ?",
                (now,),
            )
            result["dormant"] = int(cursor.rowcount or 0)
            cursor = db.execute(
                "UPDATE lm_memories SET status='forgotten' WHERE status='dormant' AND expires_at < ?",
                (forget_at,),
            )
            forgotten_ids = [int(row[0]) for row in db.execute(
                "SELECT memory_id FROM lm_memories WHERE status='forgotten' AND expires_at < ?",
                (purge_at,),
            ).fetchall()]
            result["forgotten"] = int(cursor.rowcount or 0)
            purged = 0
            if forgotten_ids:
                marks = ",".join("?" * len(forgotten_ids))
                # 休眠/遗忘记忆必须移出 FTS，否则会继续出现在检索结果里。
                db.execute(
                    f"DELETE FROM lm_memories_fts WHERE memory_id IN ({marks})", forgotten_ids
                )
                db.execute(
                    f"DELETE FROM lm_embeddings WHERE memory_id IN ({marks})", forgotten_ids
                )
                cursor = db.execute(
                    f"DELETE FROM lm_memories WHERE memory_id IN ({marks})", forgotten_ids
                )
                purged = int(cursor.rowcount or 0)
            result["purged"] = purged
        return result

    def stats(self) -> dict[str, int]:
        with self._session() as db:
            memories = db.execute("SELECT COUNT(*) FROM lm_memories").fetchone()[0]
            active = db.execute(
                "SELECT COUNT(*) FROM lm_memories WHERE status='active'"
            ).fetchone()[0]
            buffered = db.execute("SELECT COUNT(*) FROM lm_messages").fetchone()[0]
            embedded = db.execute("SELECT COUNT(*) FROM lm_embeddings").fetchone()[0]
        return {
            "memories": int(memories),
            "active": int(active),
            "buffered": int(buffered),
            "embedded": int(embedded),
        }


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    for json_key in ("topics_json", "key_facts_json"):
        if json_key in row:
            plain_key = json_key[:-5]
            try:
                row[plain_key] = json.loads(row[json_key] or "[]")
            except (TypeError, ValueError):
                row[plain_key] = []
    return row


def _fts_match_query(tokens: list[str]) -> str:
    parts: list[str] = []
    for token in tokens[:16]:
        cleaned = str(token or "").strip().strip('"')
        if not cleaned or any(ch in cleaned for ch in ('"', "*", "(", ")", ":", "^", "-")):
            continue
        parts.append(f'"{cleaned}"')
    return " OR ".join(parts)


def _pack_vector(vector: list[float]) -> bytes:
    import struct

    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    import struct

    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))
