from __future__ import annotations

import random
from typing import Any


def group_nudge_text(profile: dict[str, Any] | None = None) -> str:
    choices = (
        "？怎么忽然都安静啦。想聊什么随时叫亚托莉哦，我是高性能的嘛♪",
        "哼哼~♪……咳。没什么，只是群里安静得连我哼歌都有回音了。",
        "呣……大家都在认真生活吗？那亚托莉就不打扰了，等你们回来再聊。",
        "哼哒，明明准备了好多话题的。谁先冒泡，我就跟谁聊~",
        "群聊冷场可是要被记录进日志的哦……开玩笑的啦。累了就先歇着，想说话了随时叫我~",
    )
    return random.choice(choices)
