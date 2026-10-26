"""QQ 空间生成规则：主人触发的说说与自动评论的提示词与质量闸。

从旧 qzone_automation.rules 精简移植：删掉了定时排期、事实上下文和
内容池相关逻辑（新链路里说说只由主人口令触发、评论即时生成），
保留主题轮换、写作角度/形态、反编造约束和确定性质量闸。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

POST_WRITING_ANGLES = (
    "从一个今天的具体小观察出发，解释它为什么值得注意",
    "对一个看似正确的日常判断提出一个温和但明确的追问",
    "把一个体验拆成原因、代价和更好的选择",
    "用一个生活细节连接到更大的问题，但不要写成空泛鸡汤",
    "比较两种常见做法，给出有依据的取舍观点",
    "从一个小发现推导出一个可实践、可验证的结论",
    "写一个没有标准答案的问题，并先给出自己的暂时答案",
    "把今天的情绪转化成具体的认知，而不是只描述情绪",
    "从人与工具、效率与体验之间的张力切入，保持轻松自然",
    "从一个反常或容易被忽略的细节切入，形成清晰的转折",
    "把一个熟悉的场景换个角度看，说明这个角度改变了什么",
    "留下一个有内容的开放问题，让读者可以分享观点而非只说早安晚安",
)
POST_WRITING_FORMS = (
    "微型叙事：用一个具体场景和一次转折收束，不讲大道理，不以提问结尾",
    "观点短评：先给明确判断，再写一个理由和一个边界条件，不使用鸡汤式升华",
    "小型科普：说清一个可核验事实、背后的机制及它和普通人的关系",
    "抽象乐子：设置一本正经的前提，再用反差或错位制造包袱，不强行总结意义",
    "情感切片：写清触发情绪的细节和真实矛盾，允许留白，不虚构现实亲历",
    "角色对白：用两三句有动作感的对白推进原作相关小片段，保持人物设定",
    "清单或对照：用两到三个短项比较选择，最后给出直接取舍",
    "问题实验：提出一个能验证的问题，写出暂时答案和验证方法，不泛泛而谈",
)
DEFAULT_TOPIC_ROTATION = [
    "日常趣事与小插曲",
    "人生哲理与反常识思考",
    "情感、关系与内心矛盾",
    "抽象乐子与反差幽默",
    "某个领域的前沿新闻与科普",
    "原作剧情、角色对白与世界观",
    "季节天气与身边观察",
    "学习或技术上的小发现",
]


def resolve_timezone(value: Any) -> ZoneInfo:
    from datetime import timedelta, timezone

    try:
        return ZoneInfo(str(value or "Asia/Shanghai"))
    except Exception:
        # Windows 精简 Python 环境可能没有 tzdata；QQ 空间默认使用东八区，
        # 用固定偏移兜底，不能让发布链路因时区库缺失崩溃。
        return timezone(timedelta(hours=8))  # type: ignore[return-value]


def _rotation_items(settings: dict[str, Any], key: str) -> list[str]:
    raw = settings.get(key)
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").replace("，", ",").split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def build_post_prompt(settings: dict[str, Any], now: float) -> str:
    """主人触发的说说提示词：按日期+小时轮换主题/角度/形态，贴合发布时段。"""
    zone = resolve_timezone(settings.get("timezone"))
    local = datetime.fromtimestamp(float(now), zone)
    topics = _rotation_items(settings, "post_topics") or DEFAULT_TOPIC_ROTATION
    hour_seed = local.hour // 3
    topic = topics[(local.toordinal() + hour_seed) % len(topics)]
    angle = POST_WRITING_ANGLES[(local.toordinal() * 5 + hour_seed) % len(POST_WRITING_ANGLES)]
    writing_form = POST_WRITING_FORMS[(local.toordinal() * 3 + hour_seed) % len(POST_WRITING_FORMS)]
    weekday = "一二三四五六日"[local.weekday()]
    recent = [" ".join(str(item).split())[:140] for item in (settings.get("recent_contents") or []) if str(item).strip()][-5:]
    recent_hint = ""
    if recent:
        recent_hint = (
            "近期已经发布过的内容（只用于排重，不要复述）：\n- "
            + "\n- ".join(recent)
            + "\n"
        )
    return (
        "根据当前记忆和情绪，写一条自然、简短、带一点亚托莉口吻的 QQ 空间动态。\n"
        f"发布时间：{local:%Y-%m-%d %H:%M}（星期{weekday}）。"
        "正文必须贴合这个发布时间段：早晨写清晨/起床/晨光等场景，午后写当日进行中的见闻，"
        "夜间写收尾/回顾/夜色等场景；不要把夜间语气原样套到早晨。\n"
        f"本次主题方向：{topic}。本次切入角度：{angle}。本次写作形态：{writing_form}。\n"
        "要求：\n"
        "1. 内容必须包含具体事实、场景、概念、观察或问题之一，并严格服从本次写作形态；"
        "不要每次都以问句或人生道理结尾。\n"
        "2. 禁止只堆阳光、海风、夜色、微笑等意象，禁止早安/晚安/想你/加油式空泛句式，"
        "禁止编造现实经历，不得用『今天看到/窗边发现』等虚构亲历。\n"
        "3. 本次没有取得任何通过来源校验的最新事实。不得声称出现了新论文、新研究、新产品、"
        "官方发布、现实事件或具体数据；涉及概念科普时避免『最新、终于、据说、研究显示』等"
        "暗示现实进展的措辞，也不得用『据报道』伪装有来源。\n"
        "4. 不提及文件路径、代码、接口、密钥等内部细节，不使用「主人」等称呼。\n"
        "5. 控制在 20-120 字，一两句话自然收尾。只输出可直接发布的正文，"
        "不要解释、不要引号。\n"
        f"{recent_hint}"
    )


def build_comment_prompt(feed: dict[str, Any]) -> str:
    """自动/随口评论提示词：只针对这条动态本身，禁止编造私下关系。"""
    author = str(feed.get("name") or "").strip() or "好友"
    content = " ".join(str(feed.get("content") or "").split())[:300]
    return (
        f"你刚在 QQ 空间看到一位好友（昵称：{author}）发布的动态，内容如下：\n"
        f"{content}\n"
        "请以亚托莉的口吻写一条自然的评论区回复。\n"
        "要求：\n"
        "1. 只针对动态内容本身回应，可以轻微幽默或关心，但不要谄媚、不要刷存在感。\n"
        "2. 不编造你与对方的私下经历或未在动态中出现的信息；不确定的事不要装确定。\n"
        "3. 不使用「主人」等称呼；对好友保持平等友好口吻。\n"
        "4. 控制在 1-80 字，像真人评论。只输出评论文本本身，不要解释、不要引号。\n"
    )


def _character_ngrams(value: str, size: int = 3) -> set[str]:
    compact = re.sub(r"[\s，。！？；：、,.!?;:\-—…（）()]", "", str(value or ""))
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def post_quality_issues(content: str, recent_contents: list[str] | None = None) -> list[str]:
    """生成的说说进发布前的确定性闸；问题列表为空表示通过。"""
    text = " ".join(str(content or "").split())
    issues: list[str] = []
    if len(text) < 20:
        issues.append("正文过短，缺少具体观察和观点")
    if len(text) > 120:
        issues.append("正文过长，不适合动态阅读")
    if text and not any(mark in text for mark in "，。！？；："):
        issues.append("缺少清晰的句子推进")
    generic = ("阳光", "海风", "夜色", "微笑", "美好", "治愈", "加油", "想你")
    if sum(text.count(word) for word in generic) >= 3:
        issues.append("意象或套话密度过高")
    if re.search(r"((今天|刚刚|我刚|我在|窗边).{0,12}(看到|发现|遇到)|我看到)", text):
        issues.append("包含未经事实依据支持的虚构亲历")
    if re.search(
        r"(据.{0,8}(报道|消息|说)|官方.{0,8}(发布|宣布)|研究.{0,8}(显示|发现|证明|实现)|"
        r"论文.{0,10}(说|显示|发现|提出|证明)|实验室.{0,12}(新发|发布|实现|完成)|数据显示)",
        text,
    ):
        issues.append("包含没有可靠来源的现实事件陈述")
    if "主人" in text:
        issues.append("包含「主人」称呼")
    recent = [" ".join(str(item).split()) for item in (recent_contents or []) if str(item).strip()]
    if text and any(text == item or (len(text) >= 24 and text[:24] == item[:24]) for item in recent[-5:]):
        issues.append("与近期内容重复")
    text_grams = _character_ngrams(text)
    for item in recent[-5:]:
        item_grams = _character_ngrams(item)
        union = text_grams | item_grams
        if union and len(text_grams & item_grams) / len(union) >= 0.58:
            issues.append("与近期内容表述高度相似")
            break
    return issues


def comment_quality_issues(content: str) -> list[str]:
    """生成的评论发布前的确定性闸。"""
    text = " ".join(str(content or "").split())
    issues: list[str] = []
    if not text:
        issues.append("评论内容为空")
    if len(text) > 200:
        issues.append("评论过长")
    if re.search(r"https?://\S+", text):
        issues.append("评论包含链接")
    if "主人" in text:
        issues.append("评论包含「主人」称呼")
    return issues
