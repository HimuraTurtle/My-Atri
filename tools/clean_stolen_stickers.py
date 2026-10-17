# -*- coding: utf-8 -*-
"""盗取表情包库清理脚本：重新审查全部 steal_* 文件，删除混入的非表情包。

背景：旧版分类提示词对实拍照片/高清美图过于宽容，库里混入了宠物照、
人物照、欣赏图等。本脚本用强化后的分类标准对库内所有偷取文件做一次
全量重审：

  1. 确定性规则（零成本）：JPEG 长边 >1200px 或带相机 EXIF → 直接判照片；
  2. VLM 重分类：deepseek-flash 按新提示词判定，approved=false 的删除。

默认 dry-run 只输出报告；加 --apply 才真正删除（图片 + sidecar JSON）。

用法：
  .venv\\Scripts\\python.exe tools\\clean_stolen_stickers.py            # 预览
  .venv\\Scripts\\python.exe tools\\clean_stolen_stickers.py --apply    # 执行删除
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atri_qq_bot.meme_stealer.classify import (  # noqa: E402
    VisionSettings,
    classify_image_bytes,
    looks_like_photo,
)
from atri_qq_bot.stickers import IMAGE_EXTENSIONS, STEAL_FILE_PREFIX  # noqa: E402


def load_env_value(key: str) -> str:
    text = (ROOT / ".env").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def build_vision_settings() -> VisionSettings:
    from atri_qq_bot.config import BotConfig

    base = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=87660,
        reply_mode="smart",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4.1-mini",
        temperature=0.8,
        max_tokens=350,
        memory_path=ROOT / "data" / "runtime" / "clean-scratch" / "memory.json",
    )
    return VisionSettings.from_config(
        replace(
            base,
            toolbox_vision_enabled=True,
            toolbox_vision_model=load_env_value("TOOLBOX_VISION_MODEL") or "deepseek-flash",
            toolbox_vision_base_url=load_env_value("TOOLBOX_VISION_BASE_URL")
            or "https://api.deepseek.com/v1",
            toolbox_vision_api_key=load_env_value("TOOLBOX_VISION_API_KEY"),
        )
    )


def load_sidecar_desc(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".json")
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        return str(meta.get("desc") or "").strip()
    except Exception:
        return ""


async def review_file(
    path: Path, settings: VisionSettings, semaphore: asyncio.Semaphore
) -> tuple[Path, str, str]:
    """返回 (路径, 判定, 理由)。判定：keep / remove / error。"""
    data = path.read_bytes()
    reason = looks_like_photo(data)
    if reason:
        return path, "remove", f"照片特征：{reason}"
    async with semaphore:
        classification = await classify_image_bytes(settings, data)
    if classification.rejected:
        return path, "remove", "VLM 判定非表情包"
    if classification.approved:
        return path, "keep", classification.emotion
    # approved=False 且 rejected=False：VLM 不可用或解析失败。
    return path, "error", "VLM 分类失败（不删除）"


async def main() -> None:
    parser = argparse.ArgumentParser(description="清理盗取表情包库中的非表情包")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认只预览）")
    parser.add_argument("--concurrency", type=int, default=6, help="VLM 并发数")
    parser.add_argument(
        "--sticker-dir", type=Path, default=None, help="表情库目录（默认读 .env STICKER_DIR）"
    )
    args = parser.parse_args()

    sticker_dir = args.sticker_dir or ROOT / (load_env_value("STICKER_DIR") or "data/stickers")
    if not sticker_dir.is_absolute():
        sticker_dir = ROOT / sticker_dir
    if not sticker_dir.exists():
        print(f"表情库目录不存在：{sticker_dir}")
        sys.exit(1)

    settings = build_vision_settings()
    if not settings.enabled:
        print("视觉配置不可用（TOOLBOX_VISION_* 未启用），仅执行确定性规则。")

    files = sorted(
        path
        for path in sticker_dir.rglob(f"{STEAL_FILE_PREFIX}*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    print(f"扫描目录：{sticker_dir}")
    print(f"待审查偷取文件：{len(files)} 张\n")

    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    results: list[tuple[Path, str, str]] = []
    done = 0
    for coro in asyncio.as_completed(
        [review_file(path, settings, semaphore) for path in files]
    ):
        results.append(await coro)
        done += 1
        if done % 25 == 0 or done == len(files):
            print(f"  进度 {done}/{len(files)}")

    removed = [item for item in results if item[1] == "remove"]
    kept = [item for item in results if item[1] == "keep"]
    errors = [item for item in results if item[1] == "error"]

    print(f"\n审查结果：保留 {len(kept)} / 待删除 {len(removed)} / 失败保留 {len(errors)}\n")
    if removed:
        print("── 待删除清单 ──")
        for path, _, reason in removed:
            desc = load_sidecar_desc(path)
            print(f"  {path.parent.name}\\{path.name}  [{reason}]  {desc[:40]}")
    if errors:
        print("\n── VLM 失败（保留不删）──")
        for path, _, reason in errors:
            print(f"  {path.parent.name}\\{path.name}  [{reason}]")

    if not args.apply:
        print("\n（dry-run 预览，加 --apply 执行删除）")
        return

    deleted = 0
    for path, _, _ in removed:
        sidecar = path.with_suffix(path.suffix + ".json")
        try:
            path.unlink()
            if sidecar.exists():
                sidecar.unlink()
            deleted += 1
        except OSError as exc:
            print(f"删除失败：{path.name} {exc}")
    print(f"\n已删除 {deleted} 张（含 sidecar JSON）。")


if __name__ == "__main__":
    asyncio.run(main())
