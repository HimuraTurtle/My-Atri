# 断线回填：把归档库里断线前的群聊话题写成 L2 结构化记忆，
# 让机器人恢复后仍能接上断线前的讨论。
# 用法：
#   python _backfill_offline_topics.py           # dry-run，只打印将要写入的内容
#   python _backfill_offline_topics.py --apply    # 实际写入 users.json
import json
import re
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main")
USERS_JSON = ROOT / "data" / "memory" / "users.json"
DB = ROOT / "data" / "insights" / "insights.sqlite3"

# 断线窗口：本地 2026-09-02 12:00 -> 2026-09-03 05:48（KickedOffLine 之前）
# 归档 created_at 是 UTC，本地-8h
WINDOW_START_UTC = "2026-09-02T04:00:00"
WINDOW_END_UTC = "2026-09-02T21:48:00"
BACKFILL_TAG = "offline_backfill:20260903"

CN = timezone(timedelta(hours=8))

POLLUTION_RE = re.compile(
    r"^(\[表情包/图片[^\]]*\]|\[图片\]|\[表情\]|\[动画表情\]|\[QQ表情\]|\[语音\]|\[视频\]|\[文件\]|\s*)$"
)


BOT_OUTPUT_HINTS = ("ref_id:", "tip:", "登录成功", "使用 /", "更新时间:", "数据更新")


def clean_text(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    # 去掉 CQ 码、表情/图片/分享/戳一戳等占位
    text = re.sub(r"\[CQ:[^\]]*\]", "", text)
    text = re.sub(r"\[表情包/图片[^\]]*\]", "", text)
    text = re.sub(r"\[QQ表情[^\]]*\]", "", text)
    text = re.sub(r"\[(分享|用户|flash|戳一戳)[^\]]*\]", "", text)
    # 去掉行首 at 前缀（@昵称 后跟内容）和 URL
    text = re.sub(r"^\s*@\S+\s*", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_useful(text: str) -> bool:
    if len(text) < 6 or len(text) > 120:
        return False
    if POLLUTION_RE.match(text):
        return False
    if any(hint in text for hint in BOT_OUTPUT_HINTS):
        return False
    # 纯符号/数字/字母（无中文实质）不选
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cn_chars < 3:
        return False
    if re.fullmatch(r"(.)\1+", text):
        return False
    # 命令不选
    if text.startswith("/") or text.startswith("#"):
        return False
    return True


def pick_representative(msgs: list[str], count: int = 3) -> list[str]:
    """从用户消息里挑信息量较大的代表句。"""
    scored = []
    for m in msgs:
        # 长度适中、含中文的句子信息量高
        cn_chars = len(re.findall(r"[\u4e00-\u9fff]", m))
        score = cn_chars + (3 if 10 <= len(m) <= 60 else 0)
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    for _, m in scored:
        if m not in out:
            out.append(m)
        if len(out) >= count:
            break
    return out


def fmt_local(ts_utc: str) -> str:
    dt = datetime.fromisoformat(ts_utc.replace("+00:00", "")).replace(tzinfo=timezone.utc)
    return dt.astimezone(CN).strftime("%m月%d日%H点")


def main() -> None:
    apply = "--apply" in sys.argv

    cur = sqlite3.connect(DB).cursor()
    rows = cur.execute(
        "SELECT group_id, user_id, nickname, content, created_at FROM group_messages "
        "WHERE created_at >= ? AND created_at <= ? AND message_type = 'text' "
        "ORDER BY created_at",
        (WINDOW_START_UTC, WINDOW_END_UTC),
    ).fetchall()
    print(f"归档窗口内文本消息: {len(rows)} 条")

    # group_id -> user_id -> [(nickname, [msgs])]
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: {"nick": "", "msgs": []}))
    for gid, uid, nick, content, ts in rows:
        text = clean_text(content)
        if not is_useful(text):
            continue
        entry = groups[str(gid)][str(uid)]
        entry["msgs"].append(text)
        if nick and not entry["nick"]:
            entry["nick"] = str(nick).strip()

    now = time.time()
    plan: list[tuple[str, dict]] = []  # (conversation_id, l2_entry)

    for gid, users in groups.items():
        group_summary_parts: list[str] = []
        # 用户子会话：只回填 users.json 里已存在的（bot 跟踪过的）
        data = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        convs = data["conversations"]
        for uid, entry in users.items():
            sub_cid = f"group:{gid}:user:{uid}"
            reps = pick_representative(entry["msgs"], 2)
            if not reps:
                continue
            nick = entry["nick"] or f"群友{uid[-4:]}"
            value = f"{nick}在9月2日晚到3日凌晨聊过：「{reps[0]}」"
            if len(reps) > 1:
                value += f"，还有「{reps[1]}」"
            value = value[:120]
            if sub_cid in convs:
                plan.append((sub_cid, make_entry(value, now)))
                group_summary_parts.append(f"{nick}：「{reps[0]}」")
        if not group_summary_parts:
            continue
        value = "9月2日晚到3日凌晨（断线前）群里在聊：" + "；".join(group_summary_parts[:5])
        plan.append((f"group:{gid}", make_entry(value[:200], now)))

    print(f"\n将写入 {len(plan)} 条 L2 记忆：")
    for cid, entry in plan:
        print(f"  [{cid}] {entry['value']}")

    if not apply:
        print("\n(dry-run 未写盘，加 --apply 执行)")
        return

    # 安全写盘：重新读最新文件状态，插入后快速替换
    for attempt in range(3):
        data = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        convs = data["conversations"]
        written = 0
        for cid, entry in plan:
            item = convs.get(cid)
            if item is None:
                continue
            memory = item.setdefault("structured_memory", {})
            l2 = memory.setdefault("l2", [])
            if any(str(e.get("key", "")).startswith(BACKFILL_TAG) for e in l2):
                continue  # 已回填过，防重复
            l2.append(entry)
            written += 1
        if written == 0:
            print("没有需要写入的条目（可能已回填过）")
            return
        tmp = USERS_JSON.with_name(f".{USERS_JSON.name}.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(USERS_JSON)
        # 写回后重读验证
        verify = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        ok = all(
            any(
                str(e.get("key", "")).startswith(BACKFILL_TAG)
                for e in (verify["conversations"].get(cid) or {}).get("structured_memory", {}).get("l2", [])
            )
            for cid, _ in plan
        )
        print(f"\n已写入 {written} 条（attempt {attempt + 1}），验证: {'通过' if ok else '失败'}")
        if ok:
            print("bot 进程会在下次访问 users.json 时自动重载（_reload_if_changed）")
            return
        time.sleep(0.5)
    print("多次尝试后仍未验证通过，请手动检查")


def make_entry(value: str, now: float) -> dict:
    return {
        "layer": "L2",
        "category": "event",
        "key": BACKFILL_TAG,
        "value": value,
        "activity": 1.0,
        "confidence": 0.7,
        "source": "user",
        "created_at": now,
        "updated_at": now,
        "last_mentioned_at": now,
        "state": "active",
        "associations": [],
    }


if __name__ == "__main__":
    main()
