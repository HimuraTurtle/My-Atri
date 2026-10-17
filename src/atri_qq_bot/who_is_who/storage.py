"""Who Is Who 的 SQLite 存储（移植自 astrbot_plugin_who_is_who）。

四张表：identities（成员身份/称呼）、topics（话题词集指纹）、
messages（群消息）、states（规则/承诺状态账本）。WAL + busy_timeout
降低 Windows 上外部进程短暂锁库导致的写入失败概率（insights 教训）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import NormalizedMessage


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=4000")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    group_key TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    preferred_name TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (group_key, user_id)
                );

                CREATE TABLE IF NOT EXISTS topics (
                    topic_id TEXT PRIMARY KEY,
                    group_key TEXT NOT NULL,
                    terms_json TEXT NOT NULL DEFAULT '[]',
                    last_active INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_topics_group_active
                    ON topics(group_key, last_active DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    timestamp INTEGER NOT NULL,
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    reply_to_message_id TEXT,
                    reply_sender_id TEXT,
                    reply_text TEXT,
                    topic_id TEXT NOT NULL DEFAULT 'unassigned',
                    role TEXT NOT NULL DEFAULT 'user',
                    PRIMARY KEY (group_key, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_group_time
                    ON messages(group_key, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_group_topic
                    ON messages(group_key, topic_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    issuer_id TEXT NOT NULL,
                    target_id TEXT,
                    scope TEXT NOT NULL,
                    topic_id TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT,
                    parent_id INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    FOREIGN KEY(parent_id) REFERENCES states(id)
                );
                CREATE INDEX IF NOT EXISTS idx_states_relevant
                    ON states(group_key, status, target_id, topic_id, updated_at DESC);
                """
            )

    def upsert_identity(self, group_key: str, user_id: str, display_name: str) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO identities(group_key, user_id, display_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_key, user_id) DO UPDATE SET
                    display_name=CASE WHEN excluded.display_name <> ''
                                      THEN excluded.display_name ELSE identities.display_name END,
                    updated_at=excluded.updated_at
                """,
                (group_key, user_id, display_name, now),
            )

    def set_preferred_name(self, group_key: str, user_id: str, value: str) -> None:
        self.upsert_identity(group_key, user_id, "")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE identities SET preferred_name=?, updated_at=? WHERE group_key=? AND user_id=?",
                (value, int(time.time()), group_key, user_id),
            )

    def get_identities(self, group_key: str, user_ids: list[str]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        placeholders = ",".join("?" for _ in user_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM identities WHERE group_key=? AND user_id IN ({placeholders})",
                (group_key, *user_ids),
            ).fetchall()
        by_id = {row["user_id"]: dict(row) for row in rows}
        return [by_id[uid] for uid in user_ids if uid in by_id]

    def get_group_identities(self, group_key: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identities WHERE group_key=? ORDER BY updated_at DESC LIMIT ?",
                (group_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_message(self, message: NormalizedMessage, max_messages: int = 500) -> None:
        self.upsert_identity(message.group_key, message.sender_id, message.sender_name)
        for user_id, name in message.mention_names.items():
            self.upsert_identity(message.group_key, user_id, name)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO messages(
                    message_id, group_key, sender_id, sender_name,
                    text, timestamp, mentions_json, reply_to_message_id,
                    reply_sender_id, reply_text, topic_id, role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_key, message_id) DO UPDATE SET
                    text=excluded.text, mentions_json=excluded.mentions_json,
                    reply_to_message_id=excluded.reply_to_message_id,
                    reply_sender_id=excluded.reply_sender_id,
                    reply_text=excluded.reply_text, topic_id=excluded.topic_id
                """,
                (
                    message.message_id,
                    message.group_key,
                    message.sender_id,
                    message.sender_name,
                    message.text,
                    message.timestamp,
                    json.dumps(message.mentions, ensure_ascii=False),
                    message.reply_to_message_id,
                    message.reply_sender_id,
                    message.reply_text,
                    message.topic_id,
                    message.role,
                ),
            )
            self._conn.execute(
                """
                DELETE FROM messages WHERE group_key=? AND rowid NOT IN (
                    SELECT rowid FROM messages WHERE group_key=?
                    ORDER BY timestamp DESC, rowid DESC LIMIT ?
                )
                """,
                (message.group_key, message.group_key, max(20, max_messages)),
            )

    def get_message(self, group_key: str, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE group_key=? AND message_id=?",
                (group_key, message_id),
            ).fetchone()
        return dict(row) if row else None

    def get_recent_messages(self, group_key: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE group_key=? ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (group_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_topics(self, group_key: str, after: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topics WHERE group_key=? AND last_active>=? ORDER BY last_active DESC LIMIT 20",
                (group_key, after),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["terms"] = set(json.loads(item.pop("terms_json")))
            result.append(item)
        return result

    def upsert_topic(self, topic_id: str, group_key: str, terms: set[str], now: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO topics(topic_id, group_key, terms_json, last_active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    terms_json=excluded.terms_json, last_active=excluded.last_active
                """,
                (topic_id, group_key, json.dumps(sorted(terms), ensure_ascii=False), now),
            )

    def add_state(self, **values) -> int:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO states(
                    group_key, kind, state_key, content, issuer_id, target_id,
                    scope, topic_id, priority, status, reason, source_message_id,
                    parent_id, metadata_json, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["group_key"], values["kind"], values["state_key"],
                    values["content"], values["issuer_id"], values.get("target_id"),
                    values["scope"], values.get("topic_id"), values.get("priority", 0),
                    values["status"], values.get("reason", ""),
                    values.get("source_message_id"), values.get("parent_id"),
                    json.dumps(values.get("metadata", {}), ensure_ascii=False),
                    now, now, values.get("expires_at"),
                ),
            )
            return int(cur.lastrowid)

    def get_state(self, state_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM states WHERE id=?", (state_id,)).fetchone()
        return dict(row) if row else None

    def update_state_status(self, state_id: int, status: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE states SET status=?, reason=?, updated_at=? WHERE id=?",
                (status, reason, int(time.time()), state_id),
            )

    def supersede_states(
        self,
        group_key: str,
        state_key: str,
        scope: str,
        target_id: str | None,
        new_priority: int,
        equal_wins: bool,
    ) -> int:
        comparator = "<=" if equal_wins else "<"
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"""
                UPDATE states SET status='superseded', reason='被更高或同级的新设置替代', updated_at=?
                WHERE group_key=? AND state_key=? AND scope=?
                  AND COALESCE(target_id, '')=COALESCE(?, '')
                  AND status='active' AND priority {comparator} ?
                """,
                (int(time.time()), group_key, state_key, scope, target_id, new_priority),
            )
            return cur.rowcount

    def clear_preferred_name_states(
        self, group_key: str, target_id: str, max_priority: int
    ) -> int:
        """作废旧称呼状态，防止被删除后旧 pending 请求死灰复燃。"""
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE states
                SET status=CASE
                      WHEN status IN ('pending_consent', 'pending_response')
                        THEN 'cancelled'
                      ELSE 'superseded'
                    END,
                    reason='称呼已由插件管理员删除', updated_at=?
                WHERE group_key=? AND state_key='preferred_name'
                  AND COALESCE(target_id, '')=COALESCE(?, '')
                  AND status IN ('active', 'pending_consent', 'pending_response')
                  AND priority<=?
                """,
                (now, group_key, target_id, max_priority),
            )
            return cur.rowcount

    def get_active_conflict(
        self,
        group_key: str,
        state_key: str,
        scope: str,
        target_id: str | None,
        topic_id: str | None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM states
                WHERE group_key=? AND state_key=? AND scope=?
                  AND COALESCE(target_id, '')=COALESCE(?, '')
                  AND COALESCE(topic_id, '')=COALESCE(?, '')
                  AND status='active'
                ORDER BY priority DESC, updated_at DESC LIMIT 1
                """,
                (group_key, state_key, scope, target_id, topic_id),
            ).fetchone()
        return dict(row) if row else None

    def reject_pending_for_target(
        self,
        group_key: str,
        target_id: str,
        issuer_ids: list[str] | None = None,
    ) -> int:
        now = int(time.time())
        issuer_ids = [str(item) for item in (issuer_ids or []) if str(item)]
        issuer_clause = ""
        issuer_args: tuple[str, ...] = ()
        if issuer_ids:
            placeholders = ",".join("?" for _ in issuer_ids)
            issuer_clause = f" AND issuer_id IN ({placeholders})"
            issuer_args = tuple(issuer_ids)
        with self._lock, self._conn:
            latest_clause = "" if issuer_ids else " LIMIT 1"
            rows = self._conn.execute(
                f"""
                SELECT id FROM states
                WHERE group_key=? AND target_id=? AND status='pending_consent'
                  AND kind <> 'commitment'
                  {issuer_clause}
                ORDER BY updated_at DESC, id DESC{latest_clause}
                """,
                (group_key, target_id, *issuer_args),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE states SET status='rejected', reason='目标用户明确拒绝', updated_at=? WHERE id IN ({placeholders})",
                (now, *ids),
            )
            self._conn.execute(
                f"UPDATE states SET status='cancelled', reason='关联请求被目标用户拒绝', updated_at=? WHERE parent_id IN ({placeholders})",
                (now, *ids),
            )
            return len(ids)

    def approve_pending_for_target(
        self,
        group_key: str,
        target_id: str,
        issuer_ids: list[str] | None = None,
        confirmed_priority: int = 1_000_000,
    ) -> int:
        now = int(time.time())
        issuer_ids = [str(item) for item in (issuer_ids or []) if str(item)]
        issuer_clause = ""
        issuer_args: tuple[str, ...] = ()
        if issuer_ids:
            placeholders = ",".join("?" for _ in issuer_ids)
            issuer_clause = f" AND issuer_id IN ({placeholders})"
            issuer_args = tuple(issuer_ids)
        with self._lock, self._conn:
            latest_clause = "" if issuer_ids else " LIMIT 1"
            rows = self._conn.execute(
                f"""
                SELECT id FROM states
                WHERE group_key=? AND target_id=? AND status='pending_consent'
                  AND kind <> 'commitment'
                  {issuer_clause}
                ORDER BY updated_at DESC, id DESC{latest_clause}
                """,
                (group_key, target_id, *issuer_args),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            approved = 0
            for state_id in ids:
                state = self._conn.execute(
                    "SELECT * FROM states WHERE id=?", (state_id,)
                ).fetchone()
                if not state:
                    continue
                conflict = self._conn.execute(
                    """
                    SELECT priority FROM states
                    WHERE group_key=? AND state_key=? AND scope=?
                      AND COALESCE(target_id, '')=COALESCE(?, '')
                      AND status='active' AND id<>?
                    ORDER BY priority DESC LIMIT 1
                    """,
                    (
                        state["group_key"], state["state_key"], state["scope"],
                        state["target_id"], state_id,
                    ),
                ).fetchone()
                if conflict and int(conflict["priority"]) > confirmed_priority:
                    self._conn.execute(
                        "UPDATE states SET status='conflicted', reason='目标用户确认，但不能覆盖插件管理员设置', updated_at=? WHERE id=?",
                        (now, state_id),
                    )
                    self._conn.execute(
                        "UPDATE states SET status='cancelled', reason='关联请求与插件管理员设置冲突', updated_at=? WHERE parent_id=?",
                        (now, state_id),
                    )
                    continue
                self._conn.execute(
                    "UPDATE states SET status='active', priority=?, reason='目标用户明确同意', updated_at=? WHERE id=?",
                    (confirmed_priority, now, state_id),
                )
                self._conn.execute(
                    "UPDATE states SET status='active', priority=?, reason='目标用户确认了关联请求', updated_at=? WHERE parent_id=?",
                    (confirmed_priority, now, state_id),
                )
                self._conn.execute(
                    """
                    UPDATE states SET status='superseded',
                        reason='被目标用户本人确认的新偏好替代', updated_at=?
                    WHERE group_key=? AND state_key=? AND scope=?
                      AND COALESCE(target_id, '')=COALESCE(?, '')
                      AND id<>? AND NOT (kind='commitment' AND parent_id=?)
                      AND status='active' AND priority<=?
                    """,
                    (
                        now,
                        state["group_key"],
                        state["state_key"],
                        state["scope"],
                        state["target_id"],
                        state_id,
                        state_id,
                        confirmed_priority,
                    ),
                )
                metadata = json.loads(state["metadata_json"] or "{}")
                preferred_name = str(metadata.get("value", ""))
                if state["state_key"] == "preferred_name" and preferred_name:
                    self.upsert_identity(state["group_key"], state["target_id"], "")
                    self._conn.execute(
                        "UPDATE identities SET preferred_name=?, updated_at=? WHERE group_key=? AND user_id=?",
                        (preferred_name, now, state["group_key"], state["target_id"]),
                    )
                approved += 1
            return approved

    def get_relevant_states(
        self,
        group_key: str,
        person_ids: list[str],
        topic_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        ids = person_ids or [""]
        placeholders = ",".join("?" for _ in ids)
        now = int(time.time())
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM states
                WHERE group_key=?
                  AND status IN (
                    'active', 'pending_consent', 'conflicted',
                    'rejected', 'cancelled', 'superseded'
                  )
                  AND (expires_at IS NULL OR expires_at>?)
                  AND (status IN ('active', 'pending_consent', 'conflicted') OR updated_at>?)
                  AND (
                    scope='group' OR topic_id=? OR issuer_id IN ({placeholders})
                    OR target_id IN ({placeholders})
                  )
                ORDER BY
                  CASE status
                    WHEN 'active' THEN 0
                    WHEN 'pending_consent' THEN 1
                    WHEN 'conflicted' THEN 2
                    ELSE 3
                  END,
                  priority DESC, updated_at DESC
                LIMIT ?
                """,
                (group_key, now, now - 86400, topic_id, *ids, *ids, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_states(self, group_key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM states WHERE group_key=? ORDER BY updated_at DESC LIMIT ?",
                (group_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_group(self, group_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM messages WHERE group_key=?", (group_key,))
            self._conn.execute("DELETE FROM topics WHERE group_key=?", (group_key,))
            self._conn.execute("DELETE FROM states WHERE group_key=?", (group_key,))
            self._conn.execute("DELETE FROM identities WHERE group_key=?", (group_key,))

    def cleanup(self, retention_days: int) -> None:
        before = int(time.time()) - max(1, retention_days) * 86400
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM messages WHERE timestamp<?", (before,))
            self._conn.execute("DELETE FROM topics WHERE last_active<?", (before,))
            self._conn.execute(
                "UPDATE states SET status='expired', updated_at=? WHERE expires_at IS NOT NULL AND expires_at<=? AND status='active'",
                (int(time.time()), int(time.time())),
            )
