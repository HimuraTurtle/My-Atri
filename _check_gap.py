import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")

print("=== 断线空档验证：group_messages 时间线 ===")
conn = sqlite3.connect(ROOT / "data" / "insights" / "insights.sqlite3")
cur = conn.cursor()
rows = cur.execute(
    "SELECT created_at, group_id, substr(content,1,30) FROM group_messages "
    "WHERE created_at >= '2026-09-03T02:00:00' ORDER BY created_at"
).fetchall()
if rows:
    prev = None
    for ts, gid, content in rows:
        if prev:
            from datetime import datetime as dt
            try:
                t1 = dt.fromisoformat(prev)
                t2 = dt.fromisoformat(ts)
                gap = (t2 - t1).total_seconds() / 60
                if gap > 30:
                    print(f"  *** 空档 {gap:.0f} 分钟: {prev[11:19]} ~ {ts[11:19]} (群{gid}) ***")
            except Exception:
                pass
        prev = ts
    print(f"  归档范围: {rows[0][0][:19]} ~ {rows[-1][0][:19]}，共 {len(rows)} 条")

print()
print("=== 断线前后上下文连续性（11:05 后机器人被@的对话样本）===")
# 从 reply-events.jsonl 找恢复后的回复，看 user_text 是否引用了断线期间的话题
import json

lines = (ROOT / "logs" / "reply-events.jsonl").read_text(encoding="utf-8").strip().splitlines()
recent = [json.loads(ln) for ln in lines if ln.strip()]
recent = [r for r in recent if str(r.get("time") or "") >= "2026-09-03 10:52"]
print(f"恢复后问答对: {len(recent)} 条")
for r in recent[:6]:
    ut = str(r.get("user_text") or "")[:50].replace("\n", " ")
    mr = str(r.get("model_reply") or "")[:50].replace("\n", " ")
    print(f"  [{str(r.get('time'))[:19]}] 用户: {ut}")
    print(f"      ATRI: {mr}")
