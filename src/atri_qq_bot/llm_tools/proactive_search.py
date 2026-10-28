"""每轮对话的主动联网检索。

模型侧 `search_web` 工具依赖模型自发判断，实测常出现"等用户说搜一下才搜"的
欠触发。这里改为确定性主动搜索：每条进入模型的消息先在系统侧执行一次联网
检索，把结果作为背景证据注入上下文，模型只负责参考或忽略，不再负责
"要不要搜"的决策。
"""

from __future__ import annotations

import re
from typing import Any

from .web_search_tool import search_web


# 纯问候/纯语气消息不值得占用一次搜索（博查按次计费、RSS 也有延迟），
# 全等匹配保证不误伤包含实际内容的句子。
_TRIVIAL_MESSAGES = {
    "早上好",
    "早安",
    "中午好",
    "午安",
    "下午好",
    "晚上好",
    "晚安",
    "晚安玛",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "hello",
    "hi",
    "在吗",
    "在么",
    "好的",
    "好呀",
    "好吧",
    "嗯嗯",
    "嗯呐",
    "ok",
    "okay",
    "拜拜",
    "再见",
    "抱抱",
    "摸摸头",
    "辛苦了",
    "爱你",
    "想你",
    "好累",
    "好困",
    "好饿",
    "气死了",
    "烦死了",
    "emo了",
    "破防了",
}

# 重复字符的纯情绪表达（哈哈/嘿嘿/呜呜/……）。
_TRIVIAL_REPEAT_RE = re.compile(r"^(?:哈|嘿|嘻|呵|呜|唉|嘿咻|啊){2,}[。！!～~呀啊嘛呢哦了]*$")

# 构造检索词时剥掉的口语引导/应答语，只留话题实体。
_QUERY_FILLER_RE = re.compile(
    r"(你知道|你听说过|你听说|你听过|你了解|你认识|给我讲讲|给我说说|"
    r"跟我说说|和我说说|讲讲|说说|告诉我|我想知道|我想问|我问你|问一下|"
    r"问下|你觉得|你认为|你说|帮我看看|帮我查查|帮我查一下|帮我查|"
    r"查一下|搜一下|搜索一下|查查|请问|请告诉我|我好奇|好奇|"
    r"能不能|能不能给我|可以给我|给我看看|给我)"
)

# 句尾语气助词；对检索词只有噪音价值。
_TAIL_PARTICLE_RE = re.compile(r"[吗嘛么呢吧啊呀哦喔咯喽了啦]+$")

# 注入上下文时的固定框架，压住两类回归：把注入资料硬扯进闲聊，
# 以及对已注入的相同内容再次调用 search_web。
_EVIDENCE_HEADER = (
    "联网检索（系统已主动执行）：系统为本轮消息自动完成了联网搜索，"
    "下面的资料由系统直接提供。与当前消息相关时可作为事实依据，"
    "引用时保持来源和发布时间意识；与闲聊无关时直接忽略，"
    "不要把无关结果硬扯进回复，也不要向用户描述\"我去搜索了\"的过程。"
    "除本资料外仍可按需调用 search_web 检索更具体的内容，"
    "但不要用相同关键词重复检索已注入的资料。"
)

# 检索词上限：过长的原句对搜索引擎是噪音。
_MAX_QUERY_CHARS = 40


def _is_trivial_message(text: str) -> bool:
    stripped = re.sub(r"[\s。！!？?～~，,、.]+", "", text)
    if not stripped:
        return True
    if stripped.casefold() in _TRIVIAL_MESSAGES:
        return True
    return bool(_TRIVIAL_REPEAT_RE.match(stripped))


def build_proactive_query(user_text: str) -> str:
    """从用户消息构造检索词：剥口语引导与句尾助词，超长时截断。"""

    cleaned = _QUERY_FILLER_RE.sub("", str(user_text or "").strip())
    # 先剥外层标点再剥助词，否则“……发布呢？”的问号会挡住“呢”。
    cleaned = cleaned.strip(" ，。？！,.?!～~\t\r\n")
    cleaned = _TAIL_PARTICLE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。？！,.?!")
    if not cleaned:
        cleaned = str(user_text or "").strip()
    return cleaned[:_MAX_QUERY_CHARS]


def proactive_search_gate(user_text: str, config: Any) -> bool:
    """判断本轮是否值得主动搜索；返回 False 时完全跳过。"""

    if not bool(getattr(config, "web_search_enabled", True)):
        return False
    if not bool(getattr(config, "web_search_proactive_enabled", True)):
        return False
    text = str(user_text or "").strip()
    if not text:
        return False
    # 太短的消息没有可检索的话题实体。
    if len(text) < 4:
        return False
    if _is_trivial_message(text):
        return False
    return True


async def proactive_search_evidence(user_text: str, config: Any) -> str | None:
    """执行一次主动检索并返回带框架的注入文本；不适用时返回 None。

    检索失败不抛异常：search_web 自身返回中文失败说明，注入后模型会
    明白本轮没有可靠外部资料，走正常聊天路径。
    """

    if not proactive_search_gate(user_text, config):
        return None
    query = build_proactive_query(user_text)
    if not query.strip():
        return None
    max_results = max(1, min(3, int(getattr(config, "web_search_max_results", 5) or 5)))
    result = await search_web({"query": query, "mode": "auto", "max_results": max_results}, config)
    return f"{_EVIDENCE_HEADER}\n\n{result}"
