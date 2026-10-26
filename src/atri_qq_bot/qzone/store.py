"""QQ 空间本地状态：devlog 表沿用旧 qzone_automation 库（水位无缝继承）。

设计要点：
- 同一个 ``data/qzone_automation.sqlite3``：开发日志的提交水位、草稿、
  发布历史跨版本保留，用户升级后不需要任何迁移操作；
- 动作日志表（action+tid 唯一成功索引）同时承担点赞/评论幂等和频率统计；
- 其余旧表（内容池/评论池/目标人/槽位）只保留不再读写，避免破坏现库。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..runtime.paths import PROJECT_ROOT

DEFAULT_SETTINGS: dict[str, Any] = {
    "devlog_last_scan_sha": "",
    "devlog_last_scan_at": 0.0,
    "auto_comment_next_run": 0.0,
    "post_topics": [
        "日常趣事与小插曲",
        "人生哲理与反常识思考",
        "情感、关系与内心矛盾",
        "抽象乐子与反差幽默",
        "原作剧情、角色对白与世界观",
        "季节天气与身边观察",
        "学习或技术上的小发现",
    ],
}


class QzoneStore:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = PROJECT_ROOT / "data" / "qzone_automation.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS qzone_settings (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qzone_automation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    tid TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qzone_logs_created
                  ON qzone_automation_logs(created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_qzone_logs_success_tid
                  ON qzone_automation_logs(action, tid) WHERE status='success' AND tid <> '';
                CREATE TABLE IF NOT EXISTS qzone_devlog_commits (
                    sha TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    commit_type TEXT NOT NULL DEFAULT 'other',
                    committed_at REAL NOT NULL DEFAULT 0,
                    first_seen_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    post_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS qzone_devlog_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    commit_shas TEXT NOT NULL DEFAULT '[]',
                    prompt TEXT NOT NULL DEFAULT '',
                    model_profile_id TEXT NOT NULL DEFAULT '',
                    generated_at REAL,
                    published_at REAL,
                    tid TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_qzone_devlog_commits_pending
                  ON qzone_devlog_commits(status, committed_at);
                CREATE INDEX IF NOT EXISTS idx_qzone_devlog_posts_status
                  ON qzone_devlog_posts(status, id DESC);
                """
            )
            for key, value in DEFAULT_SETTINGS.items():
                db.execute(
                    "INSERT OR IGNORE INTO qzone_settings(key, value_json) VALUES(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

    # ---- 设置 ----

    def settings(self) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT key, value_json FROM qzone_settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            try:
                key = str(row["key"])
                if key in DEFAULT_SETTINGS:
                    result[key] = json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                continue
        return result

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        merged = self.settings()
        merged.update({key: value for key, value in values.items() if key in DEFAULT_SETTINGS})
        with self._lock, self._connect() as db:
            for key, value in merged.items():
                db.execute(
                    "INSERT INTO qzone_settings(key, value_json) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return merged

    # ---- 动作日志（点赞/评论幂等 + 频率统计）----

    def log(
        self,
        action: str,
        *,
        user_id: str = "",
        tid: str = "",
        content: str = "",
        status: str = "success",
        error: str = "",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO qzone_automation_logs"
                "(action,user_id,tid,content,status,error,created_at) VALUES(?,?,?,?,?,?,?)",
                (action, str(user_id), str(tid), str(content), status, str(error)[:500], time.time()),
            )

    def already_logged(self, action: str, tid: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT 1 FROM qzone_automation_logs WHERE action=? AND tid=? AND status='success' LIMIT 1",
                (action, str(tid)),
            ).fetchone() is not None

    def logs(self, limit: int = 50, *, action: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        query = "SELECT * FROM qzone_automation_logs"
        params: tuple[Any, ...] = ()
        if str(action or "").strip():
            query += " WHERE action=?"
            params = (str(action).strip(),)
        query += " ORDER BY created_at DESC LIMIT ?"
        params = (*params, limit)
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def recent_post_contents(self, limit: int = 5) -> list[str]:
        """最近已发布正文，用于生成前排重；没有内容池后动作日志就是唯一底稿。"""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT content FROM qzone_automation_logs "
                "WHERE action='post' AND status='success' AND content<>'' "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, min(20, int(limit))),),
            ).fetchall()
        return [str(row["content"]) for row in rows]

    def count_success_since(self, action: str, since: float) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM qzone_automation_logs "
                "WHERE action=? AND status='success' AND created_at>=?",
                (str(action), float(since)),
            ).fetchone()
        return int(row["count"] if row else 0)

    # ---- 开发日志 ----

    def devlog_add_commits(self, commits: list[dict[str, Any]]) -> int:
        now = time.time()
        added = 0
        with self._lock, self._connect() as db:
            for item in commits:
                sha = str(item.get("sha") or "").strip()
                if not sha:
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO qzone_devlog_commits"
                    "(sha,subject,commit_type,committed_at,first_seen_at,status) VALUES(?,?,?,?,?,?)",
                    (
                        sha[:64],
                        str(item.get("subject") or "")[:300],
                        str(item.get("commit_type") or "other")[:24],
                        float(item.get("committed_at") or 0),
                        now,
                        "pending" if item.get("visible", True) else "skipped",
                    ),
                )
                added += int(cursor.rowcount or 0)
        return added

    def devlog_pending_commits(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM qzone_devlog_commits WHERE status='pending' ORDER BY committed_at, sha"
            ).fetchall()
        return [dict(row) for row in rows]

    def devlog_create_post_if_absent(
        self,
        content: str,
        *,
        commit_shas: list[str],
        prompt: str = "",
    ) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text or len(text) > 1000:
            raise ValueError("开发手记不能为空且最多 1000 字")
        now = time.time()
        shas_json = json.dumps([str(item) for item in commit_shas], ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM qzone_devlog_posts WHERE status='draft' LIMIT 1").fetchone():
                return None
            cursor = db.execute(
                "INSERT INTO qzone_devlog_posts(content,status,commit_shas,prompt,generated_at) "
                "VALUES(?,'draft',?,?,?)",
                (text, shas_json, str(prompt or "")[:2000], now),
            )
            return {
                "id": int(cursor.lastrowid), "content": text, "status": "draft",
                "commit_shas": shas_json, "prompt": str(prompt or ""),
                "generated_at": now, "published_at": None, "tid": "", "last_error": "",
            }

    def devlog_draft(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM qzone_devlog_posts WHERE status='draft' ORDER BY id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def devlog_posts(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM qzone_devlog_posts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def devlog_update_post_content(self, post_id: int, content: str) -> None:
        text = str(content or "").strip()
        if not text or len(text) > 1000:
            raise ValueError("开发手记不能为空且最多 1000 字")
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE qzone_devlog_posts SET content=? WHERE id=? AND status='draft'",
                (text, int(post_id)),
            )

    def devlog_delete_post(self, post_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM qzone_devlog_posts WHERE id=? AND status IN ('draft','failed')",
                (int(post_id),),
            )

    def devlog_begin_publish(self, post_id: int) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE qzone_devlog_posts SET status='publishing' WHERE id=? AND status='draft'",
                (int(post_id),),
            )
        return int(cursor.rowcount or 0) == 1

    def devlog_finish_publish(self, post_id: int, tid: str) -> list[str]:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT commit_shas FROM qzone_devlog_posts WHERE id=? AND status='publishing'",
                (int(post_id),),
            ).fetchone()
            if row is None:
                raise RuntimeError("开发手记发布状态已失效")
            try:
                shas = [str(item) for item in json.loads(str(row["commit_shas"] or "[]"))]
            except json.JSONDecodeError:
                shas = []
            cursor = db.execute(
                "UPDATE qzone_devlog_posts SET status='published', published_at=?, tid=?, last_error='' "
                "WHERE id=? AND status='publishing'",
                (now, str(tid or ""), int(post_id)),
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError("开发手记发布状态已失效")
            for sha in shas:
                db.execute(
                    "UPDATE qzone_devlog_commits SET status='published', post_id=? "
                    "WHERE sha=? AND status='pending'",
                    (int(post_id), sha),
                )
            return shas

    def devlog_fail_publish(self, post_id: int, error: str, *, consume_commits: bool) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT commit_shas FROM qzone_devlog_posts WHERE id=? AND status='publishing'",
                (int(post_id),),
            ).fetchone()
            db.execute(
                "UPDATE qzone_devlog_posts SET status='failed', last_error=?, published_at=? "
                "WHERE id=? AND status='publishing'",
                (str(error)[:500], now, int(post_id)),
            )
            if row is not None and consume_commits:
                try:
                    shas = [str(item) for item in json.loads(str(row["commit_shas"] or "[]"))]
                except json.JSONDecodeError:
                    shas = []
                for sha in shas:
                    db.execute(
                        "UPDATE qzone_devlog_commits SET status='skipped' WHERE sha=? AND status='pending'",
                        (sha,),
                    )

    def devlog_count_published_since(self, since: float) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM qzone_devlog_posts WHERE status='published' AND published_at>=?",
                (float(since),),
            ).fetchone()
        return int(row["count"] if row else 0)
