"""群聊观察：从最近群聊消息里提取高频表达，作为热梗学习的第二数据源。

只做只读统计：读 ``users.json`` 的群聊 history，按停用词过滤后统计高频
token，供 LLM 精选判定。目标是从"群友实际在说什么"里发现正在流行的梗，
而不是只依赖公共热搜榜。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from ..runtime.paths import PROJECT_ROOT
from .store import _normalise_term, load_meme_store


LOGGER = logging.getLogger("atri.memes.observe")

_DEFAULT_MEMORY_PATH = PROJECT_ROOT / "data" / "memory" / "users.json"

_WINDOW_HOURS = 12.0
_MIN_COUNT = 2
_MAX_WORDS = 20
_MAX_TOKEN_LEN = 10

# 消息里切出来的常见功能词、语气词和聊天套话；出现再多次也不是梗。
_STOPWORDS = frozenset(
    """
    的 了 是 我 你 他 她 它 我们 你们 他们 她们 它们 这 那 这个 那个 这些 那些
    就 都 也 还 又 再 才 在 和 跟 与 对 被 把 给 让 向 从 到 为 什么 怎么 怎样
    啊 吧 呢 吗 嘛 哦 哈 嘿 唉 嗯 呃 欸 呀 罢了 而已 的话 就是 也就是 不是
    一个 一些 有点 有点儿 没有 没了 别 不要 不用 会 能 可以 要 想 觉得 知道
    说 说得 去 来 来了 回 现在 今天 昨天 明天 时候 什么意思 怎么了 干嘛
    哈哈 哈哈哈 哈哈哈哈哈 笑死 笑晕 无语 服了 绷不住 真的 确实 反正 其实
    老师 兄弟 姐妹 老哥 大佬 朋友 大家 有人 没人 有人吗 在吗 在不在
    谢谢 不谢 没事 没事儿 好的 好的好的 好行 可以吗 行吧 那行 好耶
    qq qq号 手机 电脑 游戏 视频 图片 表情 链接 广告 消息 记录 问题 东西
    群友 表情包 录音 文件 转发 大风车 动画片 动画 首次 播放 最后一次
    jpg jpeg png gif webp mp3 mp4 source http https www com cn
    """.split()
)

_SPLIT_PATTERN = re.compile(
    r"[\s,，。.!！?？~～;；:：'\"“”‘’()\[\]【】{}<>《》、|/\\@#%^&*+=_…—-]+"
)
_QQ_PLACEHOLDER = re.compile(r"\[[^\[\]]{1,40}\]")
_PURE_DIGITS = re.compile(r"^\d+$")


def collect_group_chat_words(
    memory_path: Path | None = None,
    *,
    window_hours: float = _WINDOW_HOURS,
    now: float | None = None,
    limit: int = _MAX_WORDS,
) -> list[dict[str, object]]:
    """统计最近群聊高频词，返回 ``[{"word", "count"}]``（按频次降序）。"""

    path = memory_path or _DEFAULT_MEMORY_PATH
    moment = time.time() if now is None else now
    deadline = moment - max(0.5, float(window_hours)) * 3600.0
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        LOGGER.warning("群聊观察读取记忆文件失败：%s", exc)
        return []

    conversations = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(conversations, dict):
        return []

    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for conversation_id, item in conversations.items():
        # :user: 子会话是同一条群消息的按人视图，会与群会话重复计数。
        if not str(conversation_id).startswith("group:") or ":user:" in str(conversation_id):
            continue
        if not isinstance(item, dict):
            continue
        history = item.get("history")
        if not isinstance(history, list):
            continue
        for entry in history:
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            timestamp = entry.get("at") or entry.get("time") or 0
            try:
                if float(timestamp or 0) < deadline:
                    continue
            except (TypeError, ValueError):
                continue
            text = str(entry.get("text") or "")
            for token in _tokenise(text):
                counts[token] = counts.get(token, 0) + 1
                samples.setdefault(token, text[:60])

    known_terms = {
        _normalise_term(str(entry.get("term")))
        for entry in load_meme_store().get("entries", [])
        if isinstance(entry, dict)
    }
    ranked = [
        {"word": word, "count": count, "sample": samples.get(word, "")}
        for word, count in counts.items()
        if count >= _MIN_COUNT and _normalise_term(word) not in known_terms
    ]
    ranked.sort(key=lambda item: (-int(item["count"]), str(item["word"])))
    return ranked[: max(1, limit)]


def _tokenise(text: str) -> list[str]:
    cleaned = _QQ_PLACEHOLDER.sub(" ", text)
    tokens: list[str] = []
    for raw in _SPLIT_PATTERN.split(cleaned):
        token = raw.strip()
        if not (2 <= len(token) <= _MAX_TOKEN_LEN):
            continue
        if _PURE_DIGITS.match(token):
            continue
        if token.casefold() in _STOPWORDS:
            continue
        # 纯ASCII的常见单词也过滤，减少无意义候选进入精选。
        if token.isascii() and not token.isalpha():
            continue
        if token.isascii() and token.casefold() in {
            "the", "and", "you", "are", "yes", "ok", "okay", "no", "not",
            "this", "that", "what", "how", "why", "who", "when", "where",
        }:
            continue
        tokens.append(token)
    return tokens
