"""高信号文本规则提取（移植自 astrbot_plugin_who_is_who，无平台依赖）。

全部用正则在本地完成：自称呼、第三方绰号、长期请求、AI 接受/拒绝、
用户同意/拒绝。不引入任何额外 LLM 调用。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .models import NormalizedMessage, RuleCandidate

_NOISE_RE = re.compile(r"^(?:哈+|呵+|嗯+|哦+|额+|1+|6+|[?？!！。.、~～]+)$", re.I)
_DURABLE_RE = re.compile(r"(?:以后|今后|接下来|记住|不要再|别再|一直|每次)")
_ACCEPT_RE = re.compile(
    r"(?:好的|好[，,。!！]|明白|知道了|记住了|没问题|可以|我会|我不会|以后会|以后不会)",
    re.I,
)
_REJECT_RE = re.compile(r"(?:不能|不可以|做不到|无法答应|我拒绝|不会照做)")
_REFUSAL_RE = re.compile(r"(?:不要听|别听|不同意|我拒绝|不要这样叫|别这样叫|不许叫|取消)")
_CONSENT_RE = re.compile(r"(?:我同意(?:这个称呼)?|可以这么叫我|就这么叫我|这个称呼可以)")
_SELF_NAME_RE = re.compile(
    r"(?:(?:以后|今后)\s*(?:请)?(?:叫|称呼)我(?:为|叫)?|"
    r"请\s*(?:叫|称呼)我(?:为|叫)?|"
    r"(?:我的?)?(?:绰号|外号|昵称)\s*(?:是|叫|为))"
    r"\s*[“\"'「『]?([^，。！？,\s\]”\"'」』]{1,20})"
)
_AT_MARKER_RE = re.compile(r"\[At:([^\]|]+)\|([^\]]+)\]")
_NICKNAME_SIGNAL_RE = re.compile(
    r"(?:以后|今后|一直|平时|平常|大家都|我们都|群里都|绰号|外号|昵称)"
)
_INVALID_NICKNAMES = {
    "什么", "啥", "谁", "多少", "吗", "呢", "吧", "指定称呼", "这个", "那个",
}


def compact_text(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def is_low_value_message(text: str) -> bool:
    cleaned = re.sub(r"\[[^\]]+\]", "", text or "").strip()
    return not cleaned or bool(_NOISE_RE.fullmatch(cleaned))


def lexical_terms(text: str) -> set[str]:
    text = re.sub(r"\[[^\]]+\]", " ", (text or "").lower())
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    for run in chinese_runs:
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return {word for word in words if len(word) <= 24}


def topic_fingerprint(terms: Iterable[str]) -> str:
    raw = "|".join(sorted(set(terms)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def is_refusal(text: str) -> bool:
    return bool(_REFUSAL_RE.search(text or ""))


def is_consent(text: str) -> bool:
    return bool(_CONSENT_RE.search(text or ""))


def ai_accepted(text: str) -> bool:
    if _REJECT_RE.search(text or ""):
        return False
    return bool(_ACCEPT_RE.search((text or "")[:180]))


def _nickname_after_target(suffix: str) -> str | None:
    # 「叫他/叫她」中的代词不属于称呼本身；原插件会把「他」一并捕获。
    patterns = (
        r"^\s*(?:的)?(?:绰号|外号|昵称)\s*(?:是|叫|为)?\s*[“\"'「『]?([^，。！？,\s\]”\"'」』]{1,20})",
        r"^\s*(?:以后|今后)?\s*(?:就)?(?:叫|称呼(?:为)?|称作|为)\s*(?:他|她|它|TA|ta)?\s*[“\"'「『]?([^，。！？,\s\]”\"'」』]{1,20})",
        r"^\s*[“\"'「『]?([^，。！？,\s\]”\"'」』]{1,20})",
    )
    for pattern in patterns:
        matched = re.search(pattern, suffix)
        if not matched:
            continue
        nickname = matched.group(1).strip()
        if nickname and nickname not in _INVALID_NICKNAMES:
            return nickname
    return None


def extract_rule_candidate(message: NormalizedMessage) -> RuleCandidate | None:
    """只提取高信号、长期性的请求，不额外调用 LLM。"""
    text = compact_text(message.text, 500)

    self_name = _SELF_NAME_RE.search(text)
    if self_name:
        preferred_name = self_name.group(1).strip()
        return RuleCandidate(
            kind="preference",
            key="preferred_name",
            content=f"用户希望被称为“{preferred_name}”",
            issuer_id=message.sender_id,
            target_id=message.sender_id,
            scope="user",
            topic_id=None,
            source_message_id=message.message_id,
            metadata={"value": preferred_name},
        )

    third_party_target = next(
        (uid for uid in message.mentions if uid and uid != message.sender_id),
        None,
    )
    if (
        third_party_target
        and _NICKNAME_SIGNAL_RE.search(text)
        and re.search(r"(?:叫|称呼|称作|绰号|外号|昵称)", text)
    ):
        marker = next(
            (m for m in _AT_MARKER_RE.finditer(text) if m.group(2) == third_party_target),
            None,
        )
        if marker:
            suffix = text[marker.end() :]
        else:
            target_name = message.mention_names.get(third_party_target, "")
            target_index = text.find(target_name) if target_name else -1
            suffix = text[target_index + len(target_name) :] if target_index >= 0 else ""
        nickname = _nickname_after_target(suffix) if suffix else None
        if not nickname:
            return None
        target_label = message.mention_names.get(third_party_target) or "被提及成员"
        return RuleCandidate(
            kind="request",
            key="preferred_name",
            content=f"有人提出将{target_label}称为“{nickname}”",
            issuer_id=message.sender_id,
            target_id=third_party_target,
            scope="user",
            topic_id=None,
            requires_consent=True,
            source_message_id=message.message_id,
            metadata={"value": nickname},
        )

    if not _DURABLE_RE.search(text):
        return None

    scope = "topic"
    key = "behavior"
    if re.search(r"(?:本群|群里|这个群)", text):
        scope = "group"
    if re.search(r"(?:简短|简洁|详细|展开)", text):
        key = "response_detail"
    elif re.search(r"(?:中文|英文|日语|语言)", text):
        key = "response_language"
    elif re.search(r"(?:不要再|别再|不许)", text):
        key = "prohibition"

    return RuleCandidate(
        kind="request",
        key=key,
        content=text,
        issuer_id=message.sender_id,
        target_id=None,
        scope=scope,
        topic_id=message.topic_id if scope == "topic" else None,
        source_message_id=message.message_id,
    )
