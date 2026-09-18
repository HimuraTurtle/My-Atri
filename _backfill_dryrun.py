import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
CN = timezone(timedelta(hours=8))

conn = sqlite3.connect(ROOT / "data" / "insights" / "insights.sqlite3")
cur = conn.cursor()

# 回填范围：昨晚 20:00 ~ 今晨 04:11（当前窗口起点）
START_UTC = "2026-09-02T12:00:00"
END_UTC = "2026-09-02T20:11:00"

rows = cur.execute(
    "SELECT created_at, group_id, user_id, nickname, content, message_type FROM group_messages "
    "WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
    (START_UTC, END_UTC),
).fetchall()

noise = re.compile(r"^\s*(\[表情包/图片:[^\]]*\]|\[动画表情\]|\[图片\])?\s*$")
stats = {}
clean = []
for ts, gid, uid, nick, content, mtype in rows:
    stats[gid] = stats.get(gid, 0) + 1
    text = str(content or "").strip()
    if not text or noise.match(text):
        continue
    # 表情占位为主的混合消息也过滤
    stripped = re.sub(r"\[表情包/图片:[^\]]*\]", "", text).strip()
    if not stripped:
        continue
    clean.append((ts, gid, uid, nick, stripped))

print(f"归档消息总数: {len(rows)}，过滤表情后: {len(clean)}")
for gid, cnt in stats.items():
    print(f"  群 {gid}: {cnt}")

# 机器人回复（reply-events）同时段
events = []
for ln in (ROOT / "logs" / "reply-events.jsonl").read_text(encoding="utf-8").strip().splitlines():
    try:
        e = json.loads(ln)
    except Exception:
        continue
    t = str(e.get("time") or "")
    if "2026-09-02 20:00:00" <= t <= "2026-09-03 04:11:00":
        cid = str(e.get("conversation_id") or "")
        if ":user:" not in cid:
            events.append(e)
print(f"机器人回复事件(群): {len(events)}")

# 合并后总数 vs 现有窗口
data = json.loads((ROOT / "data" / "memory" / "users.json").read_text(encoding="utf-8"))
for cid in ("group:110866825", "group:767489407"):
    cur_n = len(data["conversations"].get(cid, {}).get("history") or [])
    backfill_n = len([c for c in clean if f"group:{c[1]}" == cid])
    reply_n = len([e for e in events if str(e.get("conversation_id") or "") == cid])
    print(f"{cid}: 现有 {cur_n} + 回填群友 {backfill_n} + 回填ATRI {reply_n} = {cur_n + backfill_n + reply_n}")
