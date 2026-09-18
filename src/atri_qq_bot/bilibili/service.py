"""B站助手服务层：命令解析、总结编排、冷却与并发去重。

定位同漫画助手：确定性命令入口 + 可选的纯链接自动总结。命令需在
聊天模型之前拦截；自动总结在回复门控之前触发（分享者不会 @ 机器人）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..message_plan.schema import OutgoingMessage
from .client import BilibiliApiClient, BilibiliError, VideoInfo
from .parsing import (
    extract_aid,
    extract_bvid,
    extract_generic_url,
    extract_long_url,
    extract_page_param,
    extract_short_url,
    is_bilibili_domain,
    is_short_bili_url,
    message_is_pure_link,
    url_from_event,
)
from .subtitles import acquire_transcript
from .summarize import BilibiliSummarizer, format_time

LOGGER = logging.getLogger("atri.bilibili")

SUMMARY_PREFIXES: tuple[str, ...] = ("B站总结", "哔哩总结", "bv总结", "BV总结", "视频总结")
PARSE_PREFIXES: tuple[str, ...] = ("B站解析", "bv解析", "视频解析")
HELP_PREFIXES: tuple[str, ...] = ("B站帮助", "bv帮助")

USAGE_HELP = """【B站视频助手】
· B站总结 <链接/BV号> — 生成视频 AI 总结
· B站解析 <链接/BV号> — 查看视频信息
支持完整链接、b23.tv 短链和纯 BV 号。
无字幕视频会自动下载音频转写，速度较慢（约 1-3 分钟）。"""

_AS_FALLBACK_NOTE = "仅支持 B站 普通视频链接（番剧/课程/直播页暂不支持）"


@dataclass(slots=True)
class BilibiliCommandResult:
    messages: list[OutgoingMessage] = field(default_factory=list)
    record_reply_text: str = ""


def _fmt_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


class BilibiliService:
    """B站视频解析与 AI 总结（移植自 AstrBot biliVideo 插件核心链路）。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.client = BilibiliApiClient(cookie=str(getattr(config, "bilibili_cookie", "") or ""))
        self.summarizer = BilibiliSummarizer(config)
        audio_dir = str(getattr(config, "bilibili_audio_dir", "") or "")
        self._audio_dir = Path(audio_dir) if audio_dir else Path("data/bilibili/audio")
        self._cooldowns: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task[str]] = {}

    async def close(self) -> None:
        await self.client.close()

    # ── 冷却 ──────────────────────────────────────────────────────

    def cooldown_remaining(self, user_id: Any, conversation_id: str) -> int:
        """命令冷却剩余秒数（供 OneBot 层决定是否先发「生成中」提示）。"""
        return self._cooldown_remaining(f"cmd:{user_id or conversation_id}")

    def _cooldown_remaining(self, key: str) -> int:
        punched_at = self._cooldowns.get(key)
        if not punched_at:
            return 0
        limit = float(getattr(self.config, "bilibili_cooldown_seconds", 20))
        remaining = limit - (time.time() - punched_at)
        return max(0, int(remaining + 0.999))

    def _punch_cooldown(self, key: str) -> None:
        self._cooldowns[key] = time.time()
        if len(self._cooldowns) > 256:
            oldest = min(self._cooldowns, key=lambda k: self._cooldowns[k])
            self._cooldowns.pop(oldest, None)

    # ── 命令入口 ──────────────────────────────────────────────────

    @staticmethod
    def is_command(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        if any(stripped.startswith(prefix) for prefix in HELP_PREFIXES):
            return True
        for prefixes in (SUMMARY_PREFIXES, PARSE_PREFIXES):
            if any(stripped.startswith(prefix) for prefix in prefixes):
                return BilibiliService._looks_like_target(stripped)
        return False

    @staticmethod
    def _looks_like_target(text: str) -> bool:
        return bool(
            extract_bvid(text)
            or extract_long_url(text)
            or extract_short_url(text)
            or extract_aid(text)
            or (
                (url := extract_generic_url(text)) is not None
                and is_bilibili_domain(url)
            )
        )

    async def handle_command(
        self, text: str, conversation_id: str, user_id: Any = None
    ) -> BilibiliCommandResult:
        stripped = (text or "").strip()
        if any(stripped.startswith(prefix) for prefix in HELP_PREFIXES):
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", USAGE_HELP)],
                record_reply_text=USAGE_HELP,
            )

        remaining = self._cooldown_remaining(f"cmd:{user_id or conversation_id}")
        if remaining > 0:
            notice = f"⏳ 操作太频繁，请等 {remaining} 秒后再试。"
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", notice)], record_reply_text=notice
            )

        is_parse = any(stripped.startswith(prefix) for prefix in PARSE_PREFIXES)
        arg = stripped.split(maxsplit=1)[1].strip() if len(stripped.split(maxsplit=1)) > 1 else ""
        if not arg:
            notice = "❌ 缺少视频链接\n用法: B站总结 <B站视频链接或BV号>"
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", notice)], record_reply_text=notice
            )

        target, page, error = await self._resolve_target(arg)
        if error or not target:
            notice = f"❌ {error or '无法识别视频'}"
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", notice)], record_reply_text=notice
            )

        self._punch_cooldown(f"cmd:{user_id or conversation_id}")
        try:
            info = await self.client.get_video_info(target)
        except BilibiliError as exc:
            notice = f"❌ 视频信息获取失败：{exc}"
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", notice)], record_reply_text=notice
            )

        if is_parse:
            body = self._format_info(info)
            return BilibiliCommandResult(
                messages=[OutgoingMessage("text", body)], record_reply_text=body
            )

        return await self._summary_result(info, page, conversation_id)

    # ── 自动总结（纯链接/分享卡片） ───────────────────────────────

    async def auto_summarize(
        self, event: dict[str, Any], plain_text: str, conversation_id: str
    ) -> BilibiliCommandResult | None:
        if not bool(getattr(self.config, "bilibili_auto_summary", False)):
            return None
        if (plain_text or "").strip().startswith(tuple(HELP_PREFIXES + SUMMARY_PREFIXES + PARSE_PREFIXES)):
            return None
        if not message_is_pure_link(plain_text, event):
            return None
        url = url_from_event(event)
        if not url:
            return None
        target, page, error = await self._resolve_target(url)
        if not target or error:
            return None
        remaining = self._cooldown_remaining(f"auto:{conversation_id}")
        if remaining > 0:
            LOGGER.info("自动总结冷却中：conversation=%s 剩余 %ss", conversation_id, remaining)
            return None
        self._punch_cooldown(f"auto:{conversation_id}")
        try:
            info = await self.client.get_video_info(target)
        except BilibiliError as exc:
            LOGGER.warning("自动总结获取视频信息失败：%s", exc)
            return None
        return await self._summary_result(info, page, conversation_id)

    # ── 总结编排 ──────────────────────────────────────────────────

    async def _summary_result(
        self, info: VideoInfo, page: int, conversation_id: str
    ) -> BilibiliCommandResult:
        cached = self.summarizer.cached_summary(info.bvid)
        if cached is None:
            task_key = f"{info.bvid}:p{page}"
            task = self._inflight.get(task_key)
            if task is None:
                task = asyncio.ensure_future(self._generate_summary(info, page))
                self._inflight[task_key] = task
                task.add_done_callback(lambda _: self._inflight.pop(task_key, None))
            try:
                await task
            except BilibiliError as exc:
                notice = f"❌ {exc}"
                return BilibiliCommandResult(
                    messages=[OutgoingMessage("text", notice)], record_reply_text=notice
                )
            except Exception as exc:
                LOGGER.exception("B站总结生成失败：%s", info.bvid)
                notice = f"❌ 总结生成失败：{exc}"
                return BilibiliCommandResult(
                    messages=[OutgoingMessage("text", notice)], record_reply_text=notice
                )
            cached = self.summarizer.cached_summary(info.bvid) or ""
        body = self._format_summary_reply(info, cached)
        return BilibiliCommandResult(
            messages=[OutgoingMessage("text", body)], record_reply_text=body
        )

    async def _generate_summary(self, info: VideoInfo, page: int) -> str:
        cid = info.cid
        if page > 1 and info.pages > 1:
            try:
                page_cid = await self.client.get_page_cid(info.bvid, page)
                if page_cid:
                    cid = page_cid
            except BilibiliError:
                pass
        asr_enabled = bool(getattr(self.config, "bilibili_asr_enabled", True))
        audio_path: Path | None = None
        try:
            transcript, audio_path = await acquire_transcript(
                self.client,
                info.bvid,
                cid,
                audio_dir=self._audio_dir,
                asr_enabled=asr_enabled,
            )
            if transcript is None:
                raise BilibiliError(
                    "该视频没有可用字幕，音频转写也未成功（或已被关闭）",
                )
            tags = ", ".join(
                str(t) for t in (info.raw.get("tname") and [info.raw["tname"]] or [])
            )
            summary = await self.summarizer.summarize(
                video_info=info, transcript=transcript, tags=tags
            )
            self.summarizer.remember_summary(info.bvid, summary)
            return summary
        finally:
            if audio_path is not None:
                try:
                    audio_path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("音频清理失败：%s", audio_path)

    # ── 目标解析 ──────────────────────────────────────────────────

    async def _resolve_target(self, arg: str) -> tuple[str, int, str]:
        """把链接/BV/av 号解析成 (bvid或aid, 分P, 错误信息)。"""
        arg = arg.strip()
        bvid = extract_bvid(arg)
        if bvid:
            return bvid, extract_page_param(arg), ""
        url = extract_long_url(arg) or extract_short_url(arg) or extract_generic_url(arg)
        if url:
            if not is_bilibili_domain(url):
                return "", 0, _AS_FALLBACK_NOTE
            bvid = extract_bvid(url)
            page = extract_page_param(url)
            if bvid:
                return bvid, page, ""
            if is_short_bili_url(url):
                resolved = await self.client.follow_redirect(url)
                if resolved:
                    bvid = extract_bvid(resolved)
                    if bvid:
                        return bvid, extract_page_param(resolved), ""
                return "", 0, "短链解析失败，请直接发送 BV 号或完整链接"
            aid = extract_aid(url)
            if aid:
                return aid, 1, ""
            return "", 0, _AS_FALLBACK_NOTE
        aid = extract_aid(arg)
        if aid:
            return aid, 1, ""
        return "", 0, "请提供 B站视频链接或 BV 号"

    # ── 展示格式化 ────────────────────────────────────────────────

    def _format_summary_reply(self, info: VideoInfo, summary: str) -> str:
        header_lines = [
            f"📺 《{info.title}》",
            (
                f"UP：{info.owner_name} | 时长 {format_time(info.duration)} | "
                f"播放 {_fmt_count(info.view)}"
            ),
            info.url,
            "─────",
        ]
        return "\n".join(header_lines) + "\n" + summary

    def _format_info(self, info: VideoInfo) -> str:
        pubdate = (
            datetime.fromtimestamp(info.pubdate).strftime("%Y-%m-%d") if info.pubdate else "未知"
        )
        lines = [
            f"📺 《{info.title}》",
            f"UP：{info.owner_name}（UID {info.owner_mid}）",
            (
                f"时长 {format_time(info.duration)} | 分P {info.pages} | 发布 {pubdate}"
            ),
            f"播放 {_fmt_count(info.view)} | 弹幕 {_fmt_count(info.danmaku)} | 点赞 {_fmt_count(info.like)}",
        ]
        desc = (info.desc or "").strip()
        if desc:
            desc = desc.replace("\r", "")
            if len(desc) > 120:
                desc = desc[:120] + "…"
            lines.append(f"简介：{desc}")
        lines.append(info.url)
        return "\n".join(lines)


_SERVICE: BilibiliService | None = None


def get_bilibili_service(config: Any) -> BilibiliService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = BilibiliService(config)
    return _SERVICE
