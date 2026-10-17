"""AstrBot Context 替身：LLM 调用与联网搜索桥接到 ATRi 基础设施。

插件通过 context.llm_generate(...) 做提示词优化/多人规划/视觉验证，
通过 context.get_config(umo=...) 判断联网搜索配置，prompt_research
则直接 import AstrBot 内部的 _tavily_search。本模块把三者替换为：
- llm_generate → ATRi 的 OpenAI 兼容接口（openai_base_url/model/api_key），
  返回带 completion_text 属性的响应对象；
- get_config → 固定返回带假 tavily key 的配置，由 bocha_api_key 是否
  存在决定搜索开关；
- tavily_search → Bocha Web Search API，返回带 title/url/snippet 的
  结果对象列表（与 tavily 返回结构对齐）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.anima.compat")

_BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"


@dataclass
class LLMResponse:
    completion_text: str


@dataclass(frozen=True)
class SearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""


class CompatContext:
    """插件的 Context 依赖替身，持有 ATRi BotConfig。"""

    def __init__(self, config: Any) -> None:
        self._config = config

    def _llm_settings(self) -> tuple[str, str, str]:
        config = self._config
        model = str(
            getattr(config, "anima_prompt_model", "") or getattr(config, "openai_model", "")
        ).strip()
        base_url = str(
            getattr(config, "anima_prompt_base_url", "")
            or getattr(config, "openai_base_url", "")
        ).rstrip("/")
        api_key = str(
            getattr(config, "anima_prompt_api_key", "")
            or getattr(config, "openai_api_key", "")
            or "",
        )
        return model, base_url, api_key

    async def llm_generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 700,
        **kwargs: Any,
    ) -> LLMResponse:
        model, base_url, api_key = self._llm_settings()
        if not model or not base_url:
            raise RuntimeError("anima LLM endpoint not configured")
        messages: list[dict[str, str]] = []
        if str(system_prompt or "").strip():
            messages.append({"role": "system", "content": str(system_prompt)})
        messages.append({"role": "user", "content": str(prompt or "")})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(128, int(max_tokens or 700)),
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = max(
            20.0, float(getattr(self._config, "anima_prompt_timeout_seconds", 90.0) or 90.0)
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            body = response.json()
        choices = body.get("choices") or []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "")
        return LLMResponse(completion_text=content.strip())

    async def get_current_chat_provider_id(self, umo: str = "") -> str:
        # 插件用该 ID 判断提示词优化链路是否可用；实际 LLM 调用不依赖它路由。
        return "atri"

    def get_config(self, umo: str = "") -> dict[str, Any]:
        """返回带 tavily key 开关的配置替身，控制插件的联网搜索分支。"""
        bocha_key = str(getattr(self._config, "bocha_api_key", "") or "").strip()
        return {
            "provider_settings": {
                "websearch_tavily_key": [bocha_key] if bocha_key else [],
            }
        }


async def tavily_search(provider_settings: dict[str, Any], payload: dict[str, Any]) -> list[SearchResult]:
    """AstrBot _tavily_search 替身：调 Bocha Web Search。

    provider_settings 来自 CompatContext.get_config()，其中
    websearch_tavily_key 实际存放 Bocha key。
    """
    keys = provider_settings.get("websearch_tavily_key") or []
    api_key = ""
    for item in keys:
        if str(item or "").strip():
            api_key = str(item).strip()
            break
    if not api_key:
        return []
    query = str(payload.get("query") or "").strip()
    if not query:
        return []
    try:
        count = int(payload.get("max_results") or 5)
    except (TypeError, ValueError):
        count = 5
    body_payload: dict[str, Any] = {
        "query": query,
        "summary": True,
        "count": max(1, min(8, count)),
        "page": 1,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            _BOCHA_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body_payload,
        )
        response.raise_for_status()
        body = response.json()
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    pages = data.get("webPages") or {}
    values = pages.get("value") or []
    results: list[SearchResult] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("summary") or "").strip()
        if not title and not snippet:
            continue
        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results
