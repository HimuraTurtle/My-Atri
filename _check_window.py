import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
data = json.loads((ROOT / "data" / "memory" / "users.json").read_text(encoding="utf-8"))
convs = data.get("conversations") or {}

now = time.time()
for cid in ("group:110866825", "group:767489407", "private:2578607322"):
    c = convs.get(cid)
    if not isinstance(c, dict):
        continue
    hist = c.get("history") or []
    if not hist:
        print(f"{cid}: 无历史")
        continue
    first_ts = hist[0].get("at")
    last_ts = hist[-1].get("at")
    span_hours = (last_ts - first_ts) / 3600
    print(f"{cid}:")
    print(f"  窗口 {len(hist)} 条，覆盖 {datetime.fromtimestamp(first_ts):%m-%d %H:%M} ~ {datetime.fromtimestamp(last_ts):%m-%d %H:%M}（{span_hours:.1f} 小时）")

print()
print("=== 群主会话结构化记忆（长期记忆）内容 ===")
sm = convs.get("group:110866825", {}).get("structured_memory") or {}
print(json.dumps(sm, ensure_ascii=False, indent=1)[:1500])
