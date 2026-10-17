"""表情包小偷（移植自 astrbot_plugin_stealer 的核心链路）。

被动偷图：群聊里出现表情包（QQ 商城表情 / marketface / sub_type=1 的
图片段）时按门控（概率或冷却）偷走——下载 → SHA-256 去重 → VLM 分类
（情绪键 + tags/desc/scenes/overlay_text）→ 落入发送库
``sticker_dir/{emotion}/``，并写同名 sidecar JSON 元数据。StickerManager
的语义检索会读取 sidecar 标签参与评分，偷来的表情包按内容被自然召回。

与原插件的取舍：
- 原插件 17 分类 + SQLite 索引 + 向量检索 + 待审核池 + WebUI 仪表盘；
  ATRi 已有发送库与语义检索，只移植「偷→分类→入库」与「标签参与召回」
  两段核心价值，去重用 SHA-256（不做 pHash）。
- VLM 不可用或分类失败时退化为按上下文情绪入库（sidecar 仍写，标签空），
  与 ATRi 既有 capture_from_event 的情绪判定同构。
- 处理完全在后台 worker 中进行，绝不阻塞回复链路；队列满即丢弃。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..stickers import IMAGE_EXTENSIONS, STEAL_FILE_PREFIX
from .classify import Classification, VisionSettings, classify_image_bytes, looks_like_photo

LOGGER = logging.getLogger("atri.meme_stealer")

VALID_STEAL_MODES = {"probability", "cooldown"}

# mface/marketface 的 URL 字段名（对齐原插件 platform_detector：NapCat
# 不同版本可能只给 cdnurl / cdn_url / raw_url 中的某一个）。
_MFACE_URL_FIELDS = (
    "url",
    "cdnurl",
    "cdn_url",
    "raw_url",
    "original_url",
    "origin_url",
    "file_url",
    "thumb",
    "thumb_url",
    "preview",
)

# 图片段 summary 含这些标记即视为平台表情包（QQ 收藏表情的 summary
# 是「[动画表情]」，个别 NapCat 版本不给 sub_type=1 只靠 summary 标记）。
_STICKER_SUMMARY_MARKERS = ("表情", "emoji", "sticker")


@dataclass(frozen=True)
class StealCandidate:
    url: str
    conversation_id: str
    context_text: str
    # 投机候选（相册直发普通图片）：VLM 判定失败即弃，且只消耗自己的
    # 投机冷却窗口，绝不挤占确认表情包的偷取窗口（原插件教训：照片流
    # 不能饿死真表情包）。
    speculative: bool = False


def _segment_url(data: dict[str, Any]) -> str | None:
    """按原插件优先级提取可下载 URL：先认字段名，再兜底扫描全部字符串值。"""
    for key in _MFACE_URL_FIELDS:
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    for value in data.values():
        if isinstance(value, str):
            value = value.strip()
            if value.startswith(("http://", "https://")):
                return value
    return None


def _segment_sticker_kind(segment: dict[str, Any], plain_images_enabled: bool) -> str:
    """分类 segment：'sticker' 确认表情包 / 'image' 投机候选 / '' 不可偷。

    确认特征（对齐原插件 check_platform_emoji_metadata）：mface/marketface
    原样放行；image 段 sub_type=1、带 emoji 标记或 summary 含表情字样。
    plain_images_enabled 时普通相册图片作为投机候选交给 VLM 判定
    （相册直发的梗图没有上述任何标记）。
    """
    segment_type = segment.get("type")
    if segment_type in {"mface", "marketface"}:
        return "sticker"
    if segment_type != "image":
        return ""
    data = segment.get("data") or {}
    if not isinstance(data, dict):
        return ""
    if str(data.get("sub_type", "") or "") == "1":
        return "sticker"
    if data.get("emoji_id") or data.get("emoji_package_id"):
        return "sticker"
    summary = str(data.get("summary") or "").strip().lower()
    if any(marker in summary for marker in _STICKER_SUMMARY_MARKERS):
        return "sticker"
    return "image" if plain_images_enabled else ""


def extract_steal_candidates(
    event: dict[str, Any],
    conversation_id: str = "",
    context_text: str = "",
    plain_images_enabled: bool = False,
) -> list[StealCandidate]:
    """提取可偷候选；确认表情包排在投机图片之前，队列截断时优先保真表情。"""
    message = event.get("message")
    if not isinstance(message, list):
        return []
    confirmed: list[StealCandidate] = []
    speculative: list[StealCandidate] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        kind = _segment_sticker_kind(segment, plain_images_enabled)
        if not kind:
            continue
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            continue
        url = _segment_url(data)
        if not url:
            continue
        candidate = StealCandidate(
            url=url,
            conversation_id=conversation_id,
            context_text=context_text,
            speculative=kind == "image",
        )
        if kind == "sticker":
            confirmed.append(candidate)
        else:
            speculative.append(candidate)
    return [*confirmed, *speculative]


def _suffix_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ""


def _suffix_from_bytes(content: bytes, url: str) -> str:
    """QQ 商城表情 URL 常无后缀，按文件头嗅探真实格式。"""
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if content[:2] == b"\xff\xd8":
        return ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return _suffix_from_url(url) or ".png"


class MemeStealService:
    def __init__(self, config: Any) -> None:
        self._config = config
        self.enabled = bool(getattr(config, "meme_steal_enabled", True))
        mode = str(getattr(config, "meme_steal_mode", "cooldown")).strip().lower()
        self.mode = mode if mode in VALID_STEAL_MODES else "cooldown"
        self.chance = max(0.0, min(1.0, float(getattr(config, "meme_steal_chance", 0.5))))
        self.cooldown_seconds = max(0, int(getattr(config, "meme_steal_cooldown_seconds", 20)))
        # 相册直发图片（无表情包标记）是否作为投机候选交给 VLM 判定。
        self.steal_plain_images = bool(getattr(config, "meme_steal_plain_images", True))
        self.max_bytes = max(100_000, int(getattr(config, "meme_steal_max_bytes", 3_000_000)))
        self.library_max = max(20, int(getattr(config, "meme_steal_library_max", 300)))
        queue_size = max(1, int(getattr(config, "meme_steal_queue_size", 8)))

        sticker_dir = getattr(config, "sticker_dir", None)
        self.sticker_dir = Path(sticker_dir) if sticker_dir else Path("data/stickers")

        self._vision = VisionSettings.from_config(config)
        self._queue: asyncio.Queue[StealCandidate] = asyncio.Queue(maxsize=queue_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._last_steal_at = 0.0
        # 投机冷却从启动时刻起算：重启后首个冷却窗口先观察流量，不偷
        # 投机图片（避免刚上线就吞一排相册照片）。
        self._last_speculative_steal_at = time.time()
        self._known_hashes: set[str] | None = None
        self._processing_hashes: set[str] = set()
        self._rejected_hashes: deque[str] = deque(maxlen=512)

    # ── 门控与入队（事件线程内调用，必须快） ──

    def maybe_steal_from_event(
        self,
        event: dict[str, Any],
        context_text: str = "",
        conversation_id: str = "",
    ) -> bool:
        """按门控决定是否偷取；命中则把候选塞进后台队列并返回 True。"""
        if not self.enabled:
            return False
        candidates = extract_steal_candidates(
            event,
            conversation_id,
            context_text,
            plain_images_enabled=self.steal_plain_images,
        )
        if not candidates:
            return False
        # 双冷却窗口：确认表情包与投机图片各用各的冷却，照片流不挤占
        # 真表情包的偷取窗口（原插件教训）。
        confirmed = [c for c in candidates if not c.speculative]
        speculative = [c for c in candidates if c.speculative]
        batch: list[StealCandidate] = []
        if confirmed and self._gate_allows():
            batch.extend(confirmed[:3])
        if speculative and self._gate_allows(speculative=True):
            # 同一消息的照片流只抽一张：相册多图九成是照片，VLM 调用省着用。
            batch.append(speculative[0])
        submitted = False
        for candidate in batch:
            try:
                self._queue.put_nowait(candidate)
                submitted = True
            except asyncio.QueueFull:
                LOGGER.debug("偷图队列已满，丢弃候选：%s", candidate.url[:80])
                break
        if submitted:
            LOGGER.info(
                "检测到表情包，准备偷走它！conversation=%s confirmed=%d speculative=%d",
                conversation_id,
                len(confirmed),
                len(speculative),
            )
            self._ensure_worker()
        return submitted

    def _gate_allows(self, speculative: bool = False) -> bool:
        if self.mode == "probability":
            if self.chance <= 0:
                return False
            if self.chance >= 1.0:
                return True
            return random.random() < self.chance
        if self.cooldown_seconds <= 0:
            return True
        if speculative:
            return time.time() - self._last_speculative_steal_at >= self.cooldown_seconds
        return time.time() - self._last_steal_at >= self.cooldown_seconds

    def _consume_cooldown(self, speculative: bool = False) -> None:
        """worker 实际开始处理时刷新对应冷却窗口，防止慢 VLM 下持续入队连发。"""
        if self.mode == "cooldown":
            if speculative:
                self._last_speculative_steal_at = time.time()
            else:
                self._last_steal_at = time.time()

    def _ensure_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中的事件循环（同步上下文/测试）：候选留在队列，下次
            # 在异步上下文提交时会一并启动 worker。
            return
        self._worker_task = loop.create_task(self._worker())

    async def shutdown(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ── 后台 worker ──

    async def _worker(self) -> None:
        while True:
            candidate = await self._queue.get()
            try:
                await self.process_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("偷图处理失败：%s", candidate.url[:120])
            finally:
                self._queue.task_done()

    async def process_candidate(self, candidate: StealCandidate) -> bool:
        """下载→去重→分类→入库 单个候选。返回是否成功入库。"""
        self._consume_cooldown(candidate.speculative)
        content = await self._download(candidate.url)
        if not content:
            return False

        digest = hashlib.sha256(content).hexdigest()
        if await self._is_duplicate(digest):
            LOGGER.info("重复表情包跳过（已入库或已判定丢弃）：hash=%s", digest[:8])
            return False

        if candidate.speculative:
            # 确定性照片预审先于 VLM：高清 JPEG / 带相机 EXIF 的相册直发图
            # 直接丢弃，不消耗视觉调用。
            reason = await asyncio.to_thread(looks_like_photo, content)
            if reason:
                self._remember_rejected(digest)
                LOGGER.info("投机图片命中照片特征（%s），丢弃：url=%s", reason, candidate.url[:80])
                return False

        try:
            classification = await self._classify(content)
            if classification.rejected:
                self._remember_rejected(digest)
                LOGGER.info("偷图被判定为非表情包，丢弃：url=%s", candidate.url[:80])
                return False
            if classification.approved:
                emotion = classification.emotion
            elif candidate.speculative:
                # 投机候选（相册直发普通图）必须拿到 VLM 的明确批准才能
                # 入库：VLM 不可用/失败时不走兜底——普通照片绝不能混进
                # 表情库。失败不是图片的错，不缓存拒绝哈希，VLM 恢复后
                # 同图可重判。
                LOGGER.info("投机图片未获 VLM 批准，丢弃：url=%s", candidate.url[:80])
                return False
            else:
                # 确认表情包（平台已背书）VLM 不可用/失败：按上下文情绪
                # 兜底入库，标签留空。
                emotion = self._context_emotion(candidate.context_text)
            return await self._store(
                content,
                candidate.url,
                digest,
                emotion,
                classification,
                candidate.conversation_id,
            )
        finally:
            self._processing_hashes.discard(digest)

    def _remember_rejected(self, digest: str) -> None:
        """缓存被拒绝的哈希，避免同一张非表情包反复消耗 VLM 调用。"""
        self._rejected_hashes.append(digest)
        while len(self._rejected_hashes) > 512:
            self._rejected_hashes.popleft()

    async def _download(self, url: str) -> bytes | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("偷图下载失败：%s", exc)
            return None
        content = response.content
        if not content or len(content) > self.max_bytes:
            return None
        return content

    async def _classify(self, content: bytes) -> Classification:
        try:
            return await classify_image_bytes(self._vision, content)
        except Exception:
            LOGGER.exception("偷图分类异常")
            return Classification()

    def _context_emotion(self, context_text: str) -> str:
        compact = re.sub(r"\s+", "", str(context_text or ""))
        if not compact:
            return "unsorted"
        # 轻量关键词兜底，避免引入 StickerManager 依赖。
        hints = (
            ("happy", ("哈哈", "笑", "开心", "好耶")),
            ("sad", ("呜", "哭", "难过", "伤心")),
            ("angry", ("气", "怒", "火大")),
            ("surprised", ("？！", "惊", "不会吧", "真的假的")),
            ("teasing", ("哈哈", "草", "抽象", "整活", "梗")),
        )
        for emotion, keywords in hints:
            if any(keyword in compact for keyword in keywords):
                return emotion
        return "unsorted"

    # ── 去重与容量 ──

    async def _is_duplicate(self, digest: str) -> bool:
        if digest in self._processing_hashes or digest in self._rejected_hashes:
            return True
        if self._known_hashes is None:
            self._known_hashes = await self._scan_library_hashes()
        if digest in self._known_hashes:
            return True
        self._processing_hashes.add(digest)
        return False

    async def _scan_library_hashes(self) -> set[str]:
        def _scan() -> set[str]:
            hashes: set[str] = set()
            if not self.sticker_dir.exists():
                return hashes
            for path in self.sticker_dir.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                try:
                    hasher = hashlib.sha256()
                    with open(path, "rb") as handle:
                        while chunk := handle.read(65536):
                            hasher.update(chunk)
                    hashes.add(hasher.hexdigest())
                except OSError:
                    continue
            return hashes

        hashes = await asyncio.to_thread(_scan)
        LOGGER.info("偷图去重索引就绪：library=%s files=%d", self.sticker_dir, len(hashes))
        return hashes

    async def _enforce_capacity(self) -> None:
        def _steal_files() -> list[Path]:
            if not self.sticker_dir.exists():
                return []
            return [
                path
                for path in self.sticker_dir.rglob(f"{STEAL_FILE_PREFIX}*")
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            ]

        files = await asyncio.to_thread(_steal_files)
        overflow = len(files) - self.library_max
        if overflow <= 0:
            return
        files.sort(key=lambda path: path.stat().st_mtime)
        for path in files[:overflow]:
            sidecar = path.with_suffix(path.suffix + ".json")
            try:
                path.unlink()
                if sidecar.exists():
                    sidecar.unlink()
                LOGGER.info("偷图容量淘汰：%s", path.name)
            except OSError as exc:
                LOGGER.warning("偷图容量淘汰失败：%s %s", path.name, exc)

    # ── 入库 ──

    async def _store(
        self,
        content: bytes,
        url: str,
        digest: str,
        emotion: str,
        classification: Classification,
        conversation_id: str,
    ) -> bool:
        emotion = str(emotion or "unsorted").strip().lower() or "unsorted"
        suffix = _suffix_from_bytes(content, url)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{STEAL_FILE_PREFIX}{timestamp}_{digest[:8]}{suffix}"
        target_dir = self.sticker_dir / emotion
        target = target_dir / filename

        def _write() -> Path:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return target

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            LOGGER.warning("偷图入库写入失败：%s", exc)
            return False

        if self._known_hashes is not None:
            self._known_hashes.add(digest)
        sidecar = {
            "source": "meme_steal",
            "url": url,
            "hash": digest,
            "emotion": emotion,
            "tags": list(classification.tags),
            "desc": classification.desc,
            "scenes": list(classification.scenes),
            "overlay_text": classification.overlay_text,
            "origin_conversation": conversation_id,
            "stolen_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            target.with_suffix(target.suffix + ".json").write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            LOGGER.warning("偷图 sidecar 写入失败：%s %s", target.name, exc)
        LOGGER.info(
            "已偷取表情包入库：%s/%s tags=%s",
            emotion,
            target.name,
            ",".join(classification.tags) or "-",
        )
        await self._enforce_capacity()
        return True


_SERVICE: MemeStealService | None = None


def get_meme_steal_service(config: Any) -> MemeStealService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MemeStealService(config)
    return _SERVICE
