import sqlite3
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
conn = sqlite3.connect(ROOT / "data" / "insights" / "insights.sqlite3")
cur = conn.cursor()

print("=== 表结构 ===")
cols = cur.execute("PRAGMA table_info(group_messages)").fetchall()
for c in cols:
    print(f"  {c[1]} {c[2]}")

print()
print("=== 机器人自己发的消息是否归档（user_id=2440272213）===")
rows = cur.execute(
    "SELECT COUNT(*) FROM group_messages WHERE user_id = '2440272213'"
).fetchone()
print(f"  bot 消息数: {rows[0]}")

print()
print("=== 昨晚 18:00~今天04:00（UTC 10:00~20:00）消息样本 ===")
rows = cur.execute(
    "SELECT created_at, nickname, user_id, substr(content,1,50) FROM group_messages "
    "WHERE created_at >= '2026-09-02T10:00:00' AND created_at < '2026-09-02T20:30:00' "
    "AND group_id = '110866825' ORDER BY created_at LIMIT 8"
).fetchall()
for ts, nick, sid, content in rows:
    local_h = (int(ts[11:13]) + 8) % 24
    print(f"  本地{local_h:02d}:{ts[14:16]} [{nick}({sid})] {content}")

cnt = cur.execute(
    "SELECT COUNT(*) FROM group_messages WHERE created_at >= '2026-09-02T10:00:00' "
    "AND created_at < '2026-09-02T20:30:00' AND group_id = '110866825'"
).fetchone()[0]
print(f"\n该时段总消息数(群110866825): {cnt}")
