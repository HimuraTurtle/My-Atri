from __future__ import annotations

import random
from datetime import datetime


MORNING_GREETINGS = (
    "早上好。高性能亚托莉早就醒啦——今天也从一件小事开始吧，做成了记得来找我领夸奖♪",
    "早安，先喝水，再伸个懒腰——亚托莉查过资料，这是给一天存元气的起床步骤哦~",
    "早上好。呣……其实我也有点低电压，但叫你起床这件事是认真执行的。今天也请多关照。",
    "早安，今天也要好好吃早饭哦。有什么想做的事，先告诉亚托莉一个，我帮你记着。",
    "早上好。今天想以什么心情开场？元气也好，慢慢来也好，亚托莉都陪你。",
    "早安，新的一天开始啦。难题尽管丢过来——高性能的我最擅长解决了。出发！",
)


def morning_greeting_text(now: datetime | None = None) -> str:
    text = random.choice(MORNING_GREETINGS)
    if now and now.weekday() == 0:
        return f"早安。{text.removeprefix('早上好。').removeprefix('早安，')}"
    return text
