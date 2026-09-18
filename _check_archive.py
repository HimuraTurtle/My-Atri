import sqlite3
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
conn = sqlite3.connect(ROOT / "data" / "insights" / "insights.sqlite3")
cur = conn.cursor()

print("=== 昨晚本地 20:00~24:00 的群聊归档（UTC 12:00-16:00）===")
rows = cur.execute(
    "SELECT created_at, group_id, nickname, substr(content,1,60) FROM group_messages "
    "WHERE created_at >= '2026-09-02T12:00:00' AND created_at < '2026-09-02T16:00:00' "
    "AND (content LIKE '%目标%' OR content LIKE '%atri%' OR content LIKE '%ATRI%') "
    "ORDER BY created_at LIMIT 20"
).fetchall()
for ts, gid, nick, content in rows:
    print(f"  [UTC{ts[11:19]}=本地{ts[11:13].__class__ and ''}]{nick}: {content}")

total = cur.execute(
    "SELECT COUNT(*) FROM group_messages WHERE created_at >= '2026-09-02T12:00:00' "
    "AND created_at < '2026-09-02T16:00:00'"
).fetchone()[0]
print(f"\n昨晚本地 20:00-24:00 归档总消息数: {total}")

print("\n=== 归档每小时消息量（本地昨天 12:00 起）===")
rows2 = cur.execute(
    "SELECT substr(created_at,1,13) AS hour, COUNT(*) FROM group_messages "
    "GROUP BY hour ORDER BY hour"
).fetchall()
for hour, cnt in rows2:
    # UTC 小时 +8 = 本地
    local_h = (int(hour[11:13]) + 8) % 24
    print(f"  {hour[:10]} 本地{local_h:02d}:00 时段: {cnt} 条")

latest = cur.execute("SELECT MAX(created_at) FROM group_messages").fetchone()[0]
print(f"归档最新一条: {latest}")
