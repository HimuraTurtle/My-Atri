"""MiniMax 绘图服务：调用 image-01 / image-01-live 生成图片并排队投递。

工具执行时把图片落盘到 data/minimax_draw/outputs 并排入待投递队列，
OneBot 层在文字回复发出后消费队列发图（与 anima/manga 同一模式）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.minimax_draw")

DEFAULT_BASE_URL = "https://api.minimax.cn"
DEFAULT_TIMEOUT_SECONDS = 120.0

SUPPORTED_ASPECT_RATIOS = (
    "1:1",
    "3:4",
    "4:3",
    "2:3",
    "3:2",
    "16:9",
    "9:16",
)

STYLE_PRESETS: dict[str, dict[str, Any]] = {
    # 实测 image-01 对形象特征（发色/瞳色/服装）的遵循度显著高于
    # image-01-live，auto 默认用 image-01；live 风格保留给显式选择。
    "auto": {"model": "image-01", "style": None},
    "manga": {"model": "image-01-live", "style": {"style_type": "漫画", "style_weight": 0.9}},
    "energetic": {"model": "image-01-live", "style": {"style_type": "元气", "style_weight": 0.9}},
    "watercolor": {"model": "image-01-live", "style": {"style_type": "水彩", "style_weight": 0.9}},
    "medieval": {"model": "image-01-live", "style": {"style_type": "中世纪", "style_weight": 0.9}},
    "realistic": {"model": "image-01", "style": None},
}


class MinimaxDrawError(RuntimeError):
    """MiniMax 图像生成失败。"""


class MinimaxDrawService:
    """MiniMax 绘图门面：生图、落盘、按会话排队。"""

    def __init__(self, config: Any) -> None:
        self._config = config
        root = Path(__file__).resolve().parents[3]
        self._output_dir = root / "data" / "minimax_draw" / "outputs"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._event_log = root / "data" / "minimax_draw" / "draw-events.jsonl"
        self._deliveries: dict[str, list[Path]] = {}

    def _append_event(self, record: dict[str, Any]) -> None:
        record["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._event_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def _api_settings(self) -> tuple[str, str, float]:
        base_url = str(
            getattr(self._config, "minimax_base_url", "") or DEFAULT_BASE_URL
        ).rstrip("/")
        api_key = str(getattr(self._config, "minimax_api_key", "") or "")
        timeout = max(
            30.0,
            float(getattr(self._config, "minimax_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
        return base_url, api_key, timeout

    async def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str = "",
        style: str = "",
    ) -> list[Path]:
        """调用 MiniMax 图像 API 并把结果落盘，返回图片路径列表。"""
        base_url, api_key, timeout = self._api_settings()
        if not api_key:
            raise MinimaxDrawError("未配置 MINIMAX_API_KEY")

        preset = STYLE_PRESETS.get((style or "auto").strip().lower())
        if preset is None:
            preset = STYLE_PRESETS["auto"]

        # MiniMax 限制 prompt 少于 1500 字符，精炼产物超长时截断兜底。
        if len(prompt) > 1400:
            prompt = prompt[:1400]

        payload: dict[str, Any] = {
            "model": preset["model"],
            "prompt": prompt,
            "response_format": "base64",
            # 官方服务端提示词优化器。实测（2026-10-13）：标签式 prompt +
            # image-01 + 该开关 = 7/7 特征全遵循；关闭时 5/6（黄昏氛围弱）。
            "prompt_optimizer": True,
        }
        ratio = (aspect_ratio or "").strip()
        if ratio and ratio in SUPPORTED_ASPECT_RATIOS:
            payload["aspect_ratio"] = ratio
        if preset["style"]:
            payload["style"] = preset["style"]

        start = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/v1/image_generation",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        elapsed = time.monotonic() - start
        try:
            body = response.json()
        except Exception as exc:
            raise MinimaxDrawError(f"响应解析失败（HTTP {response.status_code}）：{exc}") from exc

        base_resp = body.get("base_resp") or {}
        api_status = base_resp.get("status_code")
        if response.status_code != 200 or api_status not in (None, 0):
            reason = base_resp.get("status_msg") or f"HTTP {response.status_code}"
            raise MinimaxDrawError(f"生成失败：{reason}")

        images = (body.get("data") or {}).get("image_base64") or []
        if not images:
            raise MinimaxDrawError("生成失败：接口未返回图片")

        import base64

        stamp = datetime.now()
        paths: list[Path] = []
        for index, encoded in enumerate(images):
            try:
                data = base64.b64decode(encoded)
            except Exception as exc:
                raise MinimaxDrawError(f"图片解码失败：{exc}") from exc
            digest = hashlib.sha1(f"{prompt}|{stamp.isoformat()}|{index}".encode()).hexdigest()[:8]
            path = self._output_dir / f"{stamp:%Y%m%d-%H%M%S}-{digest}.jpeg"
            path.write_bytes(data)
            paths.append(path)
        LOGGER.info(
            "minimax 绘图完成 model=%s style=%s ratio=%s images=%d elapsed=%.1fs prompt=%s",
            preset["model"],
            style or "auto",
            ratio or "default",
            len(paths),
            elapsed,
            prompt[:120],
        )
        return paths

    def queue_delivery(self, conversation_id: str, paths: list[Path]) -> None:
        queue = self._deliveries.setdefault(conversation_id, [])
        queue.extend(paths)

    def consume_deliveries(self, conversation_id: str) -> list[Path]:
        return self._deliveries.pop(conversation_id, [])

    async def tool_draw(
        self,
        *,
        conversation_id: str,
        prompt: str,
        aspect_ratio: str = "",
        style: str = "",
    ) -> dict[str, Any]:
        """LLM 工具 draw_image 的执行入口。"""
        import time as _time

        started = _time.monotonic()
        raw_prompt = str(prompt or "").strip()
        text = raw_prompt
        if not text:
            return {"ok": False, "message": "prompt 为空，请先向用户确认想画什么。"}
        used_search = False
        refine_failed = ""
        if bool(getattr(self._config, "minimax_prompt_refine_enabled", True)):
            try:
                from .prompt_refiner import refine_prompt

                text, used_search = await refine_prompt(self._config, text)
                text = text.strip() or raw_prompt
            except Exception as exc:
                refine_failed = str(exc)[:200]
                LOGGER.warning("提示词精炼失败，使用原始 prompt：%s", exc)
        try:
            paths = await self.generate(
                prompt=text,
                aspect_ratio=aspect_ratio,
                style=style,
            )
        except MinimaxDrawError as exc:
            LOGGER.warning("minimax 绘图失败：%s", exc)
            self._append_event(
                {
                    "event": "draw_failed",
                    "raw_prompt": raw_prompt,
                    "final_prompt": text,
                    "used_search": used_search,
                    "refine_failed": refine_failed,
                    "style": style or "auto",
                    "error": str(exc),
                    "elapsed_s": round(_time.monotonic() - started, 1),
                }
            )
            return {"ok": False, "message": f"图片生成失败：{exc}", "image_count": 0}
        self.queue_delivery(conversation_id, paths)
        self._append_event(
            {
                "event": "draw_ok",
                "conversation_id": conversation_id,
                "raw_prompt": raw_prompt,
                "final_prompt": text,
                "used_search": used_search,
                "refine_failed": refine_failed,
                "style": style or "auto",
                "aspect_ratio": aspect_ratio or "default",
                "image_count": len(paths),
                "elapsed_s": round(_time.monotonic() - started, 1),
            }
        )
        return {
            "ok": True,
            "message": "图片已生成，稍后会自动发送到当前会话。",
            "image_count": len(paths),
            "prompt_used": text,
            "prompt_refined_with_search": used_search,
        }
