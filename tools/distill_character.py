# -*- coding: utf-8 -*-
"""角色文本蒸馏工具（离线，map-reduce + 分段 reduce）。

方法论移植自 astrbot_plugin_character_distiller：LLM 按结构化框架从源文本
提取角色人设。原插件把源文本截断在 8K~50K 字符内单次蒸馏；本工具面向
完整游戏剧本（数百 K 字符）：
  map    ：分块提取目标角色的可观察证据，结果缓存于 data/runtime/distill-work/
           chunk_XX.md，重跑自动复用
  reduce ：把全部证据按 11 节框架汇总。单次输出装不下全部 11 节（10K token
           触顶；早期版本还在第 9 节出现机械重复退化循环），故按节拆成
           聚焦小阶段，每阶段带硬性行数上限与防重复约束：
           reduce2 → 第 5 节其余角色小节（已完成）
           reduce3 → 第 8 节场景化行为（已完成）
           reduce4 → 第 10~11 节世界观与金句（已完成）
           reduce6/7/9 → 第 6 节成长弧线、第 7 节雷区边界、第 9 节时间作息
           （reduce1 即第 1~4 节 + 第 5 节夏生小节，存于 data/runtime/distill_ATRI.md）
产物只作为 docs/prompts 人设的素材库；融合需人工筛除与 QQ 现实冲突的
条目（工具可用性、用户关系解耦、运行时好感度引擎等），不能直接替换。

用法：
  .venv\\Scripts\\python.exe tools\\distill_character.py --character ATRI
  可选：--source data/runtime/distill-source.md --chunk-chars 18000
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx


def load_env_value(key: str) -> str:
    text = (ROOT / ".env").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


MAP_PROMPT = """你是角色证据提取器。以下是视觉小说《ATRI -My Dear Moments-》中文完整剧本的一个片段。

目标角色：{character}（亚托莉）。

请从这个片段中提取所有关于目标角色的可观察证据，供后续构建角色扮演人设使用。逐类整理：

1. 台词风格与原句摘录：她实际说出的代表性原句（保留原文，标注说话场景）
2. 口头禅与语气词：出现过的口癖、变体、大致频次
3. 情绪表达：开心/得意/生气/难过/害羞/认真/空洞时分别说了什么（原话）
4. 行为模式：行动方式、习惯、失误、被拆穿后的反应
5. 关系互动：与夏生及其他角色的互动方式、代表性对话
6. 成长与变化：认知或情感变化的节点
7. 雷区与防御反应：被冒犯/被叫废物/被质疑没有心时的反应（原话）
8. 场景化行为：料理/学习/早晨/潜水/战斗/日志等场景中的表现
9. 世界观细节：她生活的环境、时代背景、身体设定

要求：
- 每个条目尽量附带原文引句作证据
- 只报告片段中真实出现的内容，禁止编造或从外部知识补充
- 该片段中不存在的类别写"（本片段无）"
- 直接输出整理结果，不要额外说明
"""


DISCIPLINE = """硬性要求：
- 严格遵守上面的行数上限；优先归纳模式与规律，禁止罗列全部台词
- 每条要点内容独立，禁止机械重复相同或近似条目
- 一切以证据提取结果中的原文引句为准，证据没有的不写
- 输出用中文 Markdown，不要输出任何额外说明

===== 证据提取结果 =====

{map_outputs}
"""


REDUCE2_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~4 节，以及第 5 节中「与夏生」的互动小节。现在只需产出第 5 节的其余部分：夏生以外角色的互动小节。每个角色一个加粗小节标题（如「**与水菜萌：**」），涵盖水菜萌/龙司/凛凛花/凯瑟琳/安田/诗菜/乃音子/其他角色，各附代表性互动原句。不要输出「## 5」这个节标题，直接以加粗小节开头。主要角色（水菜萌/龙司/凯瑟琳/安田/诗菜）每小节不超过 25 行，次要角色每小节不超过 8 行。

""" + DISCIPLINE


REDUCE3_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~7 节。现在只需产出以下一节，不要重复已完成的部分：

## 8. 场景化行为（料理/学习与考试/早晨与刷牙/潜水与游泳/战斗/日志/吃与味觉/告别等场景，每个场景一个小节，概括她的典型表现与失败模式，附最具代表性的原句）

每个场景小节不超过 15 行。

""" + DISCIPLINE


REDUCE6_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~5 节。现在只需产出以下一节，不要重复已完成的部分：

## 6. 成长弧线（按剧情阶段：打捞初遇→共同生活→学园日常→真相揭露→告白与诀别→结局。每个阶段一个小节，用 3~6 条要点概括她的状态变化与标志事件，每条附 1 句最具代表性的原句）

全节不超过 50 行。

""" + DISCIPLINE


REDUCE7_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~6 节。现在只需产出以下一节，不要重复已完成的部分：

## 7. 雷区与边界（分小节归纳：被叫废物/萝卜子时、被质疑没有心时、被当作物品买卖时、被偷看日志时、被质疑性能时——各附反应原话；最后单列一个「她绝对不会做的事」小节）

每个小节不超过 10 行，全节不超过 45 行。

