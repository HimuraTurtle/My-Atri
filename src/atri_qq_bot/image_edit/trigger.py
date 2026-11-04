"""对称修图的触发词匹配：判断一句话是不是在请求「左半镜像对称」。

匹配契约：只认以「对称 / 镜像」结尾的祈使表达；否定式（不要/别/不用）
和反方向（右对称、上下对称）不触发。前置的称呼与 @昵称、尾部的语气词
与标点、动作后缀（一下/处理）都容忍。是否有可用图片由调用方另行判断
——纯文本「这构图好对称」也会命中本函数，但没有图片时命令层放行给
聊天模型，不在这里拦截。
"""

from __future__ import annotations

import re

from ..bilibili.parsing import strip_address_prefix

# 群聊手打的「@亚托莉」等 @昵称 前缀；与屏幕看护指令共用同一剥离策略。
_AT_NICK_RE = re.compile(r"^@\S{1,24}[\s，。,、；;！!？?~～]+")
_TAIL_TRIM_RE = re.compile(r"[?？!！。.，,、~～呀呢啊吧哦啦呗嘛吗\s]+$")
_ACTION_SUFFIXES = ("一下", "处理")

_NEGATION_TAILS = ("不要", "别", "不用", "先不", "无需")
# 反方向/垂直对称是另一套操作，本功能只做「左半镜像覆盖右半」。
_DIRECTION_EXCLUDES = ("右", "右边", "上下", "垂直", "水平")


def _normalize(text: str) -> str:
    stripped = _AT_NICK_RE.sub("", (text or "").strip(), count=1)
    stripped = strip_address_prefix(stripped)
    stripped = _TAIL_TRIM_RE.sub("", stripped)
    while True:
        for suffix in _ACTION_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)]
                break
        else:
            break
    return "".join(stripped.split())


def is_symmetry_request(text: str) -> bool:
    """判断规范化后的文本是否为左对称祈使（含「左右对称」这类全称说法）。"""
    normalized = _normalize(text)
    if len(normalized) < 2:
        return False

    for verb in ("对称", "镜像"):
        if not normalized.endswith(verb):
            continue
        prefix = normalized[: -len(verb)]
        if prefix.endswith(_NEGATION_TAILS):
            return False
        # 「左右」是全称说法，指的正是水平镜像对称，不排除。
        if prefix.endswith("左右"):
            return True
        if prefix.endswith(_DIRECTION_EXCLUDES):
            return False
        return True
    return False
