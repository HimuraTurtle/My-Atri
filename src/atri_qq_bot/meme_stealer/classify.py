"""表情包小偷的 VLM 分类（移植自 astrbot_plugin_stealer 的分类链路）。

与 toolbox 的对话式视觉分析不同，这里要的是结构化 JSON：情绪分类键
必须落在 ATRi 的 KNOWN_EMOTIONS 词表内，tags/desc/scenes 进 sidecar
元数据供发送检索使用。视觉端点、超时与禁思考链等约定沿用
toolbox_vision_* 配置（生产为 deepseek-flash 云端接口）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..stickers import KNOWN_EMOTIONS
from ..sticker_parts.core import _normalize_emotion
from ..toolbox.helpers import _image_mime_type, _prepare_images_for_vision

LOGGER = logging.getLogger("atri.meme_stealer")

# 分类键词表：发送库按情绪目录组织，neutral/unsorted 没有发送价值。
CLASSIFY_EMOTIONS = tuple(sorted(KNOWN_EMOTIONS - {"neutral", "unsorted", "web"}))

# VLM 给不出词表内情绪时的兜底桶：泛用梗图。
FALLBACK_EMOTION = "teasing"

MAX_TAGS = 6
MAX_SCENES = 3
MAX_DESC_CHARS = 60
MAX_SCENE_CHARS = 16
MAX_OVERLAY_CHARS = 80

_THINKING_DISABLED_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}

_EMOTION_DOC = (
    "happy 开心大笑、comfort 安慰、sad 难过哭泣、angry 生气攻击、tired 累瘫、"
    "proud 自豪得意、confused 疑惑问号、speechless 无语不屑、surprised 惊讶震惊、"
    "awkward 尴尬心虚、teasing 调皮搞怪整活、shy 害羞脸红、affection 撒娇亲昵、"
    "miss 想念、care 关心、thanks 感谢、sorry 道歉、encourage 加油鼓励、"
    "thinking 思考若有所思、celebrate 庆祝、food 吃的食物、goodnight 晚安睡觉、"
    "pout 傲娇嘟嘴"
)

CLASSIFICATION_PROMPT = f"""你是表情包分类器。分析这张图并只输出一个 JSON 对象，不要输出 JSON 以外的任何文字。

核心判断：这张图是不是网友聊天斗图会主动发送的表情包？表情包的唯一目的是传达情绪或反应，
不是展示画面内容。判断依据是图的用途，不是画质、构图或风格高低。

approved=true 的判定依据（满足其一即可）：
- 画面核心是夸张的表情、动作或情绪：卡通/动漫/漫画/手绘/3D角色，脸部特写或全身均可；
- 携带梗：配字玩梗、多格漫画/条漫式吐槽、P图整活、模板化的填空图；
- 真人但明显被做成梗图（怼脸截图、狰狞夸张表情、配吐槽文字）。

approved=false（一律拒绝）：
- 实拍照片：人物照、宠物/动物照、风景、食物、手办、生活记录照——即使可爱或搞笑也不是表情包；
- 欣赏图：立绘、CG、壁纸、唯美插画、以好看为目的的场景图——画面再精致也只是用来欣赏，
  没有夸张情绪表达，不会有人拿来斗图；
- 聊天记录截图、界面截图（未做梗化处理）、二维码、广告、海报。

拿不准时问自己：斗图时会不会主动发这张图？答案是"不会"或犹豫就 false。

可选情绪分类（category 字段的值，必须是以下英文键之一）：
{_EMOTION_DOC}

JSON 字段说明：
- approved: 布尔值。是否为适合聊天斗图的表情包（按上述标准）。
- category: 情绪分类的英文键。
- tags: 最多{MAX_TAGS}个中文短语标签，描述画面主体、动作、梗或风格，每个不超过 12 字。
- desc: 一句不超过{MAX_DESC_CHARS}字的中文描述，说明这个表情包表达什么。
- scenes: 最多{MAX_SCENES}条适用聊天场景短语，每条不超过{MAX_SCENE_CHARS}字。
- overlay_text: 图上出现的文字，逐字转写，没有则空字符串。

