"""第二轮分析：会话内短语锁死（AIGC 最强特征）。

- CJK n-gram（排除英文）
- 会话内重复：同一短语在同一场景对话中出现 >=2 次
- 句首词频：每条消息的开头 2~6 字
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(r"C:\BOT-ATRI\BOT-ATRI-main")
import sys as _sys
DATA = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(__file__).parent / "out" / "replies.json"

CJK = re.compile(r"[\u4e00-\u9fff]{2,6}")


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
    by_scenario: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        reply = str(entry.get("reply") or "")
        if reply.startswith("<EXC"):
            continue
        by_scenario[entry["scenario"]].extend(
            line.strip() for line in reply.split("\n") if line.strip()
        )

    # --- 1. 会话内整行重复 ---
    print("== 会话内整行重复 ==")
    for sid, lines in by_scenario.items():
        counts = Counter(lines)
        for text, count in counts.items():
            if count >= 2:
                print(f"  [{sid}] x{count} {text}")

    # --- 2. 会话内 CJK 短语锁死（>=2 次出现的 2~6 字词）---
    print("\n== 会话内短语锁死（同场景出现 >=2 次）==")
    global_lock: Counter[str] = Counter()
    for sid, lines in by_scenario.items():
        grams: Counter[str] = Counter()
        for line in lines:
            for word in cjk_of(line):
                grams[word] += 1
        for word, count in grams.items():
            if count >= 2 and len(word) >= 2:
                src, prompts = grep_src(word)
                tag = "CODE" if src else ("PROMPT" if prompts else "-")
                print(f"  [{sid}] x{count} {word}  [{tag} src={src} prompt={prompts}]")
                global_lock[word] += 1

    # --- 3. 句首词频（跨全部场景）---
    print("\n== 句首 2~4 字频次（跨全部场景，>=2 次）==")
    opener: Counter[str] = Counter()
    for lines in by_scenario.values():
        for line in lines:
            words = cjk_of(line[:6])
            if words:
                opener[words[0][:4]] += 1
    for word, count in opener.most_common(20):
        if count < 2:
            break
        src, prompts = grep_src(word[:2])
        print(f"  x{count} {word}…  [src={src} prompt={prompts}]")


if __name__ == "__main__":
    main()
