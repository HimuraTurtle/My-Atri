"""「是啊，吃什么」群聊接梗（移植自 astrbot_plugin_what_to_eat）。

被动触发：群消息出现关键词（默认「吃什么/吃啥」）时，按概率二选一
- 推荐一个食物（默认 50%，先冒螃蟹兴奋再转实际推荐）
- 复读「是啊，吃什么」（默认 50%）

多 Bot 防循环与原插件一致：时间窗口内响应超过上限后强制推荐（推荐
文案不含关键词，接龙自然断开）；复读后的冷却期内同样强制推荐。
回复模板按亚托莉声线改写：复读保留「是啊，吃什么」原句作为可辨识
的梗核心；推荐话术围绕原作螃蟹兴奋（「是螃蟹哦！」——人设规定该
兴奋可短暂压过话题、但要很快回来），分四种节奏轮换避免复读感：
螃蟹开场被拉回正题、先给推荐再补螃蟹执念、馋到走神被问话打断、
偶尔固执地只推荐螃蟹。按会话避开最近用过的模板与食物，连续两轮
不会复读同一句。原插件的食物图片功能未移植（插件不自带图片目录）。
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# 复读模板：保留梗原句「是啊，吃什么」，追加的语气词属于亚托莉口癖。
ECHO_TEMPLATES = (
    "是啊，吃什么",
    "是啊，吃什么……",
    "是啊，吃什么……呣",
    "是啊——吃什么",
    "是啊，吃什么……（高性能也解不出这道题）",
)

# 推荐模板：台词（是螃蟹哦！/大丰收哦！/好吃就是高兴！/哒啊呜/啊呜啊呜）
# 均出自原作螃蟹片段；四种节奏分组排列，组内句式互不雷同。文案不得
# 包含触发关键词，否则会接龙自己；最后一条为无 {food} 的纯螃蟹执念。
RECOMMEND_TEMPLATES = (
    # 螃蟹开场，被拉回正题
    "是螃蟹哦！是螃蟹哦螃蟹！……呣，冷静完毕。今天要不吃{food}？",
    "快看快看，是螃蟹哦！里面有好多呢！……咳咳，总之，{food}吧。",
    "螃蟹！大丰收哦！……晚上吃{food}怎么样？",
    "螃蟹……螃蟹好好吃……呣？你在问今天？那吃{food}吧！",
    # 先给推荐，再露出螃蟹执念
    "今天吃{food}吧！……啊，对了，螃蟹很好吃的。好吃就是高兴！",
    "要不吃{food}？呣……不过高性能的首选，永远都是螃蟹哦♪",
    "{food}怎么样？……总有一天，要吃螃蟹吃到饱哒啊呜～",
    # 馋到走神，被问话打断才想起来回答
    "呣……想吃螃蟹了。啊呜啊呜……嗯？你说什么？那，{food}也可以哦！",
    # 偶尔的固执：今天就吃螃蟹
    "唔……认真讨论的结论是：螃蟹。今天就要吃螃蟹！",
)

FALLBACK_RESPONSE = "呣……高性能系统暂时想不出推荐，这一题就交给你自己啦，加油哦♪"


@dataclass(frozen=True)
class WhatToEatDecision:
    text: str
    kind: str  # "echo" / "recommend" / "fallback"


class _RateLimiter:
    """按会话记录响应次数：超限后强制推荐，复读后冷却期内强制推荐。"""

    def __init__(self, max_responses: int, window_seconds: int, echo_cooldown_seconds: int) -> None:
        self._max_responses = max(1, max_responses)
        self._window_seconds = window_seconds
        self._echo_cooldown_seconds = max(0, echo_cooldown_seconds)
        self._response_history: dict[str, list[float]] = {}
        self._echo_at: dict[str, float] = {}

    def check_and_record(self, key: str) -> bool:
        """记录本次响应；返回 True 表示窗口内已超限，需强制推荐。"""
        if not key:
            return False
        now = time.time()
        history = [
            ts for ts in self._response_history.get(key, ()) if ts > now - self._window_seconds
        ]
        force_recommend = len(history) >= self._max_responses
        history.append(now)
        self._response_history[key] = history
        return force_recommend

    def is_in_echo_cooldown(self, key: str) -> bool:
        if not key or self._echo_cooldown_seconds <= 0:
            return False
        last = self._echo_at.get(key)
        if last is None:
            return False
        if time.time() - last >= self._echo_cooldown_seconds:
            del self._echo_at[key]
            return False
        return True

    def record_echo(self, key: str) -> None:
        if key and self._echo_cooldown_seconds > 0:
            self._echo_at[key] = time.time()


class WhatToEatService:
    def __init__(self, config: Any) -> None:
        keywords = tuple(getattr(config, "what_to_eat_trigger_keywords", ()) or ())
        self._keyword_pattern = (
            re.compile("|".join(re.escape(k) for k in keywords)) if keywords else None
        )
        probability = float(getattr(config, "what_to_eat_recommend_probability", 0.3))
        self._probability = max(0.0, min(1.0, probability))
        builtin = tuple(getattr(config, "what_to_eat_builtin_foods", ()) or ())
        custom = tuple(getattr(config, "what_to_eat_custom_foods", ()) or ())
        self.foods: tuple[str, ...] = tuple(dict.fromkeys(builtin + custom))
        self._rate = _RateLimiter(
            int(getattr(config, "what_to_eat_rate_limit_max", 3)),
            int(getattr(config, "what_to_eat_rate_limit_window_seconds", 60)),
            int(getattr(config, "what_to_eat_echo_cooldown_seconds", 15)),
        )
        self._recent_templates: dict[str, deque[int]] = {}
        self._recent_foods: dict[str, str] = {}

    def matches(self, plain_text: str) -> bool:
        if self._keyword_pattern is None:
            return False
        return bool(self._keyword_pattern.search(str(plain_text or "")))

    def decide(self, conversation_id: str) -> WhatToEatDecision:
        """决定本轮回复并更新频率限制状态，调用方按会话串行调用。"""
        force_recommend = self._rate.check_and_record(conversation_id)
        echo_cooldown = self._rate.is_in_echo_cooldown(conversation_id)
        if not (force_recommend or echo_cooldown) and random.random() >= self._probability:
            self._rate.record_echo(conversation_id)
            return WhatToEatDecision(random.choice(ECHO_TEMPLATES), "echo")
        if not self.foods:
            return WhatToEatDecision(FALLBACK_RESPONSE, "fallback")
        food = self._pick_food(conversation_id)
        template = RECOMMEND_TEMPLATES[self._pick_template_index(conversation_id)]
        return WhatToEatDecision(template.format(food=food), "recommend")

    def _pick_food(self, conversation_id: str) -> str:
        """随机选食物但避开本会话上一轮的选择，防止连发同一个推荐。"""
        last = self._recent_foods.get(conversation_id)
        candidates = [f for f in self.foods if f != last] or list(self.foods)
        food = random.choice(candidates)
        self._recent_foods[conversation_id] = food
        return food

    def _pick_template_index(self, conversation_id: str) -> int:
        """随机选模板但避开本会话最近两次用过的，消除连续复读感。"""
        recent = self._recent_templates.setdefault(
            conversation_id, deque(maxlen=2)
        )
        candidates = [
            i for i in range(len(RECOMMEND_TEMPLATES)) if i not in recent
        ] or list(range(len(RECOMMEND_TEMPLATES)))
        index = random.choice(candidates)
        recent.append(index)
        return index


_SERVICE: WhatToEatService | None = None


def get_what_to_eat_service(config: Any) -> WhatToEatService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = WhatToEatService(config)
    return _SERVICE
