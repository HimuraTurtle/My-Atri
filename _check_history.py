import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
data = json.loads((ROOT / "data" / "memory" / "users.json").read_text(encoding="utf-8"))
convs = data.get("conversations") or {}
now = time.time()
print(f"当前时间: {datetime.now():%H:%M:%S} (epoch={now:.0f})")
print()
for cid, c in convs.items():
    if not isinstance(c, dict):
        continue
    # 找出实际的对话历史字段
    hist_key = None
    for key in ("history", "messages", "turns", "chat"):
        if isinstance(c.get(key), list):
            hist_key = key
            break
    if not hist_key:
        # 直接找 list 类型的字段
        for key, value in c.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                hist_key = key
                break
    if not hist_key:
        continue
    hist = c[hist_key]
    if not hist:
        print(f"{cid}: 空")
        continue
    last = hist[-1]
    ts = last.get("at") or last.get("ts") or last.get("time")
    if ts:
        age_min = (now - float(ts)) / 60
        last_role = last.get("role")
        last_text = str(last.get("text") or "")[:40].replace("\n", " ")
        flag = " <-- 旧(断线前)" if age_min > 300 else ""
        print(f"{cid}: {len(hist)}条 最后消息={datetime.fromtimestamp(ts):%m-%d %H:%M} ({age_min:.0f}分钟前) [{last_role}] {last_text}{flag}")
