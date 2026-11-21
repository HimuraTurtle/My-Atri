"""对 replies.json 做公式化回答频率分析。

输出：
1. 整句重复（同一行文本出现 >=2 次）
2. 高频 n-gram（2~6 字，跨回复共享的措辞碎片）
3. 已知口癖/兜底文案命中表
每项标注嫌疑来源：代码兜底池（src）或提示词（docs/prompts）。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

REPO = Path(r"C:\BOT-ATRI\BOT-ATRI-main")
import sys as _sys
DATA = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(__file__).parent / "out" / "replies.json"

PUNCT = re.compile(r"[\s，。！？!?；;：:、“”‘’「」『』（）()【】\[\]—…·~,~.]+")
STOPGRAMS = {
    "哈哈", "哈哈哈哈", "的时", "时候", "什么", "怎么", "可以", "一个", "自己",
    "我们", "你们", "就是", "但是", "如果", "这个", "那个", "不是", "没有",
}


def norm(text: str) -> str:
    return PUNCT.sub("", str(text or ""))


def grep_count(phrase: str) -> tuple[int, int, int]:
    """返回 (src 命中文件数, docs/prompts 命中文件数, tests 命中文件数)。"""
    escaped = phrase.replace('"', '\\"')
    out = subprocess.run(
        ["grep", "-rl", escaped, "src", "docs/prompts"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.splitlines()
    src = sum(1 for p in out if p.startswith("src/"))
    prompts = sum(1 for p in out if p.startswith("docs/prompts/"))
    return src, prompts, len(out)


def main() -> None:
    entries = json.loads(DATA.read_text(encoding="utf-8"))
    lines: list[tuple[str, int, str]] = []  # (line, scenario#, reply_text)
    for index, entry in enumerate(entries):
        reply = str(entry.get("reply") or "")
        if reply.startswith("<EXC"):
            continue
        for line in reply.split("\n"):
            if line.strip():
                lines.append((line.strip(), index, reply))

    print(f"总轮次 {len(entries)}，消息行数 {len(lines)}\n")

    # --- 1. 整句重复 ---
    counter = Counter(norm(line) for line, _, _ in lines)
    dupes = [(text, count) for text, count in counter.most_common() if count >= 2 and len(text) >= 2]
    print("== 整句重复（跨场景出现 >=2 次）==")
    for text, count in dupes[:30]:
        src, prompts, total = grep_count(text[:12])
        tag = "CODE" if src else ("PROMPT" if prompts else "-")
        print(f"  x{count}  {text[:40]}  [{tag} src={src} prompt={prompts}]")

    # --- 2. 高频 n-gram ---
    gram_counter: Counter[str] = Counter()
    for text in counter:
        if not text:
            continue
        for size in (3, 4, 5):
            for i in range(len(text) - size + 1):
                gram = text[i : i + size]
                if gram in STOPGRAMS or PUNCT.fullmatch(gram):
                    continue
                gram_counter[gram] += 1
    print("\n== 高频措辞碎片（3~5 字，出现 >=4 次）==")
    seen: list[str] = []
    for gram, count in gram_counter.most_common(120):
        if count < 4:
            break
        if any(gram in prior or prior in gram for prior in seen):
            continue
        seen.append(gram)
        src, prompts, _ = grep_count(gram)
        tag = "CODE" if src else ("PROMPT" if prompts else "-")
        print(f"  x{count}  {gram}  [{tag} src={src} prompt={prompts}]")
        if len(seen) >= 35:
            break

    # --- 3. 已知口癖命中 ---
    suspects = [
        "哼哒", "高性能", "学习完毕", "嘿嘿", "哼哼", "呣", "啊呜", "快夸夸我",
        "派上用场", "是螃蟹", "螃蟹", "喝口水", "先别硬撑", "肩膀放下来",
        "陪你慢慢", "拆小", "我在听", "先缓一下", "好了好了我知道了",
        "机器人保护法", "反歧视法", "火箭拳", "花心大萝卜", "说别人废物",
        "偷看女生的日记", "跑得快算得准", "战斗机器人", "性能测试",
        "我在这边", "我永远陪你", "别想太多", "慢慢说", "洗把脸",
        "搞定", "交给我", "交给我吧", "包在我身上", "哼", "哼，",
        "我才不是", "真拿你没办法", "真是的", "笨蛋", "哦？", "哦？",
    ]
    hits = Counter()
    for line, _, _ in lines:
        for s in suspects:
            if s in line:
                hits[s] += 1
    print("\n== 已知口癖命中（按出现次数）==")
    for text, count in hits.most_common(30):
        print(f"  x{count}  {text}")

    # --- 4. 每轮平均条数 / 平均字数 ---
    turns_with_reply = [e for e in entries if e.get("reply") and not str(e["reply"]).startswith("<EXC")]
    line_counts = [len([l for l in str(e["reply"]).split("\n") if l.strip()]) for e in turns_with_reply]
    char_counts = [len(norm(e["reply"])) for e in turns_with_reply]
    print(f"\n== 形态 == 平均 {sum(line_counts)/max(1,len(line_counts)):.2f} 条/轮，"
          f"平均 {sum(char_counts)/max(1,len(char_counts)):.1f} 字/轮，"
          f"空回复 {len(entries) - len(turns_with_reply)} 轮")


if __name__ == "__main__":
    main()
