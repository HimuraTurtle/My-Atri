"""分析 onebot-events.log 里表情包相关 segment 的真实结构，验证偷取触发覆盖面。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

path = Path(r"C:\BOT-ATRI\BOT-ATRI-main\logs\onebot-events.log")
img_subtypes: Counter = Counter()
image_keys: Counter = Counter()
mface_url: Counter = Counter()
sample_no_url: list[str] = []
total_events = 0

with open(path, encoding="utf-8", errors="replace") as f:
    for line in f:
        if '"message"' not in line:
            continue
        total_events += 1
        i = line.find("{")
        if i < 0:
            continue
        try:
            ev = json.loads(line[i:])
        except Exception:
            continue
        msg = ev.get("message") or []
        if not isinstance(msg, list):
            continue
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "image":
                data = seg.get("data") or {}
                img_subtypes[repr(data.get("sub_type"))] += 1
                for key in ("url", "emoji_id", "emoji_package_id"):
                    if data.get(key):
                        image_keys[key] += 1
            elif seg.get("type") in ("mface", "marketface"):
                data = seg.get("data") or {}
                mface_url["url" if data.get("url") else "no_url"] += 1
                if not data.get("url") and len(sample_no_url) < 3:
                    sample_no_url.append(json.dumps(data, ensure_ascii=False)[:300])

print("message lines:", total_events)
print("image sub_type 分布:", dict(img_subtypes))
print("image 字段命中:", dict(image_keys))
print("mface url:", dict(mface_url))
for sample in sample_no_url:
    print("无url mface 样本:", sample)
