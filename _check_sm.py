import json
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
data = json.loads((ROOT / "data" / "memory" / "users.json").read_text(encoding="utf-8"))

for cid in ("group:110866825", "private:2578607322"):
    sm = data["conversations"].get(cid, {}).get("structured_memory") or {}
    print(f"=== {cid} structured_memory 层级: {list(sm.keys())}")
    for layer, items in sm.items():
        if isinstance(items, list):
            print(f"  {layer}: {len(items)} 条")
            for it in items[:2]:
                if isinstance(it, dict):
                    print(f"    keys: {list(it.keys())[:10]}")
                    print(f"    样例: {json.dumps({k: it.get(k) for k in ('key','value','category','importance','created_at','last_mentioned_at') if k in it}, ensure_ascii=False)[:200]}")
    print()
