"""围观会话：主人做一件事时，ATRI 持续看屏并向该会话发送围观短评。

节奏为「混合」：帧差分确认画面显著变化才评论（confirm_frames 滤抖动），
且两次评论至少间隔 min_interval 秒——间隔约束由 FrameTrigger 的冷却
机制天然承担。评论只发到开启围观的那个会话；结束由主人话语触发
（宽松规则，宁可误停也不刷屏）。

所有对外回应（开启宣告、结束告别、围观短评、失败收工）都由主聊天
模型结合人格与记忆背景生成，不写死模板。称呼是账号级的：私聊围观用
该账号自己档案里设定的称呼（换账号换称呼，无全局称呼），群聊围观不
带入私聊专属称呼；任何场景都禁止使用「主人」一词。LLM 失败时退回
不含具体称呼的简短兜底，绝不编造。

降级契约：截图/视觉失败只累计，连续超限自动收工并坦诚告知；
评论生成失败本轮静默跳过；主动开启分类失败静默放弃，绝不编造。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .capture import capture_screen, encode_jpeg, gray_small
from .privacy import active_window_title, is_sensitive_title
from .trigger import FrameTrigger
from .vision import describe_screen

LOGGER = logging.getLogger("atri.screen_watch")

_COMMENT_MAX_TOKENS = 1200
_SHORT_REPLY_MAX_TOKENS = 400
_CLASSIFY_MAX_TOKENS = 800
_LLM_TIMEOUT_SECONDS = 12.0

# 称呼硬约束：场景化规则（私聊用账号档案称呼/群聊平等口吻）由 server 层
# 随 persona_system 注入，这里只保留通用底线——不编造、绝不叫「主人」。
_ADDRESS_RULE = (
    "称呼按 system 消息里的称呼规则执行，不要编造新称呼。"
    "绝对禁止在输出中出现「主人」这个词。"
)

_COMMENT_PROMPT = (
    "对方正在{activity}，你正在围观对方的屏幕，当前看到的画面：{description}。"
    "请以亚托莉的口吻发一条 40~80 字的围观短评：可以好奇、吐槽、鼓励，"
    "或点评画面里的细节，但必须基于画面描述，不许编造画面里没有的内容。"
    "直接输出评论文本，不要任何前缀、引号或解释。"
)

_OPEN_PROMPT = (
    "对方让你围观他做一件事，活动：{activity}。{replaced_clause}"
    "请以亚托莉的口吻发一条 20~50 字的回应，表示你要开始围观了："
    "可以好奇、期待、跃跃欲试，或带一点小动作描写。"
    "直接输出回应文本，不要前缀、引号或解释。"
)

_CLOSE_PROMPT = (
    "对方宣布结束了，你不再围观（围观的活动：{activity}）。"
    "围观期间你一共插了 {comment_count} 句嘴。"
    "请以亚托莉的口吻发一条 20~60 字的收工告别："
    "可以总结、调侃或心疼对方的辛苦。"
    "直接输出回应文本，不要前缀、引号或解释。"
)

_FAREWELL_PROMPT = (
    "你连续 {failures} 次想看对方的屏幕都失败了（截图或识别一直出问题），"
    "围观不得不提前收工。"
    "请以亚托莉的口吻发一条 20~60 字的坦诚收工短消息："
    "说明自己看不清屏幕、围观先收工，语气自然带点懊恼；"
    "如果对方可能在玩全屏游戏或播放器，可以顺带建议切成无边框窗口或休息时再叫你围观。"
    "不许编造任何画面内容。直接输出回应文本，不要前缀、引号或解释。"
)

_LOOK_REPLY_PROMPT = (
    "对方问自己电脑现在在干嘛，你刚凑过去看了一眼屏幕，"
    "看到的画面是：{summary}。"
    "请以亚托莉的口吻用 30~90 字回答对方的电脑正在做什么："
    "顺着画面描述说清楚就行，可以好奇、点评或吐槽一两句，"
    "但必须基于画面描述，不许编造画面里没有的内容。"
    "直接输出回答文本，不要前缀、引号或解释。"
)

# LLM 失败时的兜底：简短、不含任何预设称呼，绝不出现「主人」。
_OPEN_FALLBACK = "好，那我就看着你{activity}啦，有动静我就吱声~"
_OPEN_FALLBACK_REPLACED = "（收起刚才的围观）好，那我换看{activity}啦，有动静我就吱声~"
_CLOSE_FALLBACK = "好哦，那我先不看啦。{activity}辛苦了，去休息一下吧~"
_FAREWELL_FALLBACK = "呜……我这边一直看不清屏幕，围观先收工啦，等下再叫我~"

_CLASSIFY_PROMPT = (
    "判断下面这条来自机器人主人的消息是否是「宣告马上/正在开始做某个活动」。"
    "只输出一行 JSON，不要输出任何其他内容，格式："
    '{{"announce": true, "activity": "写毕业论文", "watchable": true}}。'
    "字段含义：announce=是否为活动宣告（「要去做」「正在做」「开始做」都算）；"
    "activity=活动简短名称（4~12 字，如「写毕业论文」「打游戏」「学Python」），"
    "不宣告时为空串；watchable=该活动是否主要在电脑/屏幕前进行"
    "（睡觉、洗澡、出门、吃饭等不算）。"
    "消息：{text}"
)

# 围观结束信号（宽松白名单：误停成本低——主人再说一次「看着我xx」即可重开，
# 漏停的代价是持续刷屏，所以宁可误停）。
_END_PHRASES = (
    "别看了",
    "不看了",
    "不用看了",
    "不围观了",
    "收工",
    "下班",
    "完工",
    "完事",
    "搞定了",
    "告一段落",
    "到此为止",
    "大功告成",
    "完结撒花",
    "通关",
    "写完了",
    "做完了",
    "弄完了",
    "学完了",
    "看完了",
    "肝完了",
    "画完了",
    "改完了",
    "修好了",
    "调通了",
    "跑通了",
    "交稿了",
    "交卷了",
    "睡觉了",
    "睡了",
    "洗澡去了",
    "吃饭去了",
    "干饭去了",
    "出门了",
    "上课去了",
    "休息了",
    "摆烂了",
    "摆了",
    "不弄了",
    "不写了",
    "不搞了",
    "不做了",
    "不肝了",
    "不学了",
    "不画了",
    "不玩了",
    "不打了",
    "不修了",
    "不改了",
    "先这样",
    "差不多了",
)
# 「不写了」类：否定词 +（0~3 字）+ 动词 + 语气词。
_END_NEGATION_RE = re.compile(
    r"(?:不|别|先不|不用|不想|不再|懒得|没法|没空|没心情)"
    r"[\u4e00-\u9fa5]{0,3}"
    r"(?:做|弄|写|学|玩|打|看|画|肝|搞|修|改|编|盯|围)"
    r"(?:了|啦|咯|呢|哈|啊|嘞)"
)
# 「不写论文了」类：否定词 + 动词 +（1~4 字宾语）+ 完成/语气词。
_END_QUIT_RE = re.compile(
    r"(?:不|别|先不|不用)(?:做|弄|写|学|玩|打|看|画|肝|搞|修|改|编|追)"
    r"[\u4e00-\u9fa5]{1,4}(?:了|啦|咯|嘞)"
)

# 活动宣告预筛：值得花一次 LLM 分类的最低门槛（含冷却兜底，宁松勿紧）。
_ANNOUNCE_PRECHECK_RE = re.compile(
    r"(?:我要|我去|我准备|我打算|我来|我开始|开始|准备|打算|要去|得去|想去|该|得)"
    r"[\u4e00-\u9fa5]{0,4}"
    r"(?:做|弄|写|学|玩|打|画|肝|搞|修|改|编|看|开)"
)


def _chat_completion(
    config: Any, system_texts: list[str], user_prompt: str, *, max_tokens: int
) -> str:
    """主聊天配置（openai_*）的极简补全调用；失败返回空串（不编造）。

    system_texts 为人格/记忆等分层 system 消息；为空时兜底一条最小身份
    声明，保证模型仍以亚托莉的口吻输出。
    """
    base_url = str(getattr(config, "openai_base_url", "") or "").rstrip("/")
    model = str(getattr(config, "openai_model", "") or "")
    api_key = str(getattr(config, "openai_api_key", "") or "")
    if not (base_url and model and api_key):
        LOGGER.warning("围观回应/分类配置不完整（openai_base_url/model/api_key），跳过")
        return ""
    systems = [str(text) for text in (system_texts or []) if str(text or "").strip()]
    if not systems:
        systems = ["你是机器人少女亚托莉（ATRI），性格活泼、有点傲娇的机器人少女。"]
    messages = [{"role": "system", "content": text} for text in systems]
    messages.append({"role": "user", "content": user_prompt})
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens}).encode()
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_LLM_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        LOGGER.warning("围观回应/分类 LLM 调用失败：%s", exc)
        return ""
    return str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def _clean_reply(text: str, max_chars: int) -> str:
    """清洗模型输出：剥引号、限长度；含「主人」一律拒绝（称呼硬约束）。

    提示词已禁止「主人」，这里做最后兜底：评论被拒则本轮静默跳过，
    宣告/告别被拒则由调用方退回不含称呼的兜底文本。
    """
    text = str(text or "").strip().strip("\"'“”").strip()
    if not text or len(text) > max_chars:
        return ""
    if "主人" in text:
        LOGGER.warning("围观回应含「主人」，已拒绝：%s", text[:60])
        return ""
    return text


def generate_commentary(
    config: Any,
    activity: str,
    description: str,
    persona_system: list[str],
    last_comment: str = "",
) -> str:
    """把屏幕描述包装成 ATRI 口吻的围观短评；失败返回空串（本轮静默跳过）。"""
    prompt = _COMMENT_PROMPT.format(activity=activity, description=description)
    if last_comment:
        prompt += f"你上一条评论是：「{last_comment}」，这次换个角度说。"
    prompt += _ADDRESS_RULE
    text = _chat_completion(
        config, persona_system, prompt, max_tokens=_COMMENT_MAX_TOKENS
    )
    return _clean_reply(text, 200)


def generate_opening_reply(
    config: Any,
    activity: str,
    persona_system: list[str],
    *,
    replaced: bool = False,
) -> str:
    """围观开启宣告：模型生成；LLM 失败退回不含称呼的简短兜底。"""
    replaced_clause = (
        "你刚才还在围观另一件事，对方现在换了活动，"
        "请自然地收起旧围观再迎接新活动。"
        if replaced
        else ""
    )
    prompt = _OPEN_PROMPT.format(
        activity=activity, replaced_clause=replaced_clause
    ) + _ADDRESS_RULE
    text = _chat_completion(
        config, persona_system, prompt, max_tokens=_SHORT_REPLY_MAX_TOKENS
    )
    fallback = (
        _OPEN_FALLBACK_REPLACED if replaced else _OPEN_FALLBACK
    ).format(activity=activity)
    return _clean_reply(text, 120) or fallback


def generate_closing_reply(
    config: Any, activity: str, comment_count: int, persona_system: list[str]
) -> str:
    """围观结束告别：模型生成；LLM 失败退回不含称呼的简短兜底。"""
    prompt = _CLOSE_PROMPT.format(
        activity=activity, comment_count=comment_count
    ) + _ADDRESS_RULE
    text = _chat_completion(
        config, persona_system, prompt, max_tokens=_SHORT_REPLY_MAX_TOKENS
    )
    return _clean_reply(text, 160) or _CLOSE_FALLBACK.format(activity=activity)


def generate_failure_farewell(
    config: Any, failures: int, persona_system: list[str]
) -> str:
    """连续失败收工告白：模型生成；LLM 失败退回不含称呼的简短兜底。"""
    prompt = _FAREWELL_PROMPT.format(failures=failures) + _ADDRESS_RULE
    text = _chat_completion(
        config, persona_system, prompt, max_tokens=_SHORT_REPLY_MAX_TOKENS
    )
    return _clean_reply(text, 160) or _FAREWELL_FALLBACK


def generate_look_reply(config: Any, summary: str, persona_system: list[str]) -> str:
    """「爸爸在干嘛」类指令的口语回答：把视觉描述转成亚托莉口吻。

    模型生成（与围观评论同构：persona_system 注入账号记忆与称呼规则），
    失败或被清洗拒绝时返回空串，由调用方退回固定文案兜底。
    """
    prompt = _LOOK_REPLY_PROMPT.format(summary=str(summary or "")) + _ADDRESS_RULE
    text = _chat_completion(
        config, persona_system, prompt, max_tokens=_COMMENT_MAX_TOKENS
    )
    return _clean_reply(text, 240)


def should_end_party(text: str) -> bool:
    """主人的话语是否宣告结束围观；宽松规则，误停成本低于漏停成本。"""
    normalized = str(text or "").replace(" ", "")
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _END_PHRASES):
        return True
    return bool(_END_NEGATION_RE.search(normalized) or _END_QUIT_RE.search(normalized))


def looks_like_activity_announce(text: str) -> bool:
    """主动围观通道的预筛：值得花一次 LLM 分类的最低门槛。"""
    normalized = str(text or "").replace(" ", "")
    if not normalized or len(normalized) > 40:
        return False
    return bool(_ANNOUNCE_PRECHECK_RE.search(normalized))


def classify_activity(text: str, config: Any) -> dict[str, Any] | None:
    """LLM 判定消息是否为可围观的活动宣告；失败/超时返回 None（静默放弃）。

    纯分类任务，不注入人格与记忆，也不走称呼规则。
    """
    raw = _chat_completion(
        config,
        [],
        _CLASSIFY_PROMPT.format(text=str(text or "")),
        max_tokens=_CLASSIFY_MAX_TOKENS,
    )
    match = re.search(r"\{[^{}]*\}", raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    activity = str(data.get("activity") or "").strip()
    return {
        "announce": bool(data.get("announce")),
        "watchable": bool(data.get("watchable")),
        "activity": activity[:24],
    }


class WatchParty:
    """单会话围观状态机：tick 由 server 层的后台循环驱动。"""

    def __init__(
        self,
        config: Any,
        activity: str,
        event: dict[str, Any],
        conversation_id: str,
        *,
        persona_system: list[str] | None = None,
        trigger: FrameTrigger | None = None,
    ) -> None:
        self.config = config
        self.activity = str(activity or "").strip()
        self.event = dict(event or {})
        self.conversation_id = str(conversation_id or "")
        # 回应生成所需的人格与记忆背景（server 层渲染一次，围观期间复用）。
        self.persona_system = [str(text) for text in (persona_system or [])]
        self.started_at = time.time()
        self.last_comment_at = 0.0
        self.last_comment_text = ""
        self.comment_count = 0
        self.consecutive_failures = 0
        min_interval = max(
            30.0, float(getattr(config, "screen_watch_party_min_interval_seconds", 180.0) or 180.0)
        )
        self.trigger = trigger or FrameTrigger(
            threshold=float(getattr(config, "screen_watch_diff_threshold", 8.0) or 8.0),
            confirm_frames=2,
            cooldown=min_interval,
            backoff_max=max(360.0, min_interval * 2.0),
        )

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """执行一次围观采样，返回 comment/sensitive/skipped/failed_stop 等事件。"""
        now = time.time() if now is None else now
        image = capture_screen()
        if image is None:
            self.consecutive_failures += 1
            return self._failure_result()
        if not self.trigger.feed(gray_small(image), now):
            return {"event": "idle"}
        title = active_window_title()
        if is_sensitive_title(title):
            self.trigger.reset_baseline()
            return {"event": "sensitive", "window_title": title}
        description = describe_screen(encode_jpeg(image), self.config)
        self.trigger.reset_baseline()
        if not description:
            self.consecutive_failures += 1
            return self._failure_result()
        text = generate_commentary(
            self.config,
            self.activity,
            description,
            self.persona_system,
            self.last_comment_text,
        )
        if not text:
            # 评论生成失败本轮静默跳过；视觉是硬能力故不计入失败。
            return {"event": "comment_failed"}
        self.consecutive_failures = 0
        self.last_comment_at = now
        self.last_comment_text = text
        self.comment_count += 1
        return {"event": "comment", "text": text, "description": description}

    def _failure_result(self) -> dict[str, Any]:
        limit = max(1, int(getattr(self.config, "screen_watch_party_max_failures", 5) or 5))
        if self.consecutive_failures >= limit:
            return {"event": "failed_stop", "failures": self.consecutive_failures}
        return {"event": "skipped"}
