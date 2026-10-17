"""Suwayomi 漫画助手（移植自 astrbot_plugin_suwayomi_server）。

MangaService 是唯一门面：OneBot 层用它处理「漫画 xxx」命令和更新推送，
人格引擎用它执行 suwayomi_* LLM 工具。订阅与缓存状态持久化在
``data/manga/store.json``，章节图片临时目录发送后延迟清理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bilibili.parsing import strip_address_prefix
from ..message_plan import OutgoingMessage
from .client import SuwayomiClient, SuwayomiError
from .downloader import fetch_pages_local, schedule_cleanup
from .models import Chapter, Manga, Source
from .packer import (
    build_chapter_output_path,
    normalize_pack_format,
    pack_images,
    parse_download_args,
)
from .service import (
    fmt_chapter_display,
    fmt_chapter_label,
    fmt_chapter_num,
    format_manga_status,
    match_source_hint,
    normalize_zh,
    resolve_chapter,
    select_search_sources,
)
from .store import MangaStore

LOGGER = logging.getLogger("atri.manga")

_COMMAND_PREFIXES = ("漫画", "/漫画")
_SEARCH_CACHE_TTL = 600.0
_UPDATE_CONCURRENCY = 5
_MAX_BATCH_SUBSCRIBE = 20

# 章节名含这些关键词时视为番外/资料类，数字同号时优先让位于正篇。
_NON_STORY_MARKERS = (
    "番外", "外传", "外傳", "特别", "特別", "設定", "设定", "彩页", "彩頁",
    "单行本", "短篇", "附录", "附錄", "画集", "畫集", "情报", "情報",
    "目录", "目錄", "公告", "感言", "贺图", "賀圖", "插图", "插圖",
    "extra", "omake", "special", "bonus", "survey", "vote", "notice",
)


def _is_non_story_chapter(ch: Chapter) -> bool:
    name = (ch.name or "").strip().lower()
    return any(marker in name for marker in _NON_STORY_MARKERS)


@dataclass
class MangaCommandResult:
    messages: list[OutgoingMessage] = field(default_factory=list)
    cleanup_dirs: list[Path] = field(default_factory=list)
    cleanup_delay: int = 60
    notifications: list["MangaUpdateNotification"] = field(default_factory=list)


@dataclass
class MangaUpdateNotification:
    """一部漫画对一个会话的更新通知；push_chapters 仅在开启自动推送时有内容。"""

    conversation_id: str
    items: list[dict[str, Any]] = field(default_factory=list)
    push_chapters: list[tuple[int, str, Chapter]] = field(default_factory=list)


@dataclass
class MangaDeliveryRequest:
    """LLM 工具受理的章节发送请求；由 OneBot 层消费并真正发送。"""

    manga_id: int
    chapter_id: int
    fmt: str = "pdf"


class MangaService:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.client = SuwayomiClient(
            server_url=str(getattr(config, "manga_server_url", "http://127.0.0.1:4567")),
            auth_mode=str(getattr(config, "manga_auth_mode", "none")),
            username=str(getattr(config, "manga_username", "") or ""),
            password=str(getattr(config, "manga_password", "") or ""),
        )
        store_path = getattr(config, "manga_store_path", None)
        self.store = MangaStore(Path(store_path) if store_path else Path("data/manga/store.json"))
        self._search_caches: dict[str, tuple[float, dict[str, Manga]]] = {}
        self._exposed_chapters: dict[str, dict[int, set[int]]] = {}
        self._update_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.close()

    # ── 小工具 ─────────────────────────────────────────────────────

    @staticmethod
    def _result(messages: list[OutgoingMessage], **kwargs: Any) -> MangaCommandResult:
        return MangaCommandResult(messages=messages, **kwargs)

    def _cached_search(self, conversation_id: str) -> dict[str, Manga]:
        entry = self._search_caches.get(conversation_id)
        if entry and time.time() - entry[0] <= _SEARCH_CACHE_TTL:
            return entry[1]
        return {}

    def _remember_search(self, conversation_id: str, cache: dict[str, Manga]) -> None:
        self._search_caches[conversation_id] = (time.time(), cache)
        if len(self._search_caches) > 64:
            oldest = min(self._search_caches, key=lambda key: self._search_caches[key][0])
            self._search_caches.pop(oldest, None)

    def remember_exposed_chapters(self, conversation_id: str, manga_id: int, chapter_ids: set[int]) -> None:
        if not chapter_ids:
            return
        per_manga = self._exposed_chapters.setdefault(conversation_id, {})
        exposed = per_manga.setdefault(manga_id, set())
        exposed |= set(chapter_ids)
        if len(exposed) > 400:
            exposed.clear()
            exposed |= set(chapter_ids)

    def was_chapter_exposed(self, conversation_id: str, manga_id: int, chapter_id: int) -> bool:
        return chapter_id in self._exposed_chapters.get(conversation_id, {}).get(manga_id, set())

    # ── 章节缓存 ───────────────────────────────────────────────────

    async def get_or_fetch_chapters(self, manga_id: int, force: bool = False) -> list[Chapter]:
        cache_hours = int(getattr(self.config, "manga_chapter_cache_hours", 6))
        if cache_hours < -1:
            cache_hours = 0

        if cache_hours == -1:
            try:
                chapters = await self.client.fetch_chapters(manga_id)
                if chapters:
                    self.store.set_chapter_timestamp(manga_id)
                return chapters
            except SuwayomiError:
                return await self.client.get_chapters(manga_id)

        should_fetch = force
        if not should_fetch and cache_hours > 0:
            last_ts = self.store.get_chapter_timestamp(manga_id)
            if last_ts == 0 or (time.time() - last_ts) > cache_hours * 3600:
                should_fetch = True

        if not should_fetch:
            chapters = await self.client.get_chapters(manga_id)
            if chapters:
                return chapters
            should_fetch = True

        chapters = await self.client.fetch_chapters(manga_id)
        if chapters:
            self.store.set_chapter_timestamp(manga_id)
        return chapters

    async def resolve_manga(
        self, name_or_id: str, conversation_id: str, cmd: str
    ) -> tuple[Manga | None, str | None]:
        try:
            manga_id = int(name_or_id)
            manga = await self.client.get_manga(manga_id)
            return manga, None
        except (ValueError, SuwayomiError):
            pass

        norm_input = normalize_zh(name_or_id)
        for sub in self.store.get_subscriptions(conversation_id):
            if norm_input in normalize_zh(sub["title"]):
                try:
                    return await self.client.get_manga(sub["manga_id"]), None
                except SuwayomiError:
                    continue

        try:
            mangas = await self.client.search_manga_by_title(name_or_id)
        except Exception as exc:
            LOGGER.warning("resolve_manga 查找失败: %s", exc)
            return None, "查找漫画失败。"
        if len(mangas) == 0:
            return None, "未找到该漫画。"
        if len(mangas) == 1:
            return mangas[0], None

        src_map: dict[str, str] = {}
        try:
            sources = await self.client.get_sources()
            src_map = {str(s.id): s.display_name for s in sources}
        except Exception:
            pass

        lines = [f"找到多个结果，请使用 ID 指定。例如: 漫画 {cmd} {mangas[0].id}"]
        for manga in mangas:
            status = format_manga_status(manga.status)
            src_name = src_map.get(str(manga.source_id), f"源{manga.source_id}")
            lines.append(f"  ID {manga.id}: {manga.title} [{status}] ({src_name})")
        return None, "\n".join(lines)

    async def search_best_match(
        self, name: str, source_filter: Source | None = None
    ) -> tuple[Manga | None, str | None]:
        sources = await self.client.get_sources()
        if not sources:
            return None, "未找到已安装的漫画源"

        if source_filter:
            target_sources = [source_filter]
        else:
            target_sources = select_search_sources(
                sources,
                "",
                getattr(self.config, "manga_default_source_id", 0),
                max_sources=3,
            )

        for src in target_sources:
            try:
                result = await self.client.search_manga(src.id, name)
                if result.mangas:
                    return result.mangas[0], None
            except Exception as exc:
                LOGGER.warning("批量订阅搜索源 %s 失败: %s", src.name, exc)

        return None, "未找到匹配结果"

    # ── 投递准备 ───────────────────────────────────────────────────

    def _delivery_params(self) -> dict[str, Any]:
        return {
            "concurrency": int(getattr(self.config, "manga_download_concurrency", 6)),
            "custom_tmp": str(getattr(self.config, "manga_temp_dir", "") or "").strip(),
            "retries": int(getattr(self.config, "manga_download_retries", 3)),
            "headers": self.client.auth_headers,
        }

    async def prepare_chapter_images(
        self, chapter_id: int
    ) -> tuple[list[str], Path | None]:
        """下载章节页面，返回 (本地图片路径, 临时目录)。"""
        max_pages = int(getattr(self.config, "manga_max_pages", 20))
        params = self._delivery_params()
        total_pages, _page_urls, local_paths, tmp_dir = await fetch_pages_local(
            self.client,
            chapter_id,
            max_pages=max_pages,
            concurrency=params["concurrency"],
            custom_tmp=params["custom_tmp"],
            retries=params["retries"],
            headers=params["headers"],
        )
        valid = [p for p in local_paths if p]
        if not valid:
            schedule_cleanup(tmp_dir, delay=10)
            return [], None
        return valid, tmp_dir

    async def prepare_chapter_file(
        self, manga: Manga, chapter: Chapter, fmt: str
    ) -> tuple[Path | None, Path | None, str]:
        """下载章节页面并打包，返回 (打包文件路径, 临时目录, 实际格式)。"""
        params = self._delivery_params()
        _total, _urls, local_paths, tmp_dir = await fetch_pages_local(
            self.client,
            chapter.id,
            concurrency=params["concurrency"],
            custom_tmp=params["custom_tmp"],
            retries=params["retries"],
            headers=params["headers"],
        )
        valid = [p for p in local_paths if p]
        if not valid:
            schedule_cleanup(tmp_dir, delay=10)
            return None, None, fmt
        if len(valid) < len(local_paths):
            LOGGER.warning(
                "%s 页下载失败，将用已有页面打包 %s", len(local_paths) - len(valid), fmt
            )

        file_ext = normalize_pack_format(fmt)
        output_path = build_chapter_output_path(
            Path(valid[0]).parent, manga.title, fmt_chapter_display(chapter), file_ext
        )
        loop = asyncio.get_running_loop()
        actual_fmt = await loop.run_in_executor(
            None, pack_images, valid, output_path, fmt
        )
        return output_path, tmp_dir, actual_fmt

    # ── 命令入口 ───────────────────────────────────────────────────

    @staticmethod
    def is_command(text: str) -> bool:
        # 群友习惯在命令前带称呼（"atri，漫画 搜索 xxx"、@机器人 漫画 帮助），
        # 先剥离再分词匹配，避免命令漏拦后落入聊天模型（与 B站命令同一容错层）。
        tokens = strip_address_prefix(text).split()
        if len(tokens) < 2:
            return False
        if tokens[0] not in _COMMAND_PREFIXES:
            return False
        return tokens[1] in {
            "搜索", "订阅", "批量订阅", "取消订阅", "我的订阅",
            "章节", "阅读", "下载", "更新", "源", "帮助", "推送",
        }

    async def handle_command(self, text: str, conversation_id: str) -> MangaCommandResult:
        raw = strip_address_prefix(text).strip()
        tokens = raw.split()
        sub = tokens[1]
        handler = {
            "帮助": self._cmd_help,
            "源": self._cmd_sources,
            "搜索": self._cmd_search,
            "订阅": self._cmd_subscribe,
            "批量订阅": self._cmd_batch_subscribe,
            "取消订阅": self._cmd_unsubscribe,
            "我的订阅": self._cmd_my_subscriptions,
            "章节": self._cmd_chapters,
            "阅读": self._cmd_read,
            "下载": self._cmd_download,
            "更新": self._cmd_update,
            "推送": self._cmd_push,
        }[sub]
        try:
            return await handler(raw, conversation_id)
        except SuwayomiError as exc:
            return self._result([OutgoingMessage("text", f"漫画服务暂时不可用：{exc}")])
        except Exception as exc:
            LOGGER.exception("漫画命令处理失败：%s", exc)
            return self._result([OutgoingMessage("text", "漫画命令执行失败，请稍后重试。")])

    async def _cmd_help(self, raw: str, conversation_id: str) -> MangaCommandResult:
        text = (
            "📖 漫画助手\n"
            "🔍 搜索与订阅\n"
            "  漫画 搜索 <关键词> [源名] — 搜索漫画（多词用 + 连接）\n"
            "  漫画 订阅 <编号> — 订阅搜索结果\n"
            "  漫画 批量订阅 <名称1>, <名称2>, ... [源名]\n"
            "  漫画 取消订阅 <ID或名称>\n"
            "  漫画 我的订阅\n"
            "📚 阅读与下载\n"
            "  漫画 章节 <漫画名或ID> — 查看章节列表（--刷新 强制更新）\n"
            "  漫画 阅读 <漫画名或ID> <章节号> — 阅读章节\n"
            "  漫画 下载 <漫画名或ID> <章节号> [zip/pdf/cbz]\n"
            "  重复编号章节用 ID: 指定，如 漫画 阅读 <漫画名> ID:123\n"
            "🔄 更新\n"
            "  漫画 更新 — 手动检查更新并推送（全局）\n"
            "📡 自动推送\n"
            "  漫画 推送 开/关/状态\n"
            "📋 其他\n"
            "  漫画 源 — 查看已安装的漫画源"
        )
        return self._result([OutgoingMessage("text", text)])

    async def _cmd_sources(self, raw: str, conversation_id: str) -> MangaCommandResult:
        sources = await self.client.get_sources()
        if not sources:
            return self._result([OutgoingMessage("text", "未找到已安装的漫画源，请在 Suwayomi WebUI 中安装扩展。")])
        lines = ["📚 已安装的漫画源:"]
        for i, src in enumerate(sources, 1):
            lines.append(f"  [{i}] {src.display_name} ({src.lang})")
        return self._result([OutgoingMessage("text", "\n".join(lines))])

    async def _cmd_search(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            idx = tokens.index("搜索")
            args = " ".join(tokens[idx + 1:]).strip()
        except ValueError:
            args = ""
        words = args.rsplit(" ", 1) if args else []
        if len(words) == 2 and match_source_hint(await self.client.get_sources(), words[1]):
            keyword, source_hint = words[0].strip(), words[1].strip()
        elif len(words) == 2:
            keyword, source_hint = f"{words[0]} {words[1]}".strip(), ""
        else:
            keyword, source_hint = args, ""

        if not keyword:
            return self._result([OutgoingMessage("text", "用法: 漫画 搜索 <关键词> [源名]（多词标题用 + 连接）")])

        sources = await self.client.get_sources()
        if not sources:
            return self._result([OutgoingMessage("text", "未找到已安装的漫画源。")])

        search_query = keyword
        target_sources: list[Source] = []
        if source_hint:
            hinted = match_source_hint(sources, source_hint)
            if hinted:
                target_sources = [hinted]
        if not target_sources:
            target_sources = select_search_sources(
                sources,
                "",
                getattr(self.config, "manga_default_source_id", 0),
                max_sources=5,
            )

        all_results: list[tuple[str, list[Manga]]] = []
        for src in target_sources:
            try:
                result = await self.client.search_manga(src.id, search_query)
                all_results.append((src.display_name, result.mangas))
            except Exception as exc:
                LOGGER.warning("搜索源 %s 失败: %s", src.name, exc)

        if not all_results:
            return self._result([OutgoingMessage("text", "未找到相关漫画，请确认关键词。")])

        lines: list[str] = []
        idx = 1
        cache: dict[str, Manga] = {}
        for source_name, mangas in all_results:
            if not mangas:
                continue
            lines.append(f"\n🔍 搜索结果（源: {source_name}）:")
            for manga in mangas:
                status = format_manga_status(manga.status)
                lines.append(f"  [{idx}] {manga.title} - {status}")
                cache[str(idx)] = manga
                idx += 1

        if idx == 1:
            return self._result([OutgoingMessage("text", "未找到相关漫画，请确认关键词。")])

        lines.append("\n回复「漫画 订阅 <编号>」订阅，如「漫画 订阅 1」")
        self._remember_search(conversation_id, cache)
        return self._result([OutgoingMessage("text", "\n".join(lines))])

    async def _cmd_subscribe(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            index = tokens[tokens.index("订阅") + 1]
        except (ValueError, IndexError):
            index = ""
        cache = self._cached_search(conversation_id)
        manga = cache.get(index)
        if manga is None:
            return self._result([OutgoingMessage("text", "未找到该编号的漫画，请先使用「漫画 搜索」。")])

        await self._subscribe_manga(conversation_id, manga)
        return self._result([OutgoingMessage("text", f"✅ 已订阅「{manga.title}」，有新章节时会推送。")])

    async def _subscribe_manga(self, conversation_id: str, manga: Manga) -> None:
        self.store.subscribe(manga.id, manga.title, manga.source_id, conversation_id)
        LOGGER.info("订阅「%s」(ID:%s) conversation=%s", manga.title, manga.id, conversation_id)
        try:
            chapters = await self.get_or_fetch_chapters(manga.id)
            if chapters:
                self.store.update_latest_chapter(manga.id, max(ch.id for ch in chapters))
        except Exception as exc:
            LOGGER.warning("拉取「%s」章节失败: %s", manga.title, exc)

    async def _cmd_batch_subscribe(self, raw: str, conversation_id: str) -> MangaCommandResult:
        prefix = "漫画 批量订阅"
        if raw.startswith("/漫画 批量订阅"):
            prefix = "/漫画 批量订阅"
        if not raw.startswith(prefix):
            return self._result([OutgoingMessage("text", "用法: 漫画 批量订阅 <名称1>, <名称2>, ... [源名]")])
        args_str = raw[len(prefix):].strip()
        if not args_str:
            return self._result([OutgoingMessage("text", "用法: 漫画 批量订阅 <名称1>, <名称2>, ... [源名]\n名称用逗号分隔")])

        import re as _re

        sources = await self.client.get_sources()
        src_map = {str(s.id): s.display_name for s in sources}
        source_filter = None
        search_str = args_str
        last_space = args_str.rfind(" ")
        if last_space > 0:
            potential_source = args_str[last_space + 1:].lower()
            for src in sources:
                if potential_source in (src.name.lower(), src.display_name.lower(), src.lang.lower()):
                    source_filter = src
                    search_str = args_str[:last_space]
                    break

        raw_names = [n.strip() for n in _re.split(r"[,，;；]", search_str) if n.strip()]
        if not raw_names:
            return self._result([OutgoingMessage("text", "请提供漫画名称，用逗号分隔。")])
        if len(raw_names) > _MAX_BATCH_SUBSCRIBE:
            return self._result([OutgoingMessage("text", f"一次最多批量订阅 {_MAX_BATCH_SUBSCRIBE} 部漫画。")])

        existing_ids = {s["manga_id"] for s in self.store.get_subscriptions(conversation_id)}
        results: list[tuple[str, str, str]] = []
        for name in raw_names:
            manga, error = await self.search_best_match(name, source_filter)
            if error or manga is None:
                results.append((name, "fail", error or "未找到匹配结果"))
                continue
            if manga.id in existing_ids:
                status_text = format_manga_status(manga.status)
                source_name = src_map.get(str(manga.source_id), "")
                results.append((name, "exists", f"{manga.title} - {status_text} - {source_name}"))
                continue
            await self._subscribe_manga(conversation_id, manga)
            existing_ids.add(manga.id)
            status_text = format_manga_status(manga.status)
            source_name = src_map.get(str(manga.source_id), "")
            results.append((name, "ok", f"{manga.title} - {status_text} - {source_name}"))

        ok_count = sum(1 for _, s, _ in results if s == "ok")
        exist_count = sum(1 for _, s, _ in results if s == "exists")
        fail_count = sum(1 for _, s, _ in results if s == "fail")
        lines = [f"📚 批量订阅完成 ({ok_count} 新增, {exist_count} 已存在, {fail_count} 失败):"]
        for name, status, info in results:
            if status == "ok":
                lines.append(f"  ✅ {info}")
            elif status == "exists":
                lines.append(f"  ⏭ {info} (已订阅)")
            else:
                lines.append(f"  ❌ {name} - {info}")
        return self._result([OutgoingMessage("text", "\n".join(lines))])

    async def _cmd_unsubscribe(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            name_or_id = tokens[tokens.index("取消订阅") + 1]
        except (ValueError, IndexError):
            name_or_id = ""

        manga_id: int | None = None
        manga_title = name_or_id
        try:
            manga_id = int(name_or_id)
        except (ValueError, TypeError):
            norm_input = normalize_zh(name_or_id)
            for sub in self.store.get_subscriptions(conversation_id):
                if norm_input and norm_input in normalize_zh(sub["title"]):
                    manga_id = sub["manga_id"]
                    manga_title = sub["title"]
                    break

        if manga_id is None:
            return self._result([OutgoingMessage("text", "未找到匹配的订阅，请使用漫画 ID 或名称。")])
        self.store.unsubscribe(manga_id, conversation_id)
        return self._result([OutgoingMessage("text", f"✅ 已取消订阅「{manga_title}」(ID: {manga_id})。")])

    async def _cmd_my_subscriptions(self, raw: str, conversation_id: str) -> MangaCommandResult:
        subs = self.store.get_subscriptions(conversation_id)
        if not subs:
            return self._result([OutgoingMessage("text", "📭 你还没有订阅任何漫画。使用「漫画 搜索」来查找并订阅。")])
        sources = await self.client.get_sources()
        src_map = {str(s.id): s.display_name for s in sources}
        lines = ["📋 你的订阅列表:"]
        for sub in subs:
            source_name = src_map.get(str(sub["source_id"]), "")
            tag = f" - {source_name}" if source_name else ""
            push = "🔔推送" if sub.get("push_enabled") else ""
            lines.append(f"  • {sub['title']}{tag} - ID: {sub['manga_id']} {push}")
        return self._result([OutgoingMessage("text", "\n".join(lines))])

    async def _cmd_push(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            action = tokens[tokens.index("推送") + 1]
        except (ValueError, IndexError):
            action = ""
        subs = self.store.get_subscriptions(conversation_id)
        if action not in {"开", "关", "状态"}:
            return self._result([OutgoingMessage("text", "用法: 漫画 推送 开/关/状态")])
        if not subs:
            return self._result([OutgoingMessage("text", "📭 你还没有订阅任何漫画。")])

        if action == "开":
            self.store.set_auto_push_all(conversation_id, True)
            self.store.set_push_default(conversation_id, True)
            return self._result([OutgoingMessage("text", f"✅ 已开启自动推送，共 {len(subs)} 部漫画。有更新时将自动推送内容。")])
        if action == "关":
            self.store.set_auto_push_all(conversation_id, False)
            self.store.clear_push_default(conversation_id)
            return self._result([OutgoingMessage("text", "✅ 已关闭自动推送。有更新时将只发送文本通知。")])

        all_subs = self.store.get_all_subscriptions()
        lines = ["📡 自动推送状态:"]
        for sub in subs:
            enabled = self.store.is_auto_push_enabled(all_subs, sub["manga_id"], conversation_id)
            status = "✅ 开启" if enabled else "❌ 关闭"
            lines.append(f"  • {sub['title']} — {status}")
        return self._result([OutgoingMessage("text", "\n".join(lines))])

    async def _cmd_chapters(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            idx = tokens.index("章节")
            args = tokens[idx + 1:]
        except ValueError:
            args = []
        force = "--刷新" in args
        name_or_id = " ".join(a for a in args if a != "--刷新").strip()
        if not name_or_id:
            return self._result([OutgoingMessage("text", "用法: 漫画 章节 <漫画名或ID> [--刷新]")])

        manga, err = await self.resolve_manga(name_or_id, conversation_id, "章节")
        if err or manga is None:
            return self._result([OutgoingMessage("text", err or "未找到该漫画。")])

        chapters = await self.get_or_fetch_chapters(manga.id, force=force)
        if not chapters:
            return self._result([OutgoingMessage("text", f"「{manga.title}」暂无章节。")])

        chapters.sort(key=lambda ch: ch.source_order)
        num_count: dict[float, int] = {}
        for ch in chapters:
            num_count[ch.chapter_number] = num_count.get(ch.chapter_number, 0) + 1
        self.remember_exposed_chapters(
            conversation_id, manga.id, {ch.id for ch in chapters}
        )

        try:
            sources = await self.client.get_sources()
            src_name = next(
                (s.display_name for s in sources if str(s.id) == str(manga.source_id)), None
            )
        except Exception:
            src_name = None
        src_tag = f" - {src_name}" if src_name else ""

        header = f"📖「{manga.title}」{src_tag} 章节列表（共 {len(chapters)} 话）:"
        chunks: list[list[str]] = [[]]
        for ch in chapters:
            dl_mark = " 📥" if ch.is_downloaded else ""
            line = f"  {fmt_chapter_label(ch, num_count)}{dl_mark}"
            current_len = sum(len(item) for item in chunks[-1]) + len(header)
            if current_len + len(line) > 1500 and chunks[-1]:
                chunks.append([])
            chunks[-1].append(line)

        messages: list[OutgoingMessage] = []
        for i, chunk in enumerate(chunks):
            prefix = header if i == 0 else f"📖「{manga.title}」{src_tag} 章节续 ({i + 1}/{len(chunks)}):"
            messages.append(OutgoingMessage("text", prefix + "\n" + "\n".join(chunk)))
        return self._result(messages)

    async def _cmd_read(self, raw: str, conversation_id: str) -> MangaCommandResult:
        tokens = raw.split()
        try:
            idx = tokens.index("阅读")
            args = tokens[idx + 1:]
        except ValueError:
            args = []
        if len(args) < 2:
            return self._result([OutgoingMessage("text", "用法: 漫画 阅读 <漫画名或ID> <章节号>\n示例: 漫画 阅读 一拳超人 1\n指定章节 ID: 漫画 阅读 一拳超人 ID:123")])
        name_or_id = args[0]
        chapter_num = " ".join(args[1:])

        manga, err = await self.resolve_manga(name_or_id, conversation_id, "阅读")
        if err or manga is None:
            return self._result([OutgoingMessage("text", err or "未找到该漫画。")])

        chapters = await self.get_or_fetch_chapters(manga.id)
        self.remember_exposed_chapters(
            conversation_id, manga.id, {ch.id for ch in chapters}
        )
        target, err_msg = resolve_chapter(chapters, chapter_num, name_or_id, "阅读")
        if err_msg or target is None:
            return self._result([OutgoingMessage("text", err_msg or f"未找到「{manga.title}」指定的章节。")])

        paths, tmp_dir = await self.prepare_chapter_images(target.id)
        if not paths:
            return self._result([OutgoingMessage("text", "该章节图片加载失败，请稍后重试或改用「漫画 下载」。")])

        label = fmt_chapter_display(target)
        messages = [OutgoingMessage("text", f"📖「{manga.title}」{label}")]
        messages.extend(OutgoingMessage("image", path) for path in paths)
        return self._result(messages, cleanup_dirs=[tmp_dir] if tmp_dir else [], cleanup_delay=60)

    async def _cmd_download(self, raw: str, conversation_id: str) -> MangaCommandResult:
        default_fmt = str(getattr(self.config, "manga_download_format", "pdf"))
        name_or_id, chapter_num, fmt = parse_download_args(raw, default_fmt)
        if not name_or_id or not chapter_num:
            return self._result([OutgoingMessage("text", "用法: 漫画 下载 <漫画名或ID> <章节号> [格式]\n示例: 漫画 下载 一拳超人 1 pdf")])

        manga, err = await self.resolve_manga(name_or_id, conversation_id, "下载")
        if err or manga is None:
            return self._result([OutgoingMessage("text", err or "未找到该漫画。")])

        chapters = await self.get_or_fetch_chapters(manga.id)
        self.remember_exposed_chapters(
            conversation_id, manga.id, {ch.id for ch in chapters}
        )
        target, err_msg = resolve_chapter(chapters, chapter_num, name_or_id, "下载")
        if err_msg or target is None:
            return self._result([OutgoingMessage("text", err_msg or f"未找到「{manga.title}」指定的章节。")])

        output_path, tmp_dir, actual_fmt = await self.prepare_chapter_file(manga, target, fmt)
        if output_path is None:
            return self._result([OutgoingMessage("text", "该章节图片下载失败，无法打包。")])
        return self._result(
            [OutgoingMessage("file", str(output_path))],
            cleanup_dirs=[tmp_dir] if tmp_dir else [],
            cleanup_delay=180,
        )

    async def _cmd_update(self, raw: str, conversation_id: str) -> MangaCommandResult:
        notifications = await self.check_updates(force=True)
        total_new = sum(len(item.get("chapters", [])) for n in notifications for item in n.items)
        if not notifications:
            text = "✅ 所有订阅的漫画暂无更新。"
        elif total_new == 0:
            text = "📭 没有订阅的漫画，无需检查更新。"
        else:
            text = f"✅ 更新检查完成：{len(notifications)} 个会话有更新，共 {total_new} 个新章节。"
        return self._result([OutgoingMessage("text", text)], notifications=notifications)

    # ── LLM 工具 ───────────────────────────────────────────────────

    async def tool_search_manga(
        self, query: str, source_hint: str = "", search_all_sources: bool = False
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"success": False, "error": "搜索关键词为空"}

        sources = await self.client.get_sources()
        if not sources:
            return {"success": False, "error": "未找到已安装的漫画源"}

        if search_all_sources:
            target_sources = [s for s in sources if str(s.id) != "0"]
        elif source_hint:
            target_sources = [
                s
                for s in sources
                if str(s.id) != "0"
                and source_hint.casefold() in s.name.casefold() + s.display_name.casefold()
            ] or [s for s in sources if str(s.id) != "0" and source_hint.casefold() == s.lang.casefold()]
        else:
            target_sources = select_search_sources(
                sources,
                "",
                getattr(self.config, "manga_default_source_id", 0),
                max_sources=int(getattr(self.config, "manga_ai_max_sources", 5)),
            )
        if not target_sources:
            return {"success": False, "error": f"未找到与「{source_hint}」匹配的漫画源"}

        per_source = int(getattr(self.config, "manga_ai_results_per_source", 5))
        results: list[dict[str, Any]] = []
        for src in target_sources:
            try:
                result = await self.client.search_manga(src.id, query)
            except Exception as exc:
                LOGGER.warning("AI 搜索源 %s 失败: %s", src.name, exc)
                continue
            for manga in result.mangas[:per_source]:
                results.append(
                    {
                        "manga_id": manga.id,
                        "title": manga.title,
                        "status": format_manga_status(manga.status),
                        "source": src.display_name,
                        "author": manga.author or "",
                        "in_library": manga.in_library,
                    }
                )
            if not search_all_sources and results:
                break

        if not results:
            return {"success": False, "error": f"未搜索到「{query}」相关漫画，可尝试其他关键词"}
        return {"success": True, "query": query, "count": len(results), "results": results}

    async def tool_get_chapters(
        self,
        conversation_id: str,
        manga_id: int,
        selector: str = "latest",
        refresh: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        try:
            manga_id = int(manga_id)
        except (TypeError, ValueError):
            return {"success": False, "error": "manga_id 必须是整数"}
        try:
            manga = await self.client.get_manga(manga_id)
        except SuwayomiError as exc:
            return {"success": False, "error": f"漫画不存在或服务异常：{exc}"}

        chapters = await self.get_or_fetch_chapters(manga_id, force=bool(refresh))
        if not chapters:
            return {"success": True, "manga": self._manga_brief(manga), "chapters": [], "note": "该漫画暂无章节"}
        # 记录已暴露章节，作为 suwayomi_send_chapter 的防误发闸门。
        self.remember_exposed_chapters(conversation_id, manga_id, {ch.id for ch in chapters})

        chapters.sort(key=lambda ch: ch.source_order)
        num_count: dict[float, int] = {}
        for ch in chapters:
            num_count[ch.chapter_number] = num_count.get(ch.chapter_number, 0) + 1

        selector = str(selector or "latest").strip()
        selected: Chapter | None = None
        if selector.lower().startswith("id:"):
            try:
                selected = next(ch for ch in chapters if ch.id == int(selector[3:]))
            except (ValueError, StopIteration):
                return {"success": False, "error": f"未找到 ID 为 {selector[3:]} 的章节"}
        elif selector not in {"list", "latest"}:
            parsed = None
            try:
                parsed = float(selector.replace("第", "").replace("话", "").replace("話", ""))
            except ValueError:
                parsed = None
            if parsed is None:
                return {"success": False, "error": "selector 可为 list、latest、章节号或 ID:数字"}
            matches = [ch for ch in chapters if abs(ch.chapter_number - parsed) < 0.01]
            if not matches:
                return {"success": False, "error": f"未找到第 {selector} 话"}
            if len(matches) > 1:
                story = [ch for ch in matches if not _is_non_story_chapter(ch)]
                if len(story) == 1:
                    selected = story[0]
                    return self._chapters_payload(
                        manga, chapters, selected, limit, num_count,
                        note=(
                            f"第 {selector} 话有多个匹配，已自动选择正篇"
                            f"「{fmt_chapter_display(selected)}」；"
                            f"其余为番外/设定类章节，用户需要时可用 ID:数字 指定。"
                        ),
                    )
                return {
                    "success": False,
                    "error": (
                        "该章节号有多个匹配，请把候选列给用户让用户选择；"
                        "用户确认后必须在同一轮直接调用 suwayomi_send_chapter"
                        "(manga_id, chapter_id) 发送，无需再次调用本工具"
                    ),
                    "candidates": [
                        {"chapter_id": ch.id, "name": fmt_chapter_label(ch, num_count)} for ch in matches
                    ],
                }
            selected = matches[0]

        if selected is None:
            selected = chapters[-1]

        return self._chapters_payload(manga, chapters, selected, limit, num_count)

    def _chapters_payload(
        self,
        manga: Manga,
        chapters: list[Chapter],
        selected: Chapter,
        limit: int,
        num_count: dict[float, int],
        note: str = "",
    ) -> dict[str, Any]:
        limit = max(1, min(100, int(limit or 20)))
        tail = chapters[-limit:]
        payload: dict[str, Any] = {
            "success": True,
            "manga": self._manga_brief(manga),
            "total_chapters": len(chapters),
            "chapters": [
                {
                    "chapter_id": ch.id,
                    "label": fmt_chapter_label(ch, num_count),
                    "number": fmt_chapter_num(ch.chapter_number),
                }
                for ch in tail
            ],
            "selected_chapter": {
                "chapter_id": selected.id,
                "label": fmt_chapter_label(selected, num_count),
                "number": fmt_chapter_num(selected.chapter_number),
            },
        }
        if note:
            payload["note"] = note
        return payload

    def _manga_brief(self, manga: Manga) -> dict[str, Any]:
        return {
            "manga_id": manga.id,
            "title": manga.title,
            "status": format_manga_status(manga.status),
            "author": manga.author or "",
            "in_library": manga.in_library,
        }

    async def tool_subscribe(
        self, conversation_id: str, manga_id: int, push_enabled: bool | None = None
    ) -> dict[str, Any]:
        try:
            manga_id = int(manga_id)
        except (TypeError, ValueError):
            return {"success": False, "error": "manga_id 必须是整数"}
        try:
            manga = await self.client.get_manga(manga_id)
        except SuwayomiError as exc:
            return {"success": False, "error": f"漫画不存在或服务异常：{exc}"}

        existing = [s for s in self.store.get_subscriptions(conversation_id) if s["manga_id"] == manga_id]
        if existing:
            return {
                "success": True,
                "already_subscribed": True,
                "manga_id": manga_id,
                "title": existing[0]["title"],
                "push_enabled": existing[0]["push_enabled"],
            }

        self.store.subscribe(manga.id, manga.title, manga.source_id, conversation_id)
        if push_enabled is not None:
            self.store.set_auto_push(manga_id, conversation_id, bool(push_enabled))
        try:
            chapters = await self.get_or_fetch_chapters(manga_id)
            if chapters:
                self.store.update_latest_chapter(manga_id, max(ch.id for ch in chapters))
        except Exception as exc:
            LOGGER.warning("订阅后拉取章节失败: %s", exc)
        return {"success": True, "manga_id": manga_id, "title": manga.title}

    async def tool_get_subscriptions(self, conversation_id: str) -> dict[str, Any]:
        subs = self.store.get_subscriptions(conversation_id)
        return {
            "success": True,
            "count": len(subs),
            "subscriptions": [
                {
                    "manga_id": s["manga_id"],
                    "title": s["title"],
                    "push_enabled": s["push_enabled"],
                }
                for s in subs
            ],
        }

    async def tool_unsubscribe(self, conversation_id: str, manga_id: int) -> dict[str, Any]:
        try:
            manga_id = int(manga_id)
        except (TypeError, ValueError):
            return {"success": False, "error": "manga_id 必须是整数"}
        existing = [s for s in self.store.get_subscriptions(conversation_id) if s["manga_id"] == manga_id]
        if not existing:
            return {"success": True, "not_subscribed": True, "message": "当前会话没有订阅该漫画"}
        self.store.unsubscribe(manga_id, conversation_id)
        return {"success": True, "manga_id": manga_id, "title": existing[0]["title"]}

    async def tool_send_chapter(
        self, conversation_id: str, manga_id: int, chapter_id: int
    ) -> tuple[bool, str]:
        """校验章节可发送；返回 (是否受理, 说明文本)。实际发送由 OneBot 层完成。"""
        try:
            manga_id = int(manga_id)
            chapter_id = int(chapter_id)
        except (TypeError, ValueError):
            return False, "manga_id 和 chapter_id 必须是整数"
        if not self.was_chapter_exposed(conversation_id, manga_id, chapter_id):
            return False, "该章节不在当前会话最近的章节查询结果中；请先调用 suwayomi_get_chapters 明确章节"
        try:
            chapters = await self.get_or_fetch_chapters(manga_id)
        except SuwayomiError as exc:
            return False, f"获取章节失败：{exc}"
        target = next((ch for ch in chapters if ch.id == chapter_id), None)
        if target is None:
            return False, "指定章节不属于该漫画或已经失效"
        return True, fmt_chapter_display(target)

    # ── 更新检查 ───────────────────────────────────────────────────

    async def check_updates(self, force: bool = False) -> list[MangaUpdateNotification]:
        async with self._update_lock:
            return await self._check_updates_locked(force)

    async def _check_updates_locked(self, force: bool) -> list[MangaUpdateNotification]:
        all_subs = self.store.get_all_subscriptions()
        if not all_subs:
            self.store.set_last_check()
            return []

        try:
            await asyncio.wait_for(self.client.update_library(), timeout=30)
        except Exception as exc:
            LOGGER.warning("触发书库更新失败: %s", exc)

        cache_hours = int(getattr(self.config, "manga_chapter_cache_hours", 6))
        if cache_hours < -1:
            cache_hours = 0

        sem = asyncio.Semaphore(_UPDATE_CONCURRENCY)

        async def _run(item: tuple[str, dict[str, Any]]):
            async with sem:
                return await self._check_one_manga(force, cache_hours, item[0], item[1])

        results = await asyncio.gather(*(_run(item) for item in all_subs.items()))
        updated = [r for r in results if r is not None]

        if not updated:
            LOGGER.info(
                "更新检查完成: 检查 %s 部漫画，暂无更新", len(all_subs)
            )
            self.store.set_last_check()
            return []

        LOGGER.info("更新检查完成: 发现 %s 部有更新", len(updated))

        notifications: dict[str, MangaUpdateNotification] = {}
        for manga_id, title, new_chapters, subscribers, manga_obj in updated:
            num_count: dict[float, int] = {}
            try:
                all_chapters = await self.get_or_fetch_chapters(manga_id, force=force)
                for ch in all_chapters:
                    num_count[ch.chapter_number] = num_count.get(ch.chapter_number, 0) + 1
            except Exception:
                pass
            new_chapters.sort(key=lambda ch: ch.source_order)
            ch_info = [fmt_chapter_label(ch, num_count) for ch in new_chapters]
            latest_num = fmt_chapter_num(new_chapters[-1].chapter_number)
            for conversation_id in subscribers:
                notification = notifications.setdefault(
                    conversation_id, MangaUpdateNotification(conversation_id=conversation_id)
                )
                notification.items.append(
                    {
                        "manga_id": manga_id,
                        "title": title,
                        "status": manga_obj.status if manga_obj else "UNKNOWN",
                        "chapters": ch_info,
                        "latest_num": latest_num,
                    }
                )
                if self.store.is_auto_push_enabled(
                    self.store.get_all_subscriptions(), manga_id, conversation_id
                ):
                    notification.push_chapters.extend(
                        (manga_id, title, ch) for ch in new_chapters
                    )

        self.store.set_last_check()
        return list(notifications.values())

    async def _check_one_manga(
        self,
        force: bool,
        cache_hours: int,
        manga_id_str: str,
        info: dict[str, Any],
    ) -> tuple[int, str, list[Chapter], dict, Manga | None] | None:
        try:
            manga_id = int(manga_id_str)
        except (TypeError, ValueError):
            LOGGER.warning("订阅数据损坏: 忽略非法漫画 ID %r", manga_id_str)
            return None
        title = info.get("title", f"ID:{manga_id}")
        latest_stored = info.get("latest_chapter_id", 0)
        subscribers = info.get("subscribers", {})
        if not subscribers:
            return None

        manga_obj: Manga | None = None
        try:
            if force or cache_hours != 0:
                manga = await self.client.get_manga(manga_id)
                manga_obj = manga
                if self.store.update_title(manga_id, manga.title):
                    LOGGER.info("漫画标题已更新:「%s」->「%s」", title, manga.title)
                    title = manga.title
        except Exception:
            pass

        try:
            chapters = await self.get_or_fetch_chapters(manga_id, force=force)
        except Exception as exc:
            LOGGER.warning("检查漫画 %s (ID:%s) 更新失败: %s", title, manga_id, exc)
            return None
        if not chapters:
            return None

        new_chapters = [ch for ch in chapters if ch.id > latest_stored]
        if not new_chapters:
            return None

        self.store.update_latest_chapter(manga_id, max(ch.id for ch in chapters))
        LOGGER.info("发现更新:「%s」新增 %s 章节", title, len(new_chapters))
        if manga_obj is None:
            try:
                manga_obj = await self.client.get_manga(manga_id)
            except Exception:
                pass
        return manga_id, title, new_chapters, subscribers, manga_obj


_service_instance: MangaService | None = None


def get_manga_service(config: Any) -> MangaService | None:
    """进程级单例；manga_enabled=False 时返回 None，调用方需自行判空。"""
    global _service_instance
    if not bool(getattr(config, "manga_enabled", True)):
        return None
    if _service_instance is None:
        _service_instance = MangaService(config)
    return _service_instance


def reset_manga_service() -> None:
    """测试隔离用：丢弃单例，避免跨用例共享状态。"""
    global _service_instance
    _service_instance = None
