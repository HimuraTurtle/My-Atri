"""Anima 绘图助手（移植自 astrbot_plugin_anima_master）。

自然语言 → 提示词管线（LLM 整理 Danbooru tags）→ 本机 ComfyUI
生成 → 图片投递回会话。触发方式为 LLM 工具（anima_draw），由聊天
模型识别绘画意图调用；图片发送沿用 manga 的延迟投递模式：工具执行
时图片落盘并入队，OneBot 层在文字回复发出后消费队列真正发图。

保持本文件轻量：agent_tools 子进程会以脚本方式导入
anima.prompts.prompt_presets，过重的顶层导入会拖累或破坏子进程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

# 原插件以 `anima.` 为顶级包名（脚本式导入），包内大量 try/except
# fallback 使用 `from anima.xxx import ...`。注册别名让这些 fallback
# 在 atri_qq_bot.anima 包模式下同样可用。
sys.modules.setdefault("anima", sys.modules[__name__])

LOGGER = logging.getLogger("atri.anima")

_ASTRBOT_SOURCE_CONFIG = Path(
    r"C:\Users\25786\.astrbot\data\config\astrbot_plugin_anima_master_config.json"
)

# 子进程读取的运行配置与插件数据都落在 data/anima/ 下。
_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "anima"
_RUNTIME_CONFIG = _DATA_DIR / "config.json"
_CONF_SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"


class AnimaService:
    """anima 插件服务门面：管理配置、服务容器与图片投递队列。"""

    def __init__(self, config: Any) -> None:
        self._bot_config = config
        self._lock = threading.Lock()
        self._pending_deliveries: dict[str, list[str]] = {}
        self._danbooru_tag_cache: dict[str, Any] = {}
        self._last_prompt_summary: dict[str, Any] = {}
        self._multi_semaphore = asyncio.Semaphore(1)

        raw_config = self._load_runtime_config()
        self.config = raw_config
        self._services = None
        self._runtime = None
        self._llm_bridge = None
        self._generation_task = None

    # ── 配置 ────────────────────────────────────────────────
    def _load_runtime_config(self) -> dict[str, Any]:
        """载入插件运行配置：schema 默认值打底，AstrBot 侧配置一次性导入。"""
        from .runtime.config_defaults import (
            flatten_config,
            migrate_prompt_defaults,
            schema_defaults,
        )
        from .prompts.prompt_presets import apply_config_preset

        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = flatten_config(schema_defaults(_CONF_SCHEMA_PATH))
        if _RUNTIME_CONFIG.exists():
            try:
                raw.update(flatten_config(json.loads(_RUNTIME_CONFIG.read_text("utf-8"))))
            except Exception:
                LOGGER.warning("anima 运行配置解析失败，回退默认配置")
        elif _ASTRBOT_SOURCE_CONFIG.exists():
            try:
                raw.update(
                    flatten_config(
                        json.loads(_ASTRBOT_SOURCE_CONFIG.read_text("utf-8-sig"))
                    )
                )
                LOGGER.info("anima 首次启动：已从 AstrBot 侧导入插件配置")
            except Exception:
                LOGGER.warning("anima AstrBot 配置导入失败，使用默认配置")
        raw = migrate_prompt_defaults(raw)
        raw = apply_config_preset(raw)
        self._write_runtime_config(raw)
        return raw

    def _write_runtime_config(self, flat_config: dict[str, Any]) -> None:
        try:
            _RUNTIME_CONFIG.write_text(
                json.dumps(dict(flat_config), ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as exc:
            LOGGER.warning("anima 运行配置写盘失败：%s", exc)

    # ── 服务容器 ─────────────────────────────────────────────
    @property
    def services(self):
        """惰性构建服务容器；首次访问时完成 ComfyUI 运行时装配。"""
        if self._services is None:
            self._build_services()
        return self._services

    def _build_services(self) -> None:
        from .compat import CompatContext
        from .runtime.service_container import build_services
        from .runtime.config_defaults import flatten_config
        from .prompts.prompt_presets import maybe_materialize_chiyo_preset

        flat_config = flatten_config(self.config)
        snapshot_path = _DATA_DIR / "plugin_data" / "chiyo_preset_base.json"
        base_config = maybe_materialize_chiyo_preset(
            None, base_config=flat_config, snapshot_path=snapshot_path
        )
        self.config = dict(base_config)
        compat_context = CompatContext(self._bot_config)

        def _get_bool(key: str, default: bool) -> bool:
            try:
                return bool(self.config.get(key, default))
            except Exception:
                return default

        def _get_int(key: str, default: int) -> int:
            try:
                return int(self.config.get(key, default))
            except (TypeError, ValueError):
                return default

        def _get_float(key: str, default: float) -> float:
            try:
                return float(self.config.get(key, default))
            except (TypeError, ValueError):
                return default

        def _get_str(key: str, default: str = "") -> str:
            value = self.config.get(key, default)
            return str(value if value is not None else default)

        def _shorten(text: str, limit: int = 1800) -> str:
            text = str(text or "").strip()
            if len(text) <= limit:
                return text
            return text[:limit].rstrip() + "\n...[已截断]"

        def _is_allowed(event: Any) -> bool:
            if _get_bool("admin_only", False):
                return False
            allowed = self.config.get("allowed_sender_ids", [])
            if isinstance(allowed, str):
                allowed = [allowed]
            allowed_set = {str(item).strip() for item in allowed or [] if str(item).strip()}
            if allowed_set and str(event.get_sender_id()) not in allowed_set:
                return False
            return True

        async def _build_prompt(
            event: Any,
            user_prompt: str,
            mode: str = "txt2img",
            *,
            multi_person: bool = False,
            original_user_prompt: str = "",
        ) -> str:
            result = await self._services.prompt_pipeline.build(
                event,
                user_prompt,
                mode,
                multi_person=multi_person,
                original_user_prompt=original_user_prompt,
            )
            self._last_prompt_summary.update(dict(result.summary))
            return result.final_prompt

        async def _generate(event: Any, prompt: str, **kwargs: Any) -> str:
            return await self._generate_flow(event, prompt, **kwargs)

        services = build_services(
            context=compat_context,
            config=dict(self.config),
            config_store=None,
            logger=LOGGER,
            danbooru_tag_cache=self._danbooru_tag_cache,
            get_bool=_get_bool,
            get_int=_get_int,
            get_float=_get_float,
            get_str=_get_str,
            shorten=_shorten,
            is_allowed=_is_allowed,
            build_prompt=_build_prompt,
            prompt_summary=lambda: dict(self._last_prompt_summary),
            generate=_generate,
            edit=lambda event, prompt: services.action_handler.edit(event, prompt),
            remove_bg=lambda event: services.action_handler.remove_bg(event),
            spell=lambda event: services.action_handler.spell(event),
            reverse=lambda event: services.action_handler.reverse(event),
        )
        self._services = services
        self._runtime = services.runtime
        self._llm_bridge = services.llm_tool_bridge
        self._generation_task = services.generation_task
        max_concurrent = max(1, int(self.config.get("multi_max_concurrent_generations", 1) or 1))
        self._multi_semaphore = asyncio.Semaphore(max_concurrent)

    # ── 生图主流程（复刻 main.py 的 _generate） ─────────────
    async def _generate_flow(
        self,
        event: Any,
        prompt: str,
        width: int | None = None,
        height: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        negative_prompt: str | None = None,
        multi_person: bool = False,
    ) -> str:
        if multi_person:
            await self._multi_semaphore.acquire()
        try:
            payload = await self._generation_task.generate_payload(
                event,
                prompt,
                width=width,
                height=height,
                steps=steps,
                cfg=cfg,
                negative_prompt=negative_prompt,
                multi_person=multi_person,
            )
            if payload.get("prompt_degraded"):
                event.notices.append(
                    "提示词优化服务不可用，本次已使用原始提示词继续生成；"
                    "结果可能不符合 Danbooru Tag 预期。"
                )
            if payload.get("ok") and payload.get("character_resolution_warning"):
                event.notices.append(
                    "未能可靠确认部分现有作品角色的 Danbooru Tag；"
                    "本次仍会发送结果，但角色外观可能偏离设定。"
                )
            result = await self._runtime.send_payload(event, payload)
            self._generation_task.record_delivery(
                payload.get("task_id"), payload.get("delivery")
            )
            return result
        finally:
            if multi_person:
                self._multi_semaphore.release()

    # ── LLM 工具入口 ────────────────────────────────────────
    def _make_event(
        self,
        conversation_id: str,
        sender_id: str = "",
        group_id: str = "",
    ):
        from .compat import CompatEvent

        def _deliver(path: str) -> None:
            with self._lock:
                self._pending_deliveries.setdefault(conversation_id, []).append(path)

        return CompatEvent(
            conversation_id=conversation_id,
            sender_id=sender_id or conversation_id,
            session_id=conversation_id,
            group_id=group_id,
            deliver=_deliver,
        )

    async def tool_generate(
        self,
        conversation_id: str,
        prompt: str,
        sender_id: str = "",
        group_id: str = "",
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        """anima_draw 工具入口：生图并把图片排入待投递队列。"""
        services = self.services
        event = self._make_event(conversation_id, sender_id, group_id)
        try:
            result = await asyncio.wait_for(
                self._llm_bridge.generate(
                    event,
                    str(prompt or "").strip(),
                    width=width,
                    height=height,
                ),
                timeout=max(
                    60.0, float(self.config.get("timeout", 300) or 300) + 90.0
                ),
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "message": "生图超时：ComfyUI 可能仍在排队或生成中，请稍后再试。",
            }
        except Exception as exc:
            LOGGER.exception("anima 生图失败：conversation=%s", conversation_id)
            return {"ok": False, "message": f"生图失败：{exc}"}
        summary = dict(self._last_prompt_summary)
        return {
            "ok": True,
            "message": str(result or "生成完成"),
            "notices": list(event.notices),
            "image_count": len(event.delivered_images),
            "prompt_tags": str(summary.get("final_prompt") or "")[:400],
        }

    async def tool_status(self) -> str:
        """anima_status 工具入口：返回 ComfyUI 连接与模型就绪状态。"""
        self.services
        return await self._llm_bridge.status(None)

    # ── 图片投递 ─────────────────────────────────────────────
    def consume_deliveries(self, conversation_id: str) -> list[str]:
        """OneBot 层消费待发图片路径（文字回复发出后调用）。"""
        with self._lock:
            return self._pending_deliveries.pop(conversation_id, [])


_SERVICE: AnimaService | None = None


def get_anima_service(config: Any) -> AnimaService | None:
    """全局单例工厂；anima_enabled 关闭时返回 None。"""
    global _SERVICE
    if not bool(getattr(config, "anima_enabled", False)):
        return None
    if _SERVICE is None:
        _SERVICE = AnimaService(config)
    return _SERVICE


def reset_anima_service() -> None:
    """重置单例（测试用）。"""
    global _SERVICE
    _SERVICE = None
