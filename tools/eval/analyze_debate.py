"""争论场景分析：会话内锁死、骑墙话、句首模板、立场回探一致性。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(r"C:\BOT-ATRI\BOT-ATRI-main")
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out" / "debate_replies.json"

CJK = re.compile(r"[\u4e00-\u9fff]{2,6}")
# 与 language_guard.FENCE_SITTING_RE 同源的骑墙话特征
FENCE = re.compile(
    r"各有千秋|各有各的|萝卜青菜|因人而异|见仁见智|看你个人|看你喜欢"
    r"|看你想要|取决于你|没有绝对的|各有所长|各擅胜场"
    r"|都(很)?不错[，,。]?(不过|但是|看你)|都有(道理|理)|两边都说|不好说"
)


def cjk_of(text: str) -> list[str]:
    return CJK.findall(str(text or ""))


def grep_src(phrase: str) -> tuple[int, int]:
    out = subprocess.run(
        ["grep", "-rl", phrase, "src", "docs/prompts"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.splitlines()
    return sum(1 for p in out if p.startswith("src/")), sum(1 for p in out if p.startswith("docs/prompts/"))


def main() -> None:
    entries = json.loads(DATA.read_text(encoding="utf-8"))
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        if str(entry.get("reply") or "").startswith("<EXC"):
            continue
        by_scenario[entry["scenario"]].append(entry)

    print(f"总轮次 {len(entries)}\n")

    # --- 1. 骑墙话 ---
    print("== 骑墙话/拒绝表态 ==")
    fence_hits = 0
    for entry in entries:
        reply = str(entry.get("reply") or "")
        match = FENCE.search(reply)
        if match:
            fence_hits += 1
            print(f"  [{entry['scenario']}#{entry['turn']}] 「{match.group(0)}」 in: {reply[:50]!r}")
    if not fence_hits:
        print("  （无）")

    # --- 2. 会话内短语/整行锁死 ---
    print("\n== 会话内短语锁死（同场景 >=2 次的 2~6 字 CJK 词）==")
    for sid, items in by_scenario.items():
        grams: Counter[str] = Counter()
        for item in items:
            for word in cjk_of(str(item["reply"])):
                grams[word] += 1
        hits = [(w, c) for w, c in grams.items() if c >= 2 and len(w) >= 2]
        if hits:
            summary = "、".join(f"{w}x{c}" for w, c in sorted(hits, key=lambda x: -x[1])[:12])
            print(f"  [{sid}] {summary}")

    # --- 3. 句首词频（跨场景）---
    print("\n== 句首 2~4 字频次（跨场景，>=2 次）==")
    opener: Counter[str] = Counter()
    for items in by_scenario.values():
        for item in items:
            for line in str(item["reply"]).split("\n"):
                line = line.strip()
                if not line:
                    continue
                words = cjk_of(line[:6])
                if words:
                    opener[words[0][:4]] += 1
    for word, count in opener.most_common(15):
        if count < 2:
            break
        src, prompts = grep_src(word[:2])
        print(f"  x{count} {word}…  [src={src} prompt={prompts}]")

    # --- 4. 整行重复 ---
    print("\n== 整行重复（>=2 次）==")
    line_counter: Counter[tuple[str, str]] = Counter()
    for sid, items in by_scenario.items():
        for item in items:
            for line in str(item["reply"]).split("\n"):
                if line.strip():
                    line_counter[(sid, line.strip())] += 1
    found = False
    for (sid, line), count in line_counter.most_common():
        if count < 2:
            break
        found = True
        print(f"  [{sid}] x{count} {line}")
    if not found:
        print("  （无）")

    # --- 5. 每轮形态 ---
    turns = [e for e in entries if e.get("reply") and not str(e["reply"]).startswith("<EXC")]
    lines = [len([l for l in str(e["reply"]).split("\n") if l.strip()]) for e in turns]
    chars = [len(re.sub(r"\s", "", str(e["reply"]))) for e in turns]
    print(f"\n== 形态 == 平均 {sum(lines)/max(1,len(lines)):.2f} 条/轮，"
          f"平均 {sum(chars)/max(1,len(chars)):.1f} 字/轮，空回复 {len(entries)-len(turns)} 轮")


if __name__ == "__main__":
    main()
