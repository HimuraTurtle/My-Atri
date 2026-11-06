"""对称修图的触发词匹配：判断一句话是不是在请求「半区镜像对称」，并解析方向。

匹配契约：只认以「对称 / 镜像」结尾的祈使表达；否定式（不要/别/不用，
无论否定词在方向词前还是紧贴动词）不触发。前置的称呼与 @昵称、尾部的
语气词与标点、动作后缀（一下/处理）都容忍。方向词取动词前的结尾词：

- 「左 / 左边 / 左半 / 左右」→ left（「左右对称」是水平镜像的全称说法）；
- 「右 / 右边 / 右半」→ right；
- 「上 / 上面 / 上半 / 上下」→ top（「上下对称」是垂直镜像的全称说法）；
- 「下 / 下面 / 下半」→ bottom；
- 无方向词 → left（历史缺省）。

「垂直 / 水平对称」口语歧义太大（垂直对称轴还是垂直方向翻转让两派
人吵不出结果），维持不触发放行给聊天模型——误触发比放行更有害。
是否有可用图片由调用方另行判断——纯文本「这构图好对称」也会命中，
但没有图片时命令层放行给聊天模型，不在这里拦截。
"""

from __future__ import annotations

import re

from ..bilibili.parsing import strip_address_prefix

# 群聊手打的「@亚托莉」等 @昵称 前缀；与屏幕看护指令共用同一剥离策略。
_AT_NICK_RE = re.compile(r"^@\S{1,24}[\s，。,、；;！!？?~～]+")
_TAIL_TRIM_RE = re.compile(r"[?？!！。.，,、~～呀呢啊吧哦啦呗嘛吗\s]+$")
_ACTION_SUFFIXES = ("一下", "处理")

_NEGATION_TAILS = ("不要", "别", "不用", "先不", "无需")
# 口语歧义方向：垂直/水平对称不猜，放行给聊天模型。
_AMBIGUOUS_TAILS = ("垂直", "水平")

# 方向别名 → 规范方向。匹配时按长度降序尝试：「左右」必须先于「右」，
# 否则「左右对称」会被读成 right。
_DIRECTION_ALIASES: list[tuple[str, str]] = sorted(
    (
        ("左", "left"),
        ("左边", "left"),
        ("左面", "left"),
        ("左半", "left"),
        ("右", "right"),
        ("右边", "right"),
        ("右面", "right"),
        ("右半", "right"),
        ("上", "top"),
        ("上面", "top"),
        ("上边", "top"),
        ("上半", "top"),
        ("下", "bottom"),
        ("下面", "bottom"),
        ("下边", "bottom"),
        ("下半", "bottom"),
        ("左右", "left"),
        ("上下", "top"),
    ),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def _is_negated(remainder: str) -> bool:
    """方向词/动词之前的剩余前缀是否以否定词收尾（不要/别/先不……）。"""
    return remainder.endswith(_NEGATION_TAILS)


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


def parse_symmetry_request(text: str) -> str | None:
    """解析对称祈使的方向：left/right/top/bottom，不是请求则返回 None。"""
    normalized = _normalize(text)
    if len(normalized) < 2:
        return None

    for verb in ("对称", "镜像"):
        if not normalized.endswith(verb):
            continue
        prefix = normalized[: -len(verb)]
        if _is_negated(prefix):
            return None
        for alias, direction in _DIRECTION_ALIASES:
            if prefix.endswith(alias):
                # 否定词可能出现在方向词之前（「不要右边对称」）：
                # 剥掉方向词后对剩余部分再查一次。
                if _is_negated(prefix[: -len(alias)]):
                    return None
                return direction
        if prefix.endswith(_AMBIGUOUS_TAILS):
            return None
        return "left"
    return None


def is_symmetry_request(text: str) -> bool:
    """判断规范化后的文本是否为对称祈使（任意方向）。"""
    return parse_symmetry_request(text) is not None
