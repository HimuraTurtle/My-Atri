"""QQ 空间业务层：自然语言工具、主人口令与自动评论共用的唯一入口。

模块按 papers 范式组织（单例服务 + 无状态 HTTP + dict 返回 + 中文错误）：
- ``get_qzone_service(config)`` 返回进程级单例；未开启时返回 None；
- Cookie 提供器与文本生成器由 OneBot 服务端在启动时注入——服务本体不
  持有 WebSocket，也不独立登录，完全复用 NapCat 会话；
- 「发条说说」仅主人口令触发，「更新日志」走 devlog 完整闭环；自动评论
  由每日锚点定时任务与聊天随机触发共用同一实现，动作日志保证幂等。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .api import QzoneAPI
from .devlog import DevLog
from .rules import (
    build_comment_prompt,
    build_post_prompt,
    comment_quality_issues,
    post_quality_issues,
    resolve_timezone,
)
from .session import QzoneSession
from .store import QzoneStore

LOGGER = logging.getLogger("atri.qzone")

CookieProvider = Callable[[str], Awaitable[dict[str, Any]]]
TextGenerator = Callable[[str], Awaitable[dict[str, Any]]]

AUTO_COMMENT_MAX_FEEDS = 20
AUTO_COMMENT_FEED_MAX_AGE_DAYS = 3.0
_GENERATION_RETRY_LIMIT = 2


class QzoneService:
    def __init__(self, config: Any, store: QzoneStore | None = None) -> None:
        self._config = config
        self.store = store if store is not None else QzoneStore()
        self.devlog = DevLog(
            self.store,
            config,
            None,
            None,
        )
        self._session: QzoneSession | None = None
        self._api: QzoneAPI | None = None

    # ---- 注入（由 onebot 服务端在启动时调用）----

    def bind_cookie_provider(self, provider: CookieProvider) -> None:
        self._session = QzoneSession(
            provider,
            ttl_seconds=max(
                0.0, float(getattr(self._config, "qzone_session_ttl_seconds", 600.0) or 600.0)
            ),
        )
        self._api = None

    def bind_generators(
        self,
        post_generator: TextGenerator | None,
        comment_generator: TextGenerator | None,
    ) -> None:
        """devlog 与说说生成共用 post 生成器；评论生成器供自动评论使用。"""
        self.devlog.post_generator = post_generator
        self.devlog.publisher = self._devlog_publisher
        self._post_generator = post_generator
        self._comment_generator = comment_generator

    async def _devlog_publisher(self, content: str) -> dict[str, Any]:
        """devlog 专用发布器：接口失败要抛异常进入失败/不确定态处理。"""
        result = await self.publish_post(content, action="devlog_post")
        if not result.get("ok"):
            raise RuntimeError(str(result.get("message") or "发布失败"))
        return result

    # ---- HTTP 会话 ----

    @property
    def api(self) -> QzoneAPI:
        if self._session is None:
            raise RuntimeError(
                "QQ 空间 Cookie 提供器尚未接入（NapCat 未连接或服务未初始化）"
            )
        if self._api is None:
            self._api = QzoneAPI(
                self._session,
                timeout_seconds=max(
                    5.0,
                    float(getattr(self._config, "qzone_timeout_seconds", 20.0) or 20.0),
                ),
            )
        return self._api

    # ---- 只读工具（自然语言 qzone_* 工具复用）----

    async def view_recent_feeds(self, num: int = 10) -> dict[str, Any]:
        """好友动态流；供聊天工具与自动评论共用。"""
        result = await self.api.get_recent_feeds(num=AUTO_COMMENT_MAX_FEEDS)
        if not result.get("ok"):
            return result
        feeds = [
            self._trim_feed(item)
            for item in (result.get("data", {}).get("feeds") or [])
        ]
        return {"ok": True, "message": "", "data": {"feeds": feeds[: max(1, int(num))]}}

    async def view_user_feeds(self, qq: int | str, num: int = 10) -> dict[str, Any]:
        """指定 QQ 的说说列表；qq 为空时读机器人自己的空间。"""
        uin = str(qq or "").strip()
        if not uin.isdigit():
            return {"ok": False, "message": "QQ 号必须是纯数字", "data": {}}
        result = await self.api.get_feeds(int(uin), num=max(1, min(20, int(num))))
        if not result.get("ok"):
            return result
        feeds = [self._trim_feed(item) for item in result.get("data", {}).get("feeds") or []]
        return {"ok": True, "message": "", "data": {"feeds": feeds}}

    @staticmethod
    def _trim_feed(feed: dict[str, Any]) -> dict[str, Any]:
        content = " ".join(str(feed.get("content") or "").split())
        created = int(feed.get("create_time") or 0)
        return {
            "uin": int(feed.get("uin") or 0),
            "name": str(feed.get("name") or ""),
            "tid": str(feed.get("tid") or ""),
            "content": content[:200],
            "create_time": created,
            "time_text": _time_text(created),
        }

    # ---- 点赞 / 评论 / 发布 ----

    async def like_feed(self, uin: int | str, tid: str) -> dict[str, Any]:
        uin_text = str(uin or "").strip()
        tid = str(tid or "").strip()
        if not uin_text.isdigit() or not tid:
            return {"ok": False, "message": "点赞需要有效的 QQ 号与动态 ID", "data": {}}
        if self.store.already_logged("like", tid):
            return {"ok": True, "message": "这条动态已经点过赞了", "data": {"tid": tid}}
        result = await self.api.like(int(uin_text), tid)
        if result.get("ok"):
            self.store.log("like", tid=tid, status="success")
        else:
            self.store.log("like", tid=tid, status="failed", error=str(result.get("message") or ""))
        return result

    async def comment_feed(self, uin: int | str, tid: str, content: str) -> dict[str, Any]:
        uin_text = str(uin or "").strip()
        tid = str(tid or "").strip()
        text = str(content or "").strip()
        if not uin_text.isdigit() or not tid:
            return {"ok": False, "message": "评论需要有效的 QQ 号与动态 ID", "data": {}}
        if not text or len(text) > 200:
            return {"ok": False, "message": "评论内容为空或超过 200 字", "data": {}}
        if self.store.already_logged("comment", tid):
            return {"ok": True, "message": "这条动态已经评论过了", "data": {"tid": tid}}
        result = await self.api.comment(int(uin_text), tid, text)
        if result.get("ok"):
            self.store.log("comment", tid=tid, content=text, status="success")
        else:
            self.store.log(
                "comment", tid=tid, content=text, status="failed",
                error=str(result.get("message") or ""),
            )
        return result

    async def publish_post(
        self,
        text: str,
        *,
        visibility: int | None = None,
        action: str = "post",
    ) -> dict[str, Any]:
        """发布说说并写入动作日志；「发条说说」与 devlog 发布共用。"""
        content = str(text or "").strip()
        if not content or len(content) > 1000:
            return {"ok": False, "message": "说说内容为空或超过 1000 字", "data": {}}
        level = int(
            visibility
            if visibility is not None
            else getattr(self._config, "qzone_visibility", 1)
        )
        result = await self.api.publish(content, visibility=level)
        if result.get("ok"):
            self.store.log(action, tid=str(result.get("data", {}).get("tid") or ""), content=content, status="success")
        else:
            self.store.log(action, content=content, status="failed", error=str(result.get("message") or ""))
        return result

    # ---- 「发条说说」（主人口令）----

    async def owner_publish_post(self, text: str | None = None) -> dict[str, Any]:
        """主人口令发布：给了正文直接发；没给就按当日主题生成后发布。"""
        explicit = str(text or "").strip()
        if explicit:
            return await self.publish_post(explicit)
        generator = getattr(self, "_post_generator", None)
        if generator is None:
            return {"ok": False, "message": "说说生成器尚未就绪，稍后再试", "data": {}}
        settings = dict(self.store.settings())
        settings["recent_contents"] = self.store.recent_post_contents(5)
        settings["timezone"] = getattr(self._config, "qzone_timezone", "Asia/Shanghai")
        recent = list(settings["recent_contents"])
        prompt = build_post_prompt(settings, time.time())
        last_error = ""
        for attempt in range(_GENERATION_RETRY_LIMIT):
            try:
                generated = await generator(prompt)
            except Exception as exc:
                LOGGER.warning("说说生成失败（第 %d 次）：%s", attempt + 1, exc)
                last_error = str(exc)
                continue
            content = str((generated or {}).get("content") or "").strip()
            issues = post_quality_issues(content, recent)
            if not issues:
                return await self.publish_post(content)
            last_error = "；".join(issues)
            LOGGER.info("说说生成稿未通过质量闸（第 %d 次）：%s", attempt + 1, last_error)
        return {"ok": False, "message": f"生成的说说没过质量检查：{last_error}", "data": {}}

    # ---- 自动评论（每日锚点 + 聊天随机触发共用）----

    async def auto_comment_run(
        self,
        *,
        max_count: int | None = None,
        source: str = "cron",
    ) -> dict[str, Any]:
        """拉好友动态 → 过滤自己与已评论 → 生成 → 评论；返回执行摘要。"""
        if not bool(getattr(self._config, "qzone_auto_comment_enabled", True)):
            return {"ok": True, "skipped": "自动评论未启用", "commented": []}
        limit = int(
            max_count
            if max_count is not None
            else getattr(self._config, "qzone_auto_comment_max", 2)
        )
        limit = max(1, min(5, limit))
        generator = getattr(self, "_comment_generator", None)
        if generator is None:
            return {"ok": False, "skipped": "评论生成器尚未就绪", "commented": []}
        feeds_result = await self.api.get_recent_feeds(num=AUTO_COMMENT_MAX_FEEDS)
        if not feeds_result.get("ok"):
            return {
                "ok": False,
                "skipped": str(feeds_result.get("message") or "动态流读取失败"),
                "commented": [],
            }
        own_uin = await self._session.get_uin() if self._session else 0
        stale_edge = time.time() - AUTO_COMMENT_FEED_MAX_AGE_DAYS * 86400
        candidates: list[dict[str, Any]] = []
        for feed in feeds_result.get("data", {}).get("feeds") or []:
            uin = int(feed.get("uin") or 0)
            tid = str(feed.get("tid") or "")
            content = str(feed.get("content") or "").strip()
            if not tid or not content or uin == own_uin:
                continue
            if int(feed.get("create_time") or 0) < stale_edge:
                continue
            if self.store.already_logged("comment", tid):
                continue
            candidates.append(feed)
            if len(candidates) >= limit:
                break
        commented: list[dict[str, Any]] = []
        skipped: list[str] = []
        for feed in candidates:
            try:
                generated = await generator(build_comment_prompt(feed))
            except Exception as exc:
                skipped.append(f"评论生成失败：{exc}")
                continue
            content = str((generated or {}).get("content") or "").strip()
            if comment_quality_issues(content):
                skipped.append("生成的评论未通过质量检查")
                continue
            result = await self.comment_feed(feed.get("uin"), feed.get("tid"), content)
            if result.get("ok"):
                commented.append(
                    {
                        "uin": int(feed.get("uin") or 0),
                        "name": str(feed.get("name") or ""),
                        "tid": str(feed.get("tid") or ""),
                        "content": content,
                    }
                )
            else:
                skipped.append(str(result.get("message") or "评论失败"))
        summary = {
            "ok": True,
            "source": source,
            "commented": commented,
            "commented_count": len(commented),
            "skipped": skipped,
        }
        LOGGER.info(
            "自动评论完成：source=%s commented=%d skipped=%d",
            source, len(commented), len(skipped),
        )
        return summary

    def daily_comment_count(self) -> int:
        zone = resolve_timezone(getattr(self._config, "qzone_timezone", "Asia/Shanghai"))
        from datetime import datetime as _datetime

        midnight = _datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.store.count_success_since("comment", midnight)

    # ---- devlog 闭环与状态 ----

    async def devlog_cycle(self, *, force: bool = True, auto_publish: bool = True) -> dict[str, Any]:
        return await self.devlog.cycle(force=force, auto_publish=auto_publish)

    def status(self) -> dict[str, Any]:
        """WebUI/状态接口快照：配置、水位、草稿与最近动作。"""
        settings = self.store.settings()
        return {
            "ok": True,
            "settings": {
                "enabled": bool(getattr(self._config, "qzone_enabled", False)),
                "auto_comment_enabled": bool(getattr(self._config, "qzone_auto_comment_enabled", True)),
                "auto_comment_time": str(getattr(self._config, "qzone_auto_comment_time", "23:30")),
                "auto_comment_next_run": float(settings.get("auto_comment_next_run") or 0),
                "random_probability": float(
                    getattr(self._config, "qzone_random_comment_probability", 0.001)
                ),
                "visibility": int(getattr(self._config, "qzone_visibility", 1)),
                "post_topics": settings.get("post_topics") or [],
            },
            "devlog": self.devlog.status(),
            "comments_today": self.daily_comment_count(),
            "logs": [
                {
                    "action": str(item.get("action") or ""),
                    "status": str(item.get("status") or ""),
                    "error": str(item.get("error") or ""),
                    "content": str(item.get("content") or "")[:160],
                    "created_at": float(item.get("created_at") or 0),
                }
                for item in self.store.logs(20)
            ],
        }


def _time_text(ts: float) -> str:
    if not ts:
        return "时间未知"
    delta = max(0, int(time.time() - float(ts)))
    if delta < 3600:
        return f"{max(1, delta // 60)} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"