""" + DISCIPLINE


REDUCE9_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~8 节（第 8 节已详细覆盖料理/学习/早晨刷牙/潜水/战斗/日志等场景的具体表现）。现在只需产出以下一节，重点在「时间感知规律」而非场景细节，不要与第 8 节重复：

## 9. 时间与作息感知（分小节：清晨低电压与起床气的一般规律、白天的学习与活动节奏、夜晚与日志时间、45 天倒计时与日历画叉行为、她对时间流逝与自身寿命的态度变化——各附 1~3 句代表性证据）

每个小节不超过 10 行，全节不超过 45 行。

""" + DISCIPLINE


REDUCE4_PROMPT = """你是角色扮演人设蒸馏器。下面是《ATRI -My Dear Moments-》完整剧本的分块证据提取结果（共 {chunk_count} 个片段，覆盖游戏全程）。

此前已蒸馏出第 1~9 节。现在只需产出以下内容，不要重复已完成的部分：

## 10. 世界观与身体设定（沉没小镇/星之海/学园废墟/伊甸/火箭/45 天时限/机械构造细节，每个主题一个小节，归纳要点并附证据）
## 11. 金句摘录（最具角色辨识度的 20~30 句原文台词，每句标注场景）

第 10 节每个小节不超过 12 行；第 11 节每句一行。

""" + DISCIPLINE


def split_chunks(lines: list[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + 1
        if current and size + add > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks


async def call_llm(
    client: httpx.AsyncClient,
    key: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                    # deepseek-flash 默认启用思维链，会把 max_tokens 耗在思考上
                    # 导致正文为空；与 toolbox/vision.py 一致地禁用。
                    "thinking": {"type": "disabled"},
                },
                timeout=300.0,
            )
            response.raise_for_status()
            body = response.json()
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
            if content and content.strip():
                return content.strip()
            last_error = RuntimeError(f"空回复：{str(body)[:200]}")
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败：{last_error!r}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="角色文本蒸馏（map-reduce）")
    parser.add_argument("--character", default="ATRI")
    parser.add_argument(
        "--source", default=str(ROOT / "data" / "runtime" / "distill-source.md")
    )
    parser.add_argument("--chunk-chars", type=int, default=18000)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    key = load_env_value("TOOLBOX_VISION_API_KEY")
    model = load_env_value("TOOLBOX_VISION_MODEL") or "deepseek-flash"
    base = load_env_value("TOOLBOX_VISION_BASE_URL") or "https://api.deepseek.com/v1"
    if not key:
        raise SystemExit("缺少 TOOLBOX_VISION_API_KEY（.env）")

    work_dir = ROOT / "data" / "runtime" / "distill-work"
    work_dir.mkdir(parents=True, exist_ok=True)

    source = Path(args.source).read_text(encoding="utf-8")
    chunks = split_chunks(source.splitlines(), args.chunk_chars)
    print(f"源文本 {len(source)} 字符 → {len(chunks)} 个分块（每块约 {args.chunk_chars} 字符）")

    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def run_map(index: int, chunk: str) -> None:
        path = work_dir / f"chunk_{index:02d}.md"
        if path.exists() and path.stat().st_size > 0:
            print(f"  map {index + 1}/{len(chunks)} 缓存命中，跳过")
            return
        async with semaphore:
            started = time.perf_counter()
            prompt = MAP_PROMPT.format(character=args.character) + "\n===== 剧本片段 =====\n\n" + chunk
            content = await call_llm(client, key, model, prompt, 6000)
            path.write_text(content, encoding="utf-8")
            print(f"  map {index + 1}/{len(chunks)} 完成（{time.perf_counter() - started:.0f}s，{len(content)} 字符）")

    async with httpx.AsyncClient(base_url=base) as client:
        await asyncio.gather(*(run_map(i, c) for i, c in enumerate(chunks)))
        print("map 阶段完成（全部命中缓存或新跑）")

        map_outputs = []
        for i in range(len(chunks)):
            path = work_dir / f"chunk_{i:02d}.md"
            map_outputs.append(f"----- 片段 {i + 1}/{len(chunks)} -----\n\n{path.read_text(encoding='utf-8')}")

        reduce_stages = [
            ("reduce2.md", REDUCE2_PROMPT),
            ("reduce3.md", REDUCE3_PROMPT),
            ("reduce4.md", REDUCE4_PROMPT),
            ("reduce6.md", REDUCE6_PROMPT),
            ("reduce7.md", REDUCE7_PROMPT),
            ("reduce9.md", REDUCE9_PROMPT),
        ]
        for filename, template in reduce_stages:
            out_path = work_dir / filename
            if out_path.exists() and out_path.stat().st_size > 0:
                print(f"reduce {filename} 缓存命中，跳过")
                continue
            prompt = template.format(
                chunk_count=len(chunks),
                map_outputs="\n\n".join(map_outputs),
            )
            print(f"reduce {filename} 开始（输入 {len(prompt)} 字符）……")
            started = time.perf_counter()
            content = await call_llm(client, key, model, prompt, 10000)
            out_path.write_text(content, encoding="utf-8")
            print(f"reduce {filename} 完成（{len(content)} 字符，用时 {time.perf_counter() - started:.0f}s）")

    print("全部 reduce 完成。合并顺序：distill_ATRI.md（1~5 节夏生小节，去尾行）→ reduce2（5 续）→ reduce6 → reduce7 → reduce3（第 8 节部分）→ reduce9 → reduce4（10~11 节）。")


if __name__ == "__main__":
    asyncio.run(main())
