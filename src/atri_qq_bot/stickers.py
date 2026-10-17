from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .sticker_parts import core as _sticker_core

LOGGER = logging.getLogger("atri.stickers")


IMAGE_EXTENSIONS = _sticker_core.IMAGE_EXTENSIONS
CONTENT_TYPE_EXTENSIONS = _sticker_core.CONTENT_TYPE_EXTENSIONS
KNOWN_EMOTIONS = _sticker_core.KNOWN_EMOTIONS
EMOTION_PRIORITY = _sticker_core.EMOTION_PRIORITY
DEFAULT_EMOTION_KEYWORDS = _sticker_core.DEFAULT_EMOTION_KEYWORDS
DEFAULT_EMOJIS = _sticker_core.DEFAULT_EMOJIS
DEFAULT_QQ_FACE_IDS = _sticker_core.DEFAULT_QQ_FACE_IDS

# 偷取入库文件名前缀（meme_stealer 写入发送库时使用）。StickerManager
# 以此区分自带表情包与偷取表情包：容量淘汰只删偷取文件，发送配比
# （自带:偷取）也以此为界。
STEAL_FILE_PREFIX = "steal_"

_FILENAME_EMOTION_HINTS = {
    "happy": ("哈哈", "开心", "满意", "热情", "可爱"),
    "comfort": ("理解",),
    "sad": ("呜呜", "疼"),
    "angry": ("生气", "攻击", "火箭拳", "警告", "拒绝"),
    "tired": ("好累", "好热", "懒"),
    "proud": ("高性能", "自信", "认可", "赞美", "满意"),
    "confused": ("疑惑", "啊", "诶"),
    "speechless": ("无语", "不屑"),
    "surprised": ("惊喜", "啊", "诶"),
    "awkward": ("心虚", "掩饰", "装傻"),
    "teasing": ("调皮", "搞怪", "抽象", "轻浮", "捡回一条小命", "装傻", "嘴硬"),
    "shy": ("害羞",),
    "affection": ("撒娇", "请求"),
    "encourage": ("加油",),
    "thinking": ("若有所思", "让我想想", "果然如此"),
    "pout": ("傲娇", "不服", "委屈", "不快", "嘴硬"),
}
_FILENAME_CONTEXT_ALIASES = {
    "生气": ("气死", "火大", "恼火", "红温", "过分"),
    "攻击": ("攻击", "打他", "揍", "火箭拳", "欺负"),
    "警告": ("警告", "不许", "不准", "注意点"),
    "拒绝": ("拒绝", "不要", "不可以", "不行"),
    "哈哈": ("好笑", "笑死", "绷不住"),
    "理解": ("理解", "明白", "懂你", "我懂"),
    "呜呜": ("想哭", "哭了", "难过", "伤心"),
    "疼": ("疼", "痛", "受伤"),
    "好累": ("累", "疲惫", "撑不住"),
    "懒": ("懒", "不想动"),
    "高性能": ("高性能", "完成", "搞定", "成功"),
    "疑惑": ("疑惑", "不懂", "为什么", "怎么回事"),
    "无语": ("无语", "麻了", "没眼看"),
    "不屑": ("不屑", "嫌弃", "就这"),
    "惊喜": ("惊喜", "惊了", "不会吧", "真的假的"),
    "心虚": ("心虚", "被发现", "露馅"),
    "装傻": ("装傻", "不知道", "别问我"),
    "调皮": ("调皮", "逗你", "开玩笑"),
    "搞怪": ("搞怪", "抽象", "整活"),
    "轻浮": ("轻浮", "阴阳怪气", "逗你"),
    "害羞": ("害羞", "喜欢你", "爱你", "亲亲"),
    "撒娇": ("撒娇", "求求", "拜托", "陪我"),
    "加油": ("加油", "努力", "坚持", "开始做"),
    "若有所思": ("考虑", "想想", "纠结", "怎么选"),
    "傲娇": ("傲娇", "嘴硬", "才不是"),
    "委屈": ("委屈", "欺负我", "不开心"),
}
_FILENAME_SPLIT_RE = re.compile(r"[\s,，、;；。.!！?？_\-—()（）【】\[\]{}]+")
_STICKER_REQUEST_RE = re.compile(
    r"(?:发|来|整|甩|给我).{0,10}(?:表情包|表情|贴图)"
    r"|(?:表情包|表情|贴图).{0,10}(?:发|来|整|甩|给)"
)
_STICKER_NEGATION_RE = re.compile(
    r"(?:别|不要|不用|不许|禁止).{0,10}(?:表情包|表情|贴图)"
)


