"""ATRI 开发日志：把代码提交转写成 ATRI 口吻的开发手记并发布到 QQ 空间。

设计约束（与全项目降级契约一致）：
- 提交信息是唯一事实源，生成失败、无可见变更时静默或保留草稿，绝不编造。
- 公开平台安全闸：类型白名单 + 敏感信息拦截 + 每日上限。
- 幂等：草稿/发布状态机由 SQLite 原子更新保证，跨进程不会重复发布。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Callable

from atri_qq_bot.runtime import PROJECT_ROOT

from .rules import DEFAULT_SETTINGS, devlog_material_prompt, resolve_timezone

DEVLOG_SCAN_INTERVAL_SECONDS = 600.0
DEVLOG_GENERATE_RETRY_SECONDS = 300.0
DEVLOG_PUBLISH_RETRY_SECONDS = 1800.0

# conventional 前缀到标准类型的归一；仓库实际使用「模块: 中文描述」风格，
# 前缀命中这些词时直接采用，其余前缀按描述关键词推断。
_COMMIT_TYPE_ALIASES = {
    "feat": "feat", "feature": "feat", "新增": "feat",
    "fix": "fix", "bugfix": "fix", "修复": "fix",
    "perf": "perf", "优化": "perf",
    "refactor": "refactor", "重构": "refactor",
    "docs": "docs", "doc": "docs", "文档": "docs",
    "test": "test", "tests": "test", "测试": "test",
    "chore": "chore", "杂项": "chore", "build": "chore",
    "ci": "chore", "style": "chore",
    "revert": "chore", "baseline": "chore", "基线": "chore", "快照": "chore",
}
_COMMIT_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("feat", ("新增", "添加", "接入", "支持", "上线", "增加", "新增工具", "完善")),
    ("fix", ("修复", "解决", "防止", "避免", "修正", "兜底", "收紧", "回滚", "堵住")),
    ("perf", ("优化", "提速", "加速", "降耗", "收敛", "减少调用", "压缩")),
    ("refactor", ("重构", "移除", "删除", "清理", "拆除", "迁移", "整理", "拆分", "拆出", "改名")),
    ("chore", ("基线", "快照", "依赖", "版本", "初始化", "提交", "整理目录")),
)

# 公开平台敏感信息拦截：命中任意一条的提交不进入手记（标记 skipped）。
_SENSITIVE_SUBJECT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"https?://\S+",
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
        r"[A-Za-z]:\\[^\s\"']+",
        r"(?:^|\s)/[\w.-]+/[\w./-]+",
        r"[\w.-]+\.(?:py|js|ts|json|env|yaml|yml|toml|ini|cfg|db|sqlite3?|log|txt|md|html|css|sh|bat)\b",
        r"(?:api[_-]?key|apikey|token|password|passwd|secret|credential|私钥|密钥|密码|口令)",
        r"[\w.+-]+@[\w-]+\.[\w.]+",
        r"(?:ssh|rsa|sk-|ghp_|gho_)[\w-]{8,}",
    )
)
_UNCERTAIN_SEND_MARKERS = ("timed out", "timeout", "time-out", "超时")


class DevLogUnavailable(RuntimeError):
    """git 不可用或仓库缺失，采集层必须整体降级而不是编造提交。"""


def classify_commit(subject: str) -> str:
    """把提交标题映射到 feat/fix/perf/refactor/docs/test/chore/other。"""
    text = str(subject or "").strip()
    head, sep, rest = text.partition(":")
    token = head.strip()
    if sep and token and len(token) <= 24 and " " not in token:
        normalized = token.casefold()
        if normalized in _COMMIT_TYPE_ALIASES:
            return _COMMIT_TYPE_ALIASES[normalized]
        # 「模块: 描述」风格：按描述关键词推断类型。
        body = rest
    else:
        body = text
    for commit_type, keywords in _COMMIT_TYPE_KEYWORDS:
        if any(keyword in body for keyword in keywords):
            return commit_type
    return "other"


def is_sensitive_subject(subject: str) -> bool:
    """公开安全闸：包含链接、路径、密钥、邮箱等内部信息的提交不外发。"""
    text = str(subject or "")
    return any(pattern.search(text) for pattern in _SENSITIVE_SUBJECT_PATTERNS)


def _run_git(root: Any, *args: str, timeout: float = 20.0) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
        )
    except FileNotFoundError as exc:
        raise DevLogUnavailable("环境中没有 git，开发日志采集不可用") from exc
    except subprocess.TimeoutExpired as exc:
        raise DevLogUnavailable("git 命令超时") from exc
    except subprocess.CalledProcessError as exc:
        raise DevLogUnavailable(
            f"git 命令失败：{(exc.stderr or '').strip()[:200] or exc.returncode}"
        ) from exc
    return str(completed.stdout or "")


def collect_commits(root: Any, since_sha: str = "") -> list[dict[str, Any]]:
    """读取水位之后的新提交（旧→新）；水位为空时只取当前 HEAD。"""
    if not str(since_sha or "").strip():
        head = _run_git(root, "rev-parse", "HEAD").strip()
        if not head:
            raise DevLogUnavailable("仓库当前没有提交")
        return [{"sha": head, "subject": "", "committed_at": 0.0, "init_only": True}]
    output = _run_git(
        root,
        "log",
        "--no-merges",
        "--format=%H%x1f%s%x1f%ct",
        "-n",
        "300",
        f"{since_sha.strip()}..HEAD",
    )
    commits: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[0].strip():
            continue
        commits.append(
            {
                "sha": parts[0].strip(),
                "subject": parts[1].strip(),
                "committed_at": float(parts[2].strip() or 0),
            }
        )
    commits.reverse()
    return commits


class DevLogService:
    """采集 → 生成 → 发布的开发手记闭环；状态机由 store 的原子更新保证幂等。"""

    def __init__(
        self,
        store: Any,
        admin_factory: Callable[[], Any],
        post_generator: Callable[[str, str], Any] | None,
        root: Any | None = None,
    ) -> None:
        self.store = store
        self.admin_factory = admin_factory
        self.post_generator = post_generator
        self.root = PROJECT_ROOT if root is None else root
        self._scan_after = 0.0
        self._generate_after = 0.0
        self._publish_after = 0.0

    # ---- 采集 ----

    def scan(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and now < self._scan_after:
            return {"ok": True, "skipped": "采集冷却中"}
        self._scan_after = now + DEVLOG_SCAN_INTERVAL_SECONDS
        settings = self.store.settings()
        watermark = str(settings.get("devlog_last_scan_sha") or "").strip()
        commits = collect_commits(self.root, watermark)
        if watermark:
            usable = [item for item in commits if not item.get("init_only")]
            for item in usable:
                item["commit_type"] = classify_commit(str(item.get("subject") or ""))
                item["visible"] = (
                    item["commit_type"] in set(self._visibility_types(settings))
                    and not is_sensitive_subject(str(item.get("subject") or ""))
                )
            added = self.store.devlog_add_commits(usable)
            visible_added = sum(1 for item in usable if item.get("visible"))
        else:
            # 首次开启只初始化水位，绝不能把整段历史一次性广播出去。
            added = 0
            visible_added = 0
            self.store.log("devlog_scan", status="success", error="初始化开发日志水位")
        head = commits[-1]["sha"] if commits else watermark
        if head and head != watermark:
            self.store.save_settings({"devlog_last_scan_sha": head, "devlog_last_scan_at": now})
        return {
            "ok": True,
            "initialized": not bool(watermark),
            "scanned": len(commits),
            "added": added,
            "visible_added": visible_added,
        }

    def _visibility_types(self, settings: dict[str, Any]) -> list[str]:
        raw = settings.get("devlog_visibility_types")
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw or "").replace("，", ",").split(",")
        return [str(item).strip().casefold() for item in values if str(item).strip()]

    # ---- 生成 ----

    def generate(self, *, force: bool = False) -> dict[str, Any]:
        existing = self.store.devlog_draft()
        if existing is not None:
            return {"ok": True, "item": existing, "skipped": "已有草稿"}
        if not force and time.time() < self._generate_after:
            return {"ok": True, "skipped": "生成冷却中"}
        pending = self.store.devlog_pending_commits()
        if not pending:
            return {"ok": True, "skipped": "没有待写进手记的提交"}
        if self.post_generator is None:
            return {"ok": True, "skipped": "生成器尚未就绪"}
        settings = self.store.settings()
        prompt = devlog_material_prompt(pending)
        model_id = str(settings.get("post_model_profile_id") or "")
        try:
            generated = self.post_generator(prompt, model_id)
            content = str((generated or {}).get("content") or "").strip()
        except Exception as exc:
            # 生成失败保留待写提交，退避后重试；绝不编一条“今天没有更新”充数。
            self._generate_after = time.time() + DEVLOG_GENERATE_RETRY_SECONDS
            self.store.log("devlog_generate", status="failed", error=str(exc))
            return {"ok": False, "skipped": "生成失败，稍后自动重试", "error": str(exc)}
        issues = self._content_issues(content)
        if issues:
            self._generate_after = time.time() + DEVLOG_GENERATE_RETRY_SECONDS
            self.store.log("devlog_generate", status="failed", error="；".join(issues))
            return {"ok": False, "skipped": f"生成稿未通过检查：{'；'.join(issues)}"}
        self._generate_after = 0.0
        item = self.store.devlog_create_post_if_absent(
            content,
            commit_shas=[str(entry.get("sha") or "") for entry in pending],
            prompt=prompt,
            model_profile_id=model_id,
        )
        if item is None:
            item = self.store.devlog_draft()
        return {"ok": True, "item": item}

    @staticmethod
    def _content_issues(content: str) -> list[str]:
        text = str(content or "").strip()
        if len(text) < 20:
            return ["正文过短"]
        if len(text) > 400:
            return ["正文超过 400 字"]
        if any(pattern.search(text) for pattern in _SENSITIVE_SUBJECT_PATTERNS):
            return ["包含链接、路径或疑似密钥等不适宜公开的内容"]
        if "```" in text:
            return ["包含代码块"]
        return []

    # ---- 发布 ----

    def publish(self, *, post_id: int | None = None) -> dict[str, Any]:
        now = time.time()
        if post_id is None and now < self._publish_after:
            return {"ok": True, "skipped": "发布冷却中"}
        draft = self.store.devlog_draft() if post_id is None else self._post_by_id(post_id)
        if draft is None or str(draft.get("status") or "") != "draft":
            return {"ok": True, "skipped": "没有可发布的草稿"}
        settings = self.store.settings()
        cap = max(0, min(10, int(settings.get("devlog_daily_cap", 2) or 0)))
        zone = resolve_timezone(settings.get("timezone"))
        midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if cap and self.store.devlog_count_published_since(midnight) >= cap:
            return {"ok": True, "skipped": "已达每日开发手记上限"}
        resolved_id = int(draft["id"])
        content = str(draft.get("content") or "").strip()
        if not content:
            return {"ok": True, "skipped": "草稿内容为空"}
        if not self.store.devlog_begin_publish(resolved_id):
            return {"ok": True, "skipped": "草稿已被其他进程发布"}
        try:
            result = self.admin_factory().post(content, int(settings.get("visibility", 1)))
            tid = str((result or {}).get("tid") or "")
            self.store.devlog_finish_publish(resolved_id, tid)
            self.store.log("devlog_post", tid=tid, content=content)
            self._publish_after = 0.0
            return {"ok": True, "post_id": resolved_id, "tid": tid}
        except Exception as exc:
            message = str(exc)
            uncertain = any(marker in message.casefold() for marker in _UNCERTAIN_SEND_MARKERS)
            # 超时属于“可能已发出”的不确定态：放弃自动重发（对齐现有 uncertain
            # 槽位语义）；确定失败则保留提交、退避后用新草稿重试。
            self.store.devlog_fail_publish(resolved_id, message, consume_commits=uncertain)
            self.store.log("devlog_post", content=content, status="failed", error=message)
            if not uncertain:
                self._publish_after = time.time() + DEVLOG_PUBLISH_RETRY_SECONDS
            return {"ok": False, "post_id": resolved_id, "error": message, "uncertain": uncertain}

    def _post_by_id(self, post_id: int) -> dict[str, Any] | None:
        for item in self.store.devlog_posts(100):
            if int(item.get("id") or 0) == int(post_id):
                return item
        return None

    def delete_draft(self, post_id: int) -> dict[str, Any]:
        draft = self.store.devlog_draft()
        if draft is not None and int(draft["id"]) == int(post_id):
            # 手记草稿被删除后，其快照提交回到待写状态，下一轮重新聚合。
            try:
                shas = [str(item) for item in json.loads(str(draft.get("commit_shas") or "[]"))]
            except json.JSONDecodeError:
                shas = []
            self.store.devlog_delete_post(int(post_id))
            return {"ok": True, "released": len(shas)}
        self.store.devlog_delete_post(int(post_id))
        return {"ok": True, "released": 0}

    # ---- 周期与状态 ----

    def run_cycle(self, *, force: bool = False) -> dict[str, Any]:
        """30 秒循环调用：扫描（节流）→ 生成（去重）→ 自动发布（带上限）。"""
        settings = self.store.settings()
        if not settings.get("devlog_enabled"):
            return {"ok": True, "skipped": "开发日志未启用"}
        result: dict[str, Any] = {"ok": True}
        try:
            result["scan"] = self.scan(force=force)
        except DevLogUnavailable as exc:
            self._scan_after = time.time() + DEVLOG_SCAN_INTERVAL_SECONDS
            self.store.log("devlog_scan", status="failed", error=str(exc))
            result["scan"] = {"ok": False, "error": str(exc)}
        try:
            result["generate"] = self.generate()
        except Exception as exc:
            self.store.log("devlog_generate", status="failed", error=str(exc))
            result["generate"] = {"ok": False, "error": str(exc)}
        if bool(settings.get("devlog_auto_publish", DEFAULT_SETTINGS["devlog_auto_publish"])):
            try:
                result["publish"] = self.publish()
            except Exception as exc:
                self.store.log("devlog_post", status="failed", error=str(exc))
                result["publish"] = {"ok": False, "error": str(exc)}
        return result

    def status(self) -> dict[str, Any]:
        settings = self.store.settings()
        zone = resolve_timezone(settings.get("timezone"))
        midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        pending = self.store.devlog_pending_commits()
        return {
            "ok": True,
            "settings": {
                "devlog_enabled": bool(settings.get("devlog_enabled")),
                "devlog_auto_publish": bool(settings.get("devlog_auto_publish")),
                "devlog_visibility_types": settings.get("devlog_visibility_types"),
                "devlog_daily_cap": int(settings.get("devlog_daily_cap", 2) or 0),
                "devlog_last_scan_at": float(settings.get("devlog_last_scan_at") or 0),
                "post_model_profile_id": str(settings.get("post_model_profile_id") or ""),
            },
            "pending": pending[:30],
            "pending_count": len(pending),
            "draft": self.store.devlog_draft(),
            "posts": self.store.devlog_posts(20),
            "published_today": self.store.devlog_count_published_since(midnight),
        }
