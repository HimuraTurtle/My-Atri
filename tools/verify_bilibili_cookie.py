"""验证 .env 中 BILIBILI_COOKIE 的登录态与字幕可见性。

用法：.venv/Scripts/python.exe tools/verify_bilibili_cookie.py [视频BV号]
Cookie 会随浏览器端下线而失效，失效后重新复制 .env 再跑本脚本确认。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from atri_qq_bot.bilibili import BilibiliApiClient, BilibiliError


async def main() -> int:
    cookie = os.getenv("BILIBILI_COOKIE", "")
    if not cookie:
        print("❌ .env 未配置 BILIBILI_COOKIE")
        return 1
    print(f"BILIBILI_COOKIE 已加载（{len(cookie)} 字符）")

    client = BilibiliApiClient(cookie=cookie)
    print("客户端解析出的 Cookie 键:", sorted(client._cookie))
    try:
        payload = await client.request_json(
            "GET", "https://api.bilibili.com/x/web-interface/nav", expect_code_zero=False
        )
        data = payload.get("data") or {}
        if not data.get("isLogin"):
            print(f"❌ Cookie 无效或已失效（isLogin={data.get('isLogin')}），需从浏览器重新复制")
            return 1
        print(f"✅ 登录态有效：mid={data.get('mid')} 昵称={data.get('uname')}")

        bvid = sys.argv[1] if len(sys.argv) > 1 else "BV1GJ411x7h7"
        info = await client.get_video_info(bvid)
        tracks = await client.get_subtitle_tracks(info.bvid, info.cid)
        print(f"✅ 字幕接口可用：《{info.title}》检测到 {len(tracks)} 条字幕轨道")
        for track in tracks:
            kind = "AI" if track.ai_type else "人工"
            print(f"   - [{track.lan}] {track.lan_doc}（{kind}）")
        if not tracks:
            print("   （该视频无字幕，属正常现象；登录态字幕可见性以 nav 结果为准）")
        return 0
    except BilibiliError as exc:
        print(f"❌ 验证失败：{exc}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