def _default_capture_dir() -> Path:
    configured = os.getenv("STICKER_CAPTURE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    records_root = os.getenv("QQ_RECORDS_ROOT", "").strip()
    root = (
        Path(records_root).expanduser()
        if records_root
        else Path.home() / "Documents" / "Tencent Files"
    )
    bot_qq = os.getenv("BOT_QQ", "0").strip() or "0"
    return root / bot_qq / "ATRI" / "chat_history"


def is_explicit_sticker_request(text: str) -> bool:
    """识别用户明确要求发送表情的自然语言，并排除否定表达。"""

    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact or _STICKER_NEGATION_RE.search(compact):
        return False
    return bool(_STICKER_REQUEST_RE.search(compact))


@dataclass(frozen=True)
class StickerChoice:
    emotion: str
    file_url: str | None = None
    face_id: str | None = None
    emoji_text: str | None = None
    triggered: bool = False
    local: bool = False


class StickerManager:
    def __init__(
        self,
        sticker_dir: Path,
        trigger_file: Path | None = None,
        capture_dir: Path | None = None,
        steal_send_ratio: float = 0.0,
    ) -> None:
        self.sticker_dir = sticker_dir
        self.trigger_file = trigger_file or sticker_dir / "triggers.json"
        # The production server passes this explicitly. The fallback is still
        # account-scoped so direct construction can never write into the model
        # source directory.
        self.capture_dir = capture_dir or _default_capture_dir()
        self._custom = self._load_custom_config()
        # 发送配比：偷取表情包的发送占比（0.3 ≈ 自带:偷取 7:3）。0 表示
        # 不区分来源，沿用全库语义混选的旧行为。
        self._steal_send_ratio = max(0.0, min(1.0, float(steal_send_ratio)))
        # 偷取入库的 sidecar 元数据缓存：键为图片路径，值带双文件 mtime
        # 指纹，文件或 sidecar 变化自动失效。
        self._sidecar_cache: dict[Path, tuple[tuple[int, int], tuple[str, ...], str]] = {}

    def detect_emotion(self, user_text: str, reply_text: str = "") -> str:
        matched = self._match_custom_trigger(user_text)
        if matched:
            return _normalize_emotion(matched[0])

        emotion_keywords = dict(DEFAULT_EMOTION_KEYWORDS)
        custom_keywords = self._custom.get("emotion_keywords")
        if isinstance(custom_keywords, dict):
            for emotion, keywords in custom_keywords.items():
                emotion = _normalize_emotion(str(emotion))
                emotion_keywords[emotion] = tuple(map(str, _as_list(keywords)))

        user_emotion = _score_emotion(user_text, emotion_keywords, weight=3)
        reply_emotion = _score_emotion(reply_text, emotion_keywords, weight=1)
        scores = dict(reply_emotion)
        for emotion, score in user_emotion.items():
            scores[emotion] = scores.get(emotion, 0) + score

        if not scores:
            return "neutral"

        return max(
            scores,
            key=lambda emotion: (scores[emotion], EMOTION_PRIORITY.get(emotion, 0)),
        )

    def choose(
        self,
        user_text: str,
        reply_text: str,
        chance: float,
        profile: dict[str, Any] | None = None,
        cooldown_seconds: int = 0,
    ) -> StickerChoice | None:
        triggered = False
        emotion = "neutral"
        forced_image: Path | str | None = None
        explicit_request = is_explicit_sticker_request(user_text)

        custom_match = self._match_custom_trigger(user_text)
        if explicit_request:
            # 明确的发送请求属于动作，不应再受随机概率或冷却限制。
            emotion = self.detect_emotion(user_text, reply_text)
            triggered = True
        elif custom_match:
            emotion, forced_image = custom_match
            emotion = _normalize_emotion(emotion)
            triggered = True
        else:
            emotion = self.detect_emotion(user_text, reply_text)

        if not triggered and _in_cooldown(profile, cooldown_seconds):
            return None

        if not triggered:
            adjusted_chance = self._adjusted_chance(chance, profile)
            if random.random() > adjusted_chance:
                return None

        if isinstance(forced_image, str) or (
            isinstance(forced_image, Path) and self._is_primary_file(forced_image)
        ):
            image_path = forced_image
        else:
            image_path = self._choose_image_path(user_text, reply_text, emotion, forced_image)
        if image_path is None and explicit_request:
            image_path = self._pick_any_primary_image()
        if isinstance(image_path, Path):
            return StickerChoice(
                emotion=emotion,
                file_url=str(image_path.resolve()),
                triggered=triggered,
                # _online_default is a downloaded fallback image, not part of
                # the user's QQ sticker library and must stay a normal image.
                local=not self._is_online_default_file(image_path),
            )
        if isinstance(image_path, str):
            return StickerChoice(emotion=emotion, file_url=image_path, triggered=triggered)

        face_id = self._pick_face_id(emotion)
        if face_id:
            return StickerChoice(
                emotion=emotion,
                face_id=face_id,
                emoji_text=self._pick_emoji(emotion),
                triggered=triggered,
            )

        emoji = self._pick_emoji(emotion)
        if emoji:
            return StickerChoice(emotion=emotion, emoji_text=emoji, triggered=triggered)
        return None

    def _choose_image_path(
        self,
        user_text: str,
        reply_text: str,
        emotion: str,
        forced_image: Path | str | None,
    ) -> Path | str | None:
        """按来源配比选图：steal_send_ratio>0 时每次发送先掷「偷取/自带」
        骰子，命中偷取则只在偷取库（steal_ 前缀文件）内选；任一来源为空
        时回退另一来源。ratio=0 保持全库语义混选的旧行为。"""
        include_stolen = self._steal_send_ratio <= 0.0
        if not include_stolen and random.random() < self._steal_send_ratio:
            stolen = self._pick_stolen_image(user_text, reply_text, emotion)
            if stolen is not None:
                return stolen

        semantic_image = self._pick_semantic_image(
            user_text,
            reply_text,
            emotion,
            include_stolen=include_stolen,
        )
        if semantic_image is not None:
            return semantic_image
        if forced_image is not None:
            return forced_image

        image = self._pick_image(emotion, include_stolen=include_stolen)
        if image is None and not include_stolen:
            # 自带库当前情绪无图时回退偷取库，避免无图可发。
            image = self._pick_stolen_image(user_text, reply_text, emotion)
        return image

    def _adjusted_chance(self, chance: float, profile: dict[str, Any] | None) -> float:
        chance = max(0.0, min(1.0, chance))
        if not profile:
            return chance
        if float(profile.get("emoji_rate") or 0.0) >= 0.35:
            return min(0.8, chance + 0.16)
        if int(profile.get("message_count") or 0) <= 2:
            return min(chance, 0.18)
        return chance

    def _match_custom_trigger(self, text: str) -> tuple[str, Path | str | None] | None:
        trigger_words = self._custom.get("trigger_words")
        if not isinstance(trigger_words, dict):
            return None

        for trigger, target in trigger_words.items():
            if str(trigger) not in text:
                continue

            if isinstance(target, dict):
                emotion = _normalize_emotion(str(target.get("emotion") or "happy"))
                file_value = target.get("file")
            else:
                raw_target = str(target)
                if _looks_like_url(raw_target):
                    return "web", raw_target
                if _looks_like_image(raw_target):
                    file_path = self._resolve_file(raw_target)
                    return _normalize_emotion(file_path.stem if file_path else "happy"), file_path
                emotion = _normalize_emotion(raw_target)
                file_value = None

            file_path = self._resolve_file(file_value) if file_value else None
            if file_path is None and _looks_like_url(file_value):
                return emotion, str(file_value)
            return emotion, file_path

        return None

    def _pick_image(self, emotion: str, include_stolen: bool = True) -> Path | str | None:
        emotion = _normalize_emotion(emotion)
        if emotion in {"neutral", "unsorted"}:
            return None

        candidates = self._images_for_emotion(emotion, include_stolen=include_stolen)
        if candidates:
            return random.choice(candidates)
        return self._pick_web_image(emotion)

    def _images_for_emotion(self, emotion: str, include_stolen: bool = True) -> list[Path]:
        roots = self._library_roots()
        for root in roots:
            if not root.exists():
                continue
            manual_candidates = _image_files_in_dir(root / emotion)
            manual_candidates.extend(
                path
                for path in root.iterdir()
                if path.is_file()
                and path.suffix.lower() in IMAGE_EXTENSIONS
                and path.stem.lower().startswith(emotion.lower())
            )
            if not include_stolen:
                manual_candidates = [
                    path
                    for path in manual_candidates
                    if not path.name.startswith(STEAL_FILE_PREFIX)
                ]
            if manual_candidates:
                return sorted(set(manual_candidates))

        return []

    def _pick_semantic_image(
        self,
        user_text: str,
        reply_text: str,
        emotion: str,
        *,
        include_stolen: bool = True,
    ) -> Path | None:
        if not self.sticker_dir.exists():
            return None

        scored: list[tuple[int, Path]] = []
        for path in self.sticker_dir.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() not in IMAGE_EXTENSIONS
                or any(part.startswith("_") for part in path.relative_to(self.sticker_dir).parts[:-1])
            ):
                continue
            if not include_stolen and path.name.startswith(STEAL_FILE_PREFIX):
                continue
            score = _semantic_filename_score(path.stem, user_text, reply_text, emotion)
            sidecar_terms, sidecar_emotion = self._sidecar_terms(path)
            if sidecar_terms:
                score += _semantic_terms_score(
                    sidecar_terms,
                    user_text,
                    reply_text,
                    emotion,
                    emotion_match=(
                        sidecar_emotion
                        and sidecar_emotion == _normalize_emotion(emotion)
                    ),
                )
            if score > 0:
                scored.append((score, path))

        if not scored:
            return None
        best_score = max(score for score, _ in scored)
        return random.choice([path for score, path in scored if score == best_score])

    def _pick_stolen_image(self, user_text: str, reply_text: str, emotion: str) -> Path | None:
        """从偷取库（steal_ 前缀文件）选图：sidecar 语义评分优先，未命中
        时优先在当前情绪目录内随机。"""
        if not self.sticker_dir.exists():
            return None

        scored: list[tuple[int, Path]] = []
        stolen: list[Path] = []
        for path in self.sticker_dir.rglob(f"{STEAL_FILE_PREFIX}*"):
            if (
                not path.is_file()
                or path.suffix.lower() not in IMAGE_EXTENSIONS
                or any(part.startswith("_") for part in path.relative_to(self.sticker_dir).parts[:-1])
            ):
                continue
            stolen.append(path)
            sidecar_terms, sidecar_emotion = self._sidecar_terms(path)
            if not sidecar_terms:
                continue
            score = _semantic_terms_score(
                sidecar_terms,
                user_text,
                reply_text,
                emotion,
                emotion_match=bool(
                    sidecar_emotion and sidecar_emotion == _normalize_emotion(emotion)
                ),
            )
            if score > 0:
                scored.append((score, path))

        if scored:
            best_score = max(score for score, _ in scored)
            return random.choice([path for score, path in scored if score == best_score])
        if not stolen:
            return None
        emotion_dir = _normalize_emotion(emotion)
        if emotion_dir in {"neutral", "unsorted"}:
            # neutral 消息没有情绪目录可依，随机偷取包容易答非所问；
            # 语义未命中就不发，交给后续 QQ 表情/emoji 兜底。
            return None
        matches = [path for path in stolen if path.parent.name == emotion_dir]
        return random.choice(matches or stolen)

    def _sidecar_terms(self, path: Path) -> tuple[tuple[str, ...], str]:
        """读取偷取入库时写入的 sidecar 元数据，返回（检索词, 情绪）。

        偷取文件的文件名不含语义信息（steal_时间戳_哈希），VLM 生成的
        tags/desc/scenes/overlay_text 才是召回依据；读取结果按双文件
        mtime 指纹缓存，避免每轮回复重复解析 JSON。
        """
        sidecar = path.with_suffix(path.suffix + ".json")
        try:
            image_key = path.stat().st_mtime_ns
            sidecar_key = sidecar.stat().st_mtime_ns
        except OSError:
            return (), ""
        cache_key = (image_key, sidecar_key)
        cached = self._sidecar_cache.get(path)
        if cached is not None and cached[0] == cache_key:
            return cached[1], cached[2]
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return (), ""
        if not isinstance(data, dict):
            return (), ""

        terms: list[str] = []
        for field in ("tags", "scenes"):
            values = data.get(field)
            if isinstance(values, list):
                terms.extend(str(value).strip() for value in values if str(value).strip())
        for field in ("desc", "overlay_text"):
            value = str(data.get(field) or "").strip()
            if value:
                terms.extend(part.strip() for part in _FILENAME_SPLIT_RE.split(value) if part.strip())
        # 长 CJK 标签整体几乎不会原样出现在聊天句里，补 2 元滑窗子词，
        # 让「可爱猫咪摸头」能被「好可爱」召回；子词按半权计分。
        subterms = [
            term[i : i + 2]
            for term in terms
            if len(term) >= 4 and not re.search(r"[A-Za-z0-9]", term)
            for i in range(len(term) - 1)
        ]
        emotion = str(data.get("emotion") or "").strip().lower()
        all_terms = tuple(dict.fromkeys([*terms, *subterms]))[:24]
        self._sidecar_cache[path] = (cache_key, all_terms, emotion)
        return all_terms, emotion

    def _pick_any_primary_image(self) -> Path | None:
        """为“发个表情”这类泛化请求从用户表情库选一张。"""

        if not self.sticker_dir.exists():
            return None
        candidates = [
            path
            for path in self.sticker_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and not any(
                part.startswith("_")
                for part in path.relative_to(self.sticker_dir).parts[:-1]
            )
        ]
        return random.choice(candidates) if candidates else None

    def _library_roots(self) -> list[Path]:
        # The configured sticker directory is the only automatic sending
        # source. The trigger file remains configuration; explicit trigger
        # paths are resolved separately and are not mixed into discovery.
        return [self.sticker_dir]

    def _is_primary_file(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.sticker_dir.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _is_online_default_file(self, path: Path) -> bool:
        try:
            relative_parts = path.resolve().relative_to(self.sticker_dir.resolve()).parts
        except (OSError, ValueError):
            return False
        return "_online_default" in relative_parts

    def _resolve_file(self, value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        candidates = [path] if path.is_absolute() else [self.sticker_dir / path]
        for candidate in candidates:
            if (
                candidate.exists()
                and candidate.is_file()
                and candidate.suffix.lower() in IMAGE_EXTENSIONS
            ):
                return candidate
        return None

    def _pick_web_image(self, emotion: str) -> str | None:
        web_images = self._custom.get("web_images")
        if not isinstance(web_images, dict):
            return None

        urls = _as_list(web_images.get(emotion))
        urls = [str(url) for url in urls if _looks_like_url(url)]
        if not urls:
            return None
        return random.choice(urls)

    def _pick_emoji(self, emotion: str) -> str | None:
        custom_emojis = self._custom.get("emotion_emojis")
        if isinstance(custom_emojis, dict) and emotion in custom_emojis:
            values = _as_list(custom_emojis[emotion])
            if values:
                return str(random.choice(values))

        values = DEFAULT_EMOJIS.get(emotion) or DEFAULT_EMOJIS.get("neutral", ())
        return random.choice(values) if values else None

    def _pick_face_id(self, emotion: str) -> str | None:
        custom_faces = self._custom.get("emotion_faces")
        if isinstance(custom_faces, dict) and emotion in custom_faces:
            values = [str(value) for value in _as_list(custom_faces[emotion]) if str(value).strip()]
            if values:
                return random.choice(values)

        values = DEFAULT_QQ_FACE_IDS.get(emotion)
        if not values:
            return None
        return random.choice(values)

    async def capture_from_event(
        self,
        event: dict[str, Any],
        context_text: str,
        enabled: bool = True,
        max_bytes: int = 3_000_000,
    ) -> list[Path]:
        if not enabled:
            return []

        message = event.get("message")
        if not isinstance(message, list):
            return []

        emotion = self.detect_emotion(context_text)
        if emotion == "neutral":
            emotion = "unsorted"
        saved: list[Path] = []
        for index, segment in enumerate(message):
            if not isinstance(segment, dict):
                continue
            if segment.get("type") not in {"image", "mface", "marketface"}:
                continue

            data = segment.get("data") or {}
            url = _first_url(data)
            if not url:
                continue

            path = await self._download_sticker_url(url, emotion, index, max_bytes)
            if path:
                saved.append(path)

        return saved

    async def _download_sticker_url(
        self,
        url: str,
        emotion: str,
        index: int,
        max_bytes: int,
    ) -> Path | None:
        import httpx

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("表情包抓取下载失败：%s", exc)
            return None

        content = response.content
        if not content or len(content) > max_bytes:
            return None

        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        suffix = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if not suffix:
            suffix = _suffix_from_url(url)
        if suffix not in IMAGE_EXTENSIONS:
            return None

        digest = hashlib.sha1(content).hexdigest()[:12]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = self.capture_dir / emotion
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"chat_{timestamp}_{index}_{digest}{suffix}"

        if not target.exists():
            target.write_bytes(content)
            _write_capture_metadata(target, url, emotion, content_type)
            LOGGER.info("已抓取聊天表情包：%s", target)
        return target

    def _load_custom_config(self) -> dict[str, Any]:
        if not self.trigger_file.exists():
            return {}
        try:
            data = json.loads(self.trigger_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def _semantic_filename_score(
    stem: str,
    user_text: str,
    reply_text: str,
    emotion: str,
) -> int:
    terms = [term.strip().lower() for term in _FILENAME_SPLIT_RE.split(stem) if term.strip()]
    return _semantic_terms_score(terms, user_text, reply_text, emotion)


def _semantic_terms_score(
    terms: list[str] | tuple[str, ...],
    user_text: str,
    reply_text: str,
    emotion: str,
    *,
    emotion_match: bool = False,
) -> int:
    if not terms:
        return 0

    user_normalized = _normalize_semantic_text(user_text)
    reply_normalized = _normalize_semantic_text(reply_text)
    score = 0
    for term in terms:
        normalized_term = _normalize_semantic_text(term)
        if not normalized_term:
            continue
        if normalized_term in user_normalized:
            score += 28 + min(12, len(normalized_term) * 2)
        if normalized_term in reply_normalized:
            score += 34 + min(12, len(normalized_term) * 2)
        for label, aliases in _FILENAME_CONTEXT_ALIASES.items():
            if _normalize_semantic_text(label) not in normalized_term:
                continue
            if any(_normalize_semantic_text(alias) in user_normalized for alias in aliases):
                score += 18
            if any(_normalize_semantic_text(alias) in reply_normalized for alias in aliases):
                score += 22

    normalized_emotion = _normalize_emotion(emotion)
    for hint in _FILENAME_EMOTION_HINTS.get(normalized_emotion, ()):
        if any(
            _normalize_semantic_text(hint) in _normalize_semantic_text(term) for term in terms
        ):
            score += 12
    # 偷取 sidecar 的情绪与当前会话情绪一致时直接加分：VLM 判定的情绪
    # 比文件名提示词更可靠。
    if emotion_match:
        score += 12
    return score


def _normalize_semantic_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


_as_list = _sticker_core._as_list
_image_files_in_dir = _sticker_core._image_files_in_dir
_normalize_emotion = _sticker_core._normalize_emotion
_score_emotion = _sticker_core._score_emotion
_looks_like_image = _sticker_core._looks_like_image
_looks_like_url = _sticker_core._looks_like_url
_in_cooldown = _sticker_core._in_cooldown
_first_url = _sticker_core._first_url
_suffix_from_url = _sticker_core._suffix_from_url
_write_capture_metadata = _sticker_core._write_capture_metadata

