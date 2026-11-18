"""AI 味检测：识别模型自发形成的固定句式、口头禅与骑墙话。

数据来源：logs/reply-events.jsonl 中 5735 条真实回复的频次审计。
这些句式不在任何提示词里，是推理模型 RLHF 默认腔的自发口头禅，
因此除了提示词层面的封禁，还需要运行时检测并触发质量重试。
"""

from __future__ import annotations

import re

# 实测高频口头禅/固定句式（出现即判 AI 味，触发重试）。
# “高性能”是原作人设梗，允许低频使用，不进黑名单；
# 其收尾复读由跨轮句式账本去重。
AI_FLAVOR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"我记下(来)?了"), "「我记下了」式口头禅"),
    (re.compile(r"记住了记住了"), "「记住了记住了」叠词确认"),
    (re.compile(r"我听着呢"), "「我听着呢」"),
    (re.compile(r"我在这儿呢"), "「我在这儿呢」"),
    (re.compile(r"嘿嘿，被夸了"), "「嘿嘿，被夸了」固定开头"),
    (re.compile(r"我帮你认(一)?认"), "「我帮你认认」"),
    (re.compile(r"我认[，,].{0,12}锅"), "「xx我认，xx的锅我不背」句式"),
    (re.compile(r"那当然，我可是"), "「那当然，我可是」固定开头"),
)

# 骑墙话：A/B 比较、口味询问时两边讨好、拒绝表态。
FENCE_SITTING_RE = re.compile(
    r"各有千秋"
    r"|各有各的"
    r"|萝卜青菜"
    r"|因人而异"
    r"|见仁见智"
    r"|看你个人"
    r"|看你喜欢"
    r"|看你想要"
    r"|取决于你"
    r"|没有绝对的"
    r"|各有所长"
    r"|各擅胜场"
    r"|都(很)?不错[，,。]?(不过|但是|看你)"
)

# 提取句式指纹的最短行长：太短的行（“嗯。”“哦”）高频属正常，不记账。
FINGERPRINT_MIN_LINE = 8
FINGERPRINT_SIZE = 6


def detect_ai_flavor(text: str) -> str | None:
    """返回 AI 味违规描述；None 表示合格。"""
    sample = str(text or "")
    if not sample.strip():
        return None
    for pattern, label in AI_FLAVOR_RULES:
        if pattern.search(sample):
            return label
    if FENCE_SITTING_RE.search(sample):
        return "骑墙话（各有千秋/看你喜好这类两边讨好的说法），要给出自己的明确偏好"
    return None


def extract_phrase_fingerprints(reply: str) -> list[tuple[str, str]]:
    """提取回复每行的 (开头, 收尾) 指纹，用于跨轮去重。

    只取长度达标的行为，避免“嗯”“哦”这类天然高频短行污染账本。
    """
    fingerprints: list[tuple[str, str]] = []
    for raw_line in str(reply or "").splitlines():
        line = raw_line.strip()
        if len(line) < FINGERPRINT_MIN_LINE:
            continue
        head = line[:FINGERPRINT_SIZE]
        tail = line[-FINGERPRINT_SIZE:]
        fingerprints.append((head, tail))
    return fingerprints
