# 自动 gap 回填的离线验证：用生产 users.json 的副本 + 生产归档库（只读）
# 模拟"断线 2 小时后第一条群消息"，验证触发、写入、注入过滤、防重。
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from atri_qq_bot.memory import UserMemoryStore  # noqa: E402

GROUP = "110866825"

tmp_dir = Path(tempfile.mkdtemp(prefix="gap_test_"))
tmp_users = tmp_dir / "users.json"
shutil.copy(ROOT / "data" / "memory" / "users.json", tmp_users)

store = UserMemoryStore(tmp_users, config=None)
convs = store._data["conversations"]
item = convs[f"group:{GROUP}"]
last_at = max(e["at"] for e in item["history"] if isinstance(e, dict) and e.get("at"))
print(f"群 {GROUP} 副本最后活动时间戳: {last_at:.0f}")

before = len((item.get("structured_memory") or {}).get("l2") or [])
print(f"回填前 group 会话 L2 数量: {before}")

# 场景1: 消息只隔 60 秒 -> 不应触发
store.observe_group_message(GROUP, 999999, "刚回来的消息", now=last_at + 60)
after_short = len((store._data["conversations"][f"group:{GROUP}"].get("structured_memory") or {}).get("l2") or [])
assert after_short == before, "短间隔不应触发回填"
print("[PASS] 60秒间隔未触发回填")

# 场景2: 静默 2 小时 + 1 秒后的第一条消息 -> 应触发
trigger_at = last_at + 60 + 7201
store.observe_group_message(GROUP, 999999, "大家好我又回来了", now=trigger_at)

group_item = store._data["conversations"][f"group:{GROUP}"]
l2 = (group_item.get("structured_memory") or {}).get("l2") or []
gap_entries = [e for e in l2 if str(e.get("key", "")).startswith("gap_backfill:")]
assert gap_entries, "长时间静默应触发回填"
print(f"[PASS] 2小时静默触发回填，group 会话写入 {len(gap_entries)} 条")
for e in gap_entries:
    print(f"  value: {e['value'][:100]}")

# 场景3: 注入过滤验证（模拟 _structured_memory_profile 的 L2 过滤条件）
injectable = [
    e for e in l2
    if float(e.get("activity", 0)) > 0.3
    and e.get("state") != "sleeping"
    and e.get("category") in {"event", "schedule"}
]
assert any(str(e.get("key", "")).startswith("gap_backfill:") for e in injectable), "回填条目应通过注入过滤"
print(f"[PASS] 注入过滤通过（group 会话可注入 L2: {len(injectable)} 条）")

# 场景4: 成员子会话也应有回填条目
member_count = 0
for cid, conv in store._data["conversations"].items():
    if cid.startswith(f"group:{GROUP}:user:") and isinstance(conv, dict):
        for e in (conv.get("structured_memory") or {}).get("l2") or []:
            if str(e.get("key", "")).startswith("gap_backfill:"):
                member_count += 1
                if member_count <= 3:
                    print(f"  [{cid}] {str(e.get('value'))[:80]}")
                break
assert member_count > 0, "成员子会话应有回填条目"
print(f"[PASS] {member_count} 个成员子会话获得回填")

# 场景5: 同一缺口重复触发 -> 防重
l2_before = len(l2)
store._maybe_gap_backfill(GROUP, trigger_at + 5)
l2_after = len((store._data["conversations"][f"group:{GROUP}"].get("structured_memory") or {}).get("l2") or [])
assert l2_after == l2_before, "同一缺口不应重复写入"
print("[PASS] 同一缺口防重生效")

# 场景6: 已写盘验证（副本文件中确实存在）
saved = __import__("json").loads(tmp_users.read_text(encoding="utf-8"))
saved_gap = [
    e for e in (saved["conversations"][f"group:{GROUP}"].get("structured_memory") or {}).get("l2") or []
    if str(e.get("key", "")).startswith("gap_backfill:")
]
assert saved_gap, "副本文件中应有回填条目"
print(f"[PASS] 已持久化到磁盘（副本文件 group 会话 gap 条目: {len(saved_gap)}）")

shutil.rmtree(tmp_dir, ignore_errors=True)
print("\n全部通过")