如果 approved 为 false，只输出 {{"approved": false}}。"""

_GIF_SEQUENCE_NOTE = (
    "\n本次输入是同一个动画表情按时间顺序抽取的多帧，"
    "请综合动作变化理解完整含义，不要把各帧当成不同图片或不同人物。"
)

# 实拍照片的确定性痕迹：EXIF 相机制造商/型号/实拍时间。注意空值标签
# （编辑器残留的空 DateTime）不算——库里大量梗图带空 306 标签。
_CAMERA_EXIF_TAGS = (271, 272, 36867, 36868)


def looks_like_photo(data: bytes) -> str | None:
    """确定性照片预审：返回判定理由（命中时）或 None。

    唯一硬规则：JPEG 的 EXIF 里带非空相机制造商/型号/实拍时间
    （QQ 原图直发会保留，转存图与梗图不会带这些值）。
    高分辨率不作为依据——1280px 以上的高清梗图/手机长截图在库里
    占比很高，按尺寸杀会大量误杀真表情包。
    """
    if not data:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            for tag in _CAMERA_EXIF_TAGS:
                value = exif.get(tag) if exif else None
                if value and str(value).strip():
                    return f"JPEG含相机EXIF(tag{tag})"
    except Exception:
        return None
    return None


@dataclass(frozen=True)
class Classification:
    approved: bool = False
    emotion: str = FALLBACK_EMOTION
    tags: tuple[str, ...] = ()
    desc: str = ""
    scenes: tuple[str, ...] = ()
    overlay_text: str = ""
    rejected: bool = False  # approved=false（明确判定非表情包）
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VisionSettings:
    enabled: bool
    model: str
    base_url: str
    api_key: str | None
    timeout_seconds: float
    max_bytes: int

    @classmethod
    def from_config(cls, config: Any) -> "VisionSettings":
        model = str(
            getattr(config, "toolbox_vision_model", "") or getattr(config, "openai_model", "")
        )
        base_url = str(
            getattr(config, "toolbox_vision_base_url", "")
            or getattr(config, "openai_base_url", "")
        )
        enabled = (
            bool(getattr(config, "toolbox_vision_enabled", False))
            and bool(model)
            and bool(base_url)
        )
        return cls(
            enabled=enabled,
            model=model,
            base_url=base_url,
            api_key=getattr(config, "toolbox_vision_api_key", None)
            or getattr(config, "openai_api_key", None),
            timeout_seconds=max(10.0, float(getattr(config, "toolbox_vision_timeout_seconds", 55.0))),
            max_bytes=int(getattr(config, "toolbox_vision_max_bytes", 8_000_000)),
        )


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def _normalize_label_list(values: Any, max_count: int) -> tuple[str, ...]:
    if isinstance(values, str):
        values = re.split(r"[,，、;；/]", values)
    if not isinstance(values, (list, tuple)):
        return ()
    cleaned: list[str] = []
    for item in values:
        label = _clip(str(item), 16)
        if label and label not in cleaned:
            cleaned.append(label)
        if len(cleaned) >= max_count:
            break
    return tuple(cleaned)


def normalize_emotion_key(raw: Any) -> str:
    """把 VLM 输出的情绪键归一到发送库词表，未命中落兜底桶。"""
    text = str(raw or "").strip().strip("`\"'").lower()
    if "：" in text or ":" in text:
        text = text.replace("：", ":").partition(":")[2].strip()
    normalized = _normalize_emotion(text)
    if normalized in {"neutral", "unsorted"}:
        return FALLBACK_EMOTION
    return normalized


def extract_json_payload(response: str) -> dict[str, Any] | None:
    """从模型响应中提取第一个合法 JSON 对象（兼容代码围栏与前后缀说明）。"""
    text = str(response or "").strip()
    if not text:
        return None
    candidates: list[str] = []
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL):
        if block.strip():
            candidates.append(block.strip())
    candidates.append(text)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def parse_classification(response: str) -> Classification:
    """解析 VLM 分类响应；解析失败返回 rejected=False、approved=False 的空结果。"""
    data = extract_json_payload(response)
    if data is None:
        return Classification()

    approved = data.get("approved")
    if approved is False or str(approved).strip().lower() in {"false", "0", "no", "rejected"}:
        return Classification(rejected=True)

    emotion = normalize_emotion_key(data.get("category", ""))
    if not emotion:
        emotion = FALLBACK_EMOTION
    tags = _normalize_label_list(data.get("tags"), MAX_TAGS)
    desc = _clip(data.get("desc", ""), MAX_DESC_CHARS)
    scenes = tuple(
        scene for scene in _normalize_label_list(data.get("scenes"), MAX_SCENES) if scene
    )
    if not scenes:
        overlay_scene = _clip(data.get("overlay_text", ""), MAX_SCENE_CHARS)
        scenes = (overlay_scene,) if overlay_scene else ()
    overlay_text = _clip(data.get("overlay_text", ""), MAX_OVERLAY_CHARS)

    return Classification(
        approved=True,
        emotion=emotion,
        tags=tags,
        desc=desc,
        scenes=scenes,
        overlay_text=overlay_text,
    )


async def classify_image_bytes(settings: VisionSettings, data: bytes) -> Classification:
    """调用视觉模型分类表情包；不可用或失败时返回空 Classification（approved=False）。"""
    if not settings.enabled:
        return Classification()
    if not data:
        return Classification()

    frames = _prepare_images_for_vision(data)
    if not frames or sum(len(frame) for frame in frames) > settings.max_bytes:
        LOGGER.warning("偷图分类跳过：帧数为 0 或超过视觉上限 bytes=%d", len(data))
        return Classification()

    encoded = [base64.b64encode(frame).decode("ascii") for frame in frames]
    prompt = CLASSIFICATION_PROMPT + (_GIF_SEQUENCE_NOTE if len(encoded) > 1 else "")

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            content = await _chat_completion(settings, encoded, frames, prompt)
            result = parse_classification(content)
            if result.approved or result.rejected:
                return result
            # 输出无法解析为有效分类，重试一次。
            last_error = ValueError("VLM 分类输出无法解析")
        except Exception as exc:
            last_error = exc
            LOGGER.warning("偷图分类调用失败（尝试 %d/2）：%s", attempt + 1, exc)
        await asyncio.sleep(0.8 * (attempt + 1))
    if last_error is not None:
        LOGGER.warning("偷图分类最终失败：%s", last_error)
    return Classification()


async def _chat_completion(
    settings: VisionSettings,
    encoded_images: list[str],
    frames: list[bytes],
    prompt: str,
) -> str:
    import httpx

    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{_image_mime_type(frame)};base64,{image}"
                            },
                        }
                        for image, frame in zip(encoded_images, frames)
                    ],
                ],
            },
        ],
        "temperature": 0.1,
        # 与 toolbox 视觉调用同约定：云端 2000 token 上限防 JSON 截断。
        "max_tokens": 2000,
    }
    base_url = settings.base_url.casefold()
    model = settings.model.casefold()
    if "api.deepseek.com" in base_url and model in _THINKING_DISABLED_MODELS:
        # deepseek 系列默认开思考链，会先吃掉输出额度，必须显式关闭。
        payload["thinking"] = {"type": "disabled"}

    headers = {"Authorization": f"Bearer {settings.api_key or 'ollama'}"}
    async with httpx.AsyncClient(timeout=settings.timeout_seconds, headers=headers) as client:
        response = await client.post(
            f"{settings.base_url.rstrip('/')}/chat/completions",
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    choices = body.get("choices") or [{}]
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise ValueError("视觉模型返回空内容")
    return content
