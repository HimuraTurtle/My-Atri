import json
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")

lines = (ROOT / "logs" / "reply-events.jsonl").read_text(encoding="utf-8").strip().splitlines()
events = [json.loads(ln) for ln in lines if ln.strip()]

print("=== 昨晚含'目标'的问答对 ===")
for e in events:
    t = str(e.get("time") or "")
    if not t.startswith("2026-09-02"):
        continue
    ut = str(e.get("user_text") or "")
    mr = str(e.get("model_reply") or "")
    if "目标" in ut or "目标" in mr:
        print(f"[{t}] 用户: {ut[:60]}")
        print(f"    ATRI: {mr[:80]}")

print()
print("=== 昨晚 22:00-24:00 机器人的群聊回复（上下文定位）===")
for e in events:
    t = str(e.get("time") or "")
    if "2026-09-02 22:" <= t <= "2026-09-02 23:59":
        ut = str(e.get("user_text") or "")[:40].replace("\n", " ")
        mr = str(e.get("model_reply") or "")[:40].replace("\n", " ")
        cid = str(e.get("conversation_id") or e.get("cid") or "")[:30]
        print(f"[{t}] {cid} 用户:{ut}")
        print(f"          ATRI:{mr}")
