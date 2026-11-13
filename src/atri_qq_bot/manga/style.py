"""漫画反馈的人设润色：文字反馈发出前用聊天模型按 ATRI 角色卡加工。

用户要求漫画功能的回应每次都符合角色卡——固定文案念多了就出戏。
这里只润色**反馈类文字**（未找到、服务不可用等）；结构化输出
（📖 章节头、搜索结果编号列表、图片）不走模型，改坏格式会破坏
「漫画 阅读 N M」的编号链路。模型失败/超时/输出异常时优雅降级
回原始文案，反馈永远送达。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .alias import call_chat_model

LOGGER = logging.getLogger("atri.manga")

_STYLED_MAX_LEN = 200
_FALLBACK_PERSONA = (
    "你是 ATRI：高性能机器人少女，我行我素又有点小恶魔，说话口语化、"
    "短句、不用客服腔，对主人坦率亲近。失败时软着说并给个台阶，"
    "但关键信息（命令写法、原因）必须保留。"
)

_persona_cache: str | None = None

_STYLE_INSTRUCTION = (
    "接下来给你一条将要通过 QQ 发给用户的漫画功能反馈文字。"
    "请把它改写成 ATRI 的口吻：\n"
    "1. 保留全部关键信息（尤其是提到的命令写法、原因、数字）\n"
    "2. 保持简短，1-2 句，口语化\n"
    "3. 符合角色卡的说话方式\n"
    "只输出改写后的文字本身，不要解释、不要引号、不要markdown。"
)


def _load_persona_text() -> str:
    """加载角色卡（atri_persona.md + speech_style.md），失败用内置简版。"""
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache
    root = Path(__file__).resolve().parents[3]
    parts: list[str] = []
    for name in ("atri_persona.md", "speech_style.md"):
        try:
            parts.append((root / "docs" / "prompts" / name).read_text(encoding="utf-8"))
        except OSError:
            LOGGER.warning("角色卡文件缺失，润色用内置简版人设：%s", name)
    if not parts:
        _persona_cache = _FALLBACK_PERSONA
    else:
        _persona_cache = "\n\n".join(parts)
    return _persona_cache


def _clean_styled(text: str) -> str:
    styled = str(text or "").strip()
    # 模型偶尔会给整段包引号（含非对称的「」“”）
    pairs = {'"': '"', "'": "'", "「": "」", "“": "”"}
    if len(styled) >= 2 and styled[0] in pairs and styled[-1] == pairs[styled[0]]:
        styled = styled[1:-1].strip()
    return styled


async def style_feedback(config: Any, text: str) -> str:
    """把反馈文字按 ATRI 角色卡润色；任何失败都返回原文。"""
    text = str(text or "").strip()
    if not text:
        return text
    try:
        raw = await call_chat_model(
            config, _load_persona_text(), f"{_STYLE_INSTRUCTION}\n\n反馈内容：{text}",
            temperature=0.8,
        )
        styled = _clean_styled(raw)
        # 护栏：空、超长（模型跑题兜长篇）、与原文完全相同都直接用原文
        if not styled or len(styled) > _STYLED_MAX_LEN or styled == text:
            return text
        return styled
    except Exception as exc:
        LOGGER.warning("反馈润色失败（按原文发送）：%s", exc)
        return text
