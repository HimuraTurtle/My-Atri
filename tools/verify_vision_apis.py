# -*- coding: utf-8 -*-
"""视觉链路 API 可用性验证脚本。

逐项实测生产环境依赖的外部 API 与本地能力（只读，不触碰 8765 端口）：
  1. RapidOCR 本地引擎
  2. DeepSeek 视觉模型（api.deepseek.com，读 .env 真实 key）
  3. trace.moe 反向搜图（api.trace.moe，multipart 上传）
  4. AniList GraphQL 标题查询（反搜命中的标题增强链路）
  5. SauceNAO（仅确认空 key 跳过，不发请求）
  6. AnimeTrace 角色库人脸匹配（api.animetrace.com，免费公开服务）

用法：.venv\\Scripts\\python.exe tools\\verify_vision_apis.py [图片路径]
不传图片路径时使用 data/assets/goodnight.jpg。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = "  [PASS]"
FAIL = "  [FAIL]"
SUMMARY: list[tuple[str, bool, str]] = []


def load_env_value(key: str) -> str:
    text = (ROOT / ".env").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def report(name: str, ok: bool, detail: str) -> None:
    SUMMARY.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}：{detail}")


def build_analyzer(vision_key: str):
    from atri_qq_bot.config import BotConfig
    from atri_qq_bot.toolbox import ToolAnalyzer

    scratch = ROOT / "data" / "runtime" / "verify-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    config = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=87660,
        reply_mode="smart",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4.1-mini",
        temperature=0.8,
        max_tokens=350,
        memory_path=scratch / "memory.json",
    )
    config = replace(
        config,
        toolbox_vision_enabled=True,
        toolbox_ocr_enabled=True,
        toolbox_vision_model=load_env_value("TOOLBOX_VISION_MODEL") or "deepseek-flash",
        toolbox_vision_base_url=load_env_value("TOOLBOX_VISION_BASE_URL")
        or "https://api.deepseek.com/v1",
        toolbox_vision_api_key=vision_key,
        saucenao_api_key=load_env_value("SAUCENAO_API_KEY"),
    )
    return ToolAnalyzer(config)


async def test_ocr(analyzer, image: bytes) -> None:
    try:
        started = time.perf_counter()
        result = await analyzer._extract_image_ocr(image)
        elapsed = time.perf_counter() - started
        text = (result.text or "").strip()
        report(
            "RapidOCR 本地引擎",
            bool(text) and result.average_confidence > 0,
            f"{elapsed:.2f}s，{len(text)} 字，平均置信度 {result.average_confidence:.3f}"
            + (f"，文字：{text[:40]}" if text else f"，error={result.error}"),
        )
    except Exception as exc:
        report("RapidOCR 本地引擎", False, f"异常：{exc!r}")


async def test_deepseek_vision(analyzer, image: bytes) -> None:
    import base64

    try:
        encoded = base64.b64encode(image).decode("ascii")
        started = time.perf_counter()
        response = await analyzer._analyze_image_with_openai_compatible(
            [encoded],
            ["image/jpeg"],
            "你是测试，请用一句话描述图片。",
            "这张图里有什么？",
        )
        elapsed = time.perf_counter() - started
        content = (response.content or "").strip()
        usage = response.metrics.get("usage") or {}
        finish = response.metrics.get("finish_reason")
        completion_tokens = usage.get("completion_tokens")
        report(
            "DeepSeek 视觉模型",
            bool(content) and elapsed < 30,
            f"{elapsed:.1f}s，finish_reason={finish}，"
            f"completion_tokens={completion_tokens}，内容：{content[:60]}",
        )
    except Exception as exc:
        report("DeepSeek 视觉模型", False, f"异常：{exc!r}")


async def test_tracemoe(image: bytes) -> None:
    import httpx

    try:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                "https://api.trace.moe/search",
                files={"image": ("image.jpg", image, "image/jpeg")},
                headers={"User-Agent": "atri-qq-bot/1.0"},
            )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        body = response.json()
        results = body.get("result") or []
        lines = [
            f"HTTP 200，{elapsed:.1f}s，frameCount={body.get('frameCount')}，命中数={len(results)}"
        ]
        for item in results[:3]:
            similarity = float(item.get("similarity") or 0)
            lines.append(
                f"    anilist={item.get('anilist')} 相似度={similarity:.2f} "
                f"集数={item.get('episode')} 文件={str(item.get('filename') or '')[:48]}"
            )
        report("trace.moe 反向搜图", True, "；".join(lines))
    except Exception as exc:
        report("trace.moe 反向搜图", False, f"异常：{exc!r}")


async def test_anilist(anilist_id: int = 16498) -> None:
    import httpx

    try:
        query = (
            "query ($id: Int) { Media(id: $id, type: ANIME) "
            "{ title { romaji native english } } }"
        )
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"id": anilist_id}},
            )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        title = (response.json().get("data") or {}).get("Media", {}).get("title") or {}
        report(
            "AniList GraphQL",
            bool(title.get("native")),
            f"HTTP 200，{elapsed:.1f}s，id={anilist_id} -> "
            f"{title.get('native')} / {title.get('romaji')}",
        )
    except Exception as exc:
        report("AniList GraphQL", False, f"异常：{exc!r}")


async def test_saucenao(image: bytes) -> None:
    import httpx

    key = load_env_value("SAUCENAO_API_KEY")
    if not key:
        report(
            "SauceNAO 兜底",
            True,
            "key 为空（代码按 if not saucenao_key 跳过，不影响 trace.moe 主链路）",
        )
        return
    try:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://saucenao.com/search.php",
                data={
                    "output_type": 2,
                    "api_key": key,
                    "db": 999,
                    "numres": 3,
                },
                files={"file": ("image.jpg", image, "image/jpeg")},
            )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        body = response.json()
        results = body.get("results") or []
        header = (results[0].get("header") or {}) if results else {}
        status_text = str(header.get("status") or "")
        lines = [
            f"HTTP 200，{elapsed:.1f}s，返回结果数={len(results)}",
        ]
        if results:
            similarity = header.get("similarity")
            index_name = str(header.get("index_name") or "")[:40]
            lines.append(f"顶部相似度={similarity}，索引={index_name}")
        detail = "；".join(lines)
        if status_text and status_text != "0":
            detail += f"；header.status={status_text}"
        report("SauceNAO 兜底", len(results) >= 0 and not status_text or status_text == "0", detail)
    except Exception as exc:
        report("SauceNAO 兜底", False, f"异常：{exc!r}")


async def test_animetrace(image: bytes) -> None:
    import httpx

    from atri_qq_bot.toolbox.helpers import _animetrace_jpeg

    enabled = load_env_value("ANIMETRACE_ENABLED").strip().lower()
    if enabled in {"false", "0", "no", "off"}:
        report("AnimeTrace 角色库", True, "ANIMETRACE_ENABLED=false（代码按开关跳过该供应商）")
        return
    try:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=15.0) as client:
            list_response = await client.get("https://api.animetrace.com/v1/model/list")
        list_response.raise_for_status()
        models = (list_response.json() or {}).get("data") or []
        enabled_models = [m for m in models if isinstance(m, dict) and m.get("enabled")]
        chosen = next(
            (m for m in enabled_models if m.get("default")),
            enabled_models[0] if enabled_models else None,
        )
        if not chosen or not chosen.get("id"):
            report("AnimeTrace 角色库", False, "model/list 未返回可用模型")
            return
        model_id = str(chosen["id"])
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.animetrace.com/v1/search",
                data={"is_multi": "1", "model": model_id, "ai_detect": "0"},
                files={"file": ("image.jpg", _animetrace_jpeg(image), "image/jpeg")},
            )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        body = response.json()
        try:
            code = int(body.get("code") or 0)
        except (TypeError, ValueError):
            code = -1
        faces = body.get("data") or []
        lines = [
            f"HTTP 200，{elapsed:.1f}s，model={model_id}，code={code}，人脸命中数={len(faces)}"
        ]
        for face in faces[:3]:
            characters = [c for c in (face.get("character") or []) if isinstance(c, dict)]
            top = characters[0] if characters else {}
            lines.append(
                f"    「{top.get('character')}」（{top.get('work')}）候选 {len(characters)} 项"
            )
        # 与 helpers._ANIMETRACE_SUCCESS_CODES 同源：17722/17728/17731 为限流/用量码
        ok = code in {0, 200, 17720, 17721}
        if not ok:
            lines.append(f"    zh_message={str(body.get('zh_message') or '')[:60]}")
        report("AnimeTrace 角色库", ok, "；".join(lines))
    except Exception as exc:
        report("AnimeTrace 角色库", False, f"异常：{exc!r}")


async def test_end_to_end(analyzer, image: bytes) -> None:
    try:
        started = time.perf_counter()
        result = await analyzer._analyze_image_bytes(image, "api-verify-goodnight.jpg")
        elapsed = time.perf_counter() - started
        findings_preview = " ｜ ".join(item[:36] for item in result.findings[:4])
        report(
            "端到端全链路（_analyze_image_bytes）",
            result.visual_status in {"verified", "ocr_only", "reverse_only"},
            f"{elapsed:.1f}s，visual_status={result.visual_status}，"
            f"read_level={result.read_level}，"
            f"reverse_hit={result.visual_reverse_hit}，"
            f"OCR置信度={result.visual_ocr_confidence:.3f}；findings：{findings_preview}",
        )
    except Exception as exc:
        report("端到端全链路", False, f"异常：{exc!r}")


async def main() -> int:
    image_path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else ROOT / "data" / "assets" / "goodnight.jpg"
    )
    image = image_path.read_bytes()
    print(f"测试图片：{image_path.name}（{len(image)} 字节）")
    vision_key = load_env_value("TOOLBOX_VISION_API_KEY")
    print(
        f"视觉配置：model={load_env_value('TOOLBOX_VISION_MODEL')} "
        f"base={load_env_value('TOOLBOX_VISION_BASE_URL')} "
        f"key={'已配置' if vision_key else '缺失'}"
    )
    print()

    analyzer = build_analyzer(vision_key)
    await test_ocr(analyzer, image)
    await test_deepseek_vision(analyzer, image)
    await test_tracemoe(image)
    await test_anilist()
    await test_saucenao(image)
    await test_animetrace(image)
    await test_end_to_end(analyzer, image)

    print()
    failed = [item for item in SUMMARY if not item[1]]
    print(f"===== 汇总：{len(SUMMARY) - len(failed)}/{len(SUMMARY)} 项可用 =====")
    for name, ok, detail in SUMMARY:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
