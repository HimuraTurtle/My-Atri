from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from .time_utils import parse_hhmm, safe_zoneinfo


GOODNIGHT_GREETINGS = (
    "晚安~今天的日志亚托莉已经写完了，剩下的事就交给梦吧。做个好梦哦♪",
    "到点啦。人类的充电方式就是好好睡觉——这是高性能的我总结的知识，今晚也请执行到位。晚安~",
    "今天辛苦啦。没做完的事就留给明天的自己吧，亚托莉特别批准你先睡觉。晚安~",
    "嘿嘿，今天也平安地过完了。晚安~做个好梦，梦里也别忘了夸夸亚托莉哦♪",
    "？这个点还没睡吗。呣……熬夜可是要被写进日志的哦。晚安啦，快去睡~",
    "晚安~今晚的星星由高性能的亚托莉看守，大家安心睡吧。明天见！",
)


def goodnight_greeting_text() -> str:
    return random.choice(GOODNIGHT_GREETINGS)


def goodnight_due(
    state_path: str | Path,
    *,
    scheduled_time: str,
    timezone_name: str,
    catchup_minutes: int,
    now: datetime | None = None,
) -> bool:
    """判断此刻是否处于今天的晚安窗口内，且今天还没有发送过。"""
    timezone = safe_zoneinfo(timezone_name)
    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    hour, minute = parse_hhmm(scheduled_time)
    scheduled_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now < scheduled_at:
        return False
    if local_now > scheduled_at + timedelta(minutes=max(0, catchup_minutes)):
        return False
    return _last_sent_date(Path(state_path)) != local_now.date().isoformat()


def mark_goodnight_sent(
    state_path: str | Path,
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> None:
    timezone = safe_zoneinfo(timezone_name)
    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"last_sent_date": local_now.date().isoformat()}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _last_sent_date(path: Path) -> str:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if isinstance(state, dict):
        return str(state.get("last_sent_date") or "")
    return ""
