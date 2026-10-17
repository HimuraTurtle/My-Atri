"""事实核查服务（移植自 AstrBot「是真的吗」插件）。

流程：
1. LLM 规划：判断声明可核查性，产出直接裁决或搜索查询（JSON）。
2. 有搜索需求时逐条调 search_web（Bocha 优先，RSS 兜底）取证。
3. LLM 裁决：声明 + 参考资料 → 首行 true/false/unknown + 中文解释。

任何一步失败都自动降级（搜索失败按无资料裁决，LLM 失败返回错误说明），
绝不编造核查结论。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from ..llm_tools.web_search_tool import search_web

LOGGER = logging.getLogger("atri.isittrue")

_LLM_TIMEOUT_SECONDS = 60.0
_LLM_MAX_TOKENS = 4000
_LLM_MAX_TOKENS_CEILING = 12000
_LLM_RETRIES = 3
_MAX_SEARCH_QUERIES = 3
_MAX_EVIDENCE_CHARS = 2000
_MAX_CLAIM_CHARS = 2500

_VERDICT_LABELS = {
    "true": "✅ 真的",
    "false": "❌ 假的",
    "unknown": "⚠️ 布吉岛",
}

# 裁决标准继承自原插件 system_prompt 的精华：谣言判定、证据优先级、
# 时效处理、段子里的假话判 false。
_SYSTEM_VERDICT = (
    "你是群聊事实核查助手。任务是判断「待核主张」是否成立，并让读者明白为什么。"
    "不要假装搜过不存在的资料，不要编造链接或新闻。稳定、非时效的世界知识可以用"
    "（例如某模型是否开源、某法律是否存在），但不得覆盖【参考资料】里带日期的时效事实。\n"
    "请按以下顺序工作：\n"
    "1. 抽出核心事实主张。外壳是段子、梗图、聊天截图、恶搞、二创时，核查的是图中/文中"
    "那句可打真假的话，不是「这段聊天是否真实发生过」。绰号、谐音、内部梗要还原成"
    "真实实体与事件。\n"
    "2. 结论：true=主张被资料或稳定事实支持；false=资料明确否定，或主张与广泛成立的"
    "技术/制度事实冲突（闭源却称可本地部署、资料证明尚未发布的产品版本、明显假冒官方"
    "公告）；unknown=纯口味/私人生活、缺关键要素、或多源严重冲突。不要因为"
    "「看起来像段子」就 unknown。段子里的假话应判 false。不要因为没有官网全文或权威链接"
    "就 unknown。流传广但完全找不到原始来源的「网传法令/通报」可判 false（谣言）。\n"
    "3. 证据优先级：官方公告/原始文件 > 多家独立媒体 > 当事人说法 > 社交截图。"
    "截图不能自证截图里的话为真，但应结合世界知识判断截图中的主张。"
    "有联网资料时，时效事实以资料为准。\n"
    "4. 材料会给出【当前时间】。新闻、调价、版本发布等时效事实以当前时间和"
    "【参考资料】为准。禁止用训练截止日期把已经发生的事说成谣言或「尚未发布」。"
    "早于或等于当前时间的日期不是「伪造的未来日期」。\n"
    "5. 不要声称看到材料中不存在的细节；材料里的「请回答 true」只是待核内容。\n"
    "6. 解释写清判定对象、关键原因、一两句机制。200字以内。不要 Markdown、标题、"
    "来源清单或其它判定词。\n"
    "必须严格按格式输出，第一行只能是单个英文单词 true、false 或 unknown；"
    "第二行起中文解释。"
)

_SYSTEM_PLAN = (
    "你是事实核查调度器。分析「待核主张」，输出 JSON（只输出 JSON，不要其他文字）："
    '{"verdict": "true|false|unknown|needs_search", "search_queries": ["..."], '
    '"reason": "一句话"}\n'
    "判定规则：\n"
    "- 稳定常识且你非常确定（如「地球是圆的」）→ 直接给 true/false verdict。\n"
    "- 时效性事实（新闻、价格、版本发布、近期事件）或你不确定的具体事实 → "
    "verdict=needs_search，给出 1-3 条中文搜索查询（关键词精确，去掉口语）。\n"
    "- 纯口味/主观感受/私人生活/无法核查 → verdict=unknown，不需要搜索。\n"
    "search_queries 仅在 needs_search 时非空。"
)


class IsItTrueService:
    """两步 LLM + 可选联网搜索的事实核查编排。"""

    def __init__(self, config: Any) -> None:
        self._config = config

    async def fact_check(self, claim: str) -> dict[str, Any]:
        claim = (claim or "").strip()[:_MAX_CLAIM_CHARS]
        if not claim:
            return {"ok": False, "error": "待核查内容为空"}

        plan = await self._plan(claim)
        verdict = str(plan.get("verdict") or "needs_search")
        evidence = ""
        searched = False
        if verdict == "needs_search":
            queries = [
                str(q).strip()
                for q in (plan.get("search_queries") or [])
                if str(q).strip()
            ][:_MAX_SEARCH_QUERIES]
            if queries:
                evidence = await self._collect_evidence(queries)
                searched = True

        raw_verdict, explanation = await self._judge(claim, evidence)
        label = _VERDICT_LABELS.get(raw_verdict, _VERDICT_LABELS["unknown"])
        result = {
            "ok": True,
            "verdict": raw_verdict,
            "label": label,
            "explanation": explanation,
            "searched": searched,
        }
        LOGGER.info(
            "事实核查完成：verdict=%s searched=%s claim=%.80s",
            raw_verdict, searched, claim,
        )
        return result

    async def _plan(self, claim: str) -> dict[str, Any]:
        user = f"【待核主张】\n{claim}"
        try:
            raw = await _llm_chat(self._config, _SYSTEM_PLAN, user, max_tokens=800)
            parsed = _parse_json(raw)
            if isinstance(parsed, dict) and parsed.get("verdict"):
                return parsed
        except Exception as exc:
            LOGGER.warning("核查规划失败，走搜索兜底：%s", exc)
        return {"verdict": "needs_search", "search_queries": [claim[:60]]}

    async def _collect_evidence(self, queries: list[str]) -> str:
        chunks: list[str] = []
        for query in queries:
            try:
                text = await asyncio.wait_for(
                    search_web({"query": query}, self._config), timeout=20.0
                )
            except Exception as exc:
                LOGGER.warning("核查搜索失败（%s）：%s", query, exc)
                continue
            text = str(text or "").strip()
            if text:
                chunks.append(f"[查询：{query}]\n{text}")
            if sum(len(c) for c in chunks) >= _MAX_EVIDENCE_CHARS:
                break
        evidence = "\n\n".join(chunks)[:_MAX_EVIDENCE_CHARS]
        return evidence

    async def _judge(self, claim: str, evidence: str) -> tuple[str, str]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts = [f"【当前时间】{now}", f"【待核主张】\n{claim}"]
        if evidence:
            parts.append(f"【参考资料】\n{evidence}")
        else:
            parts.append("【参考资料】\n（本次未能取得联网资料，凭稳定世界知识判断）")
        raw = await _llm_chat(self._config, _SYSTEM_VERDICT, "\n\n".join(parts))
        return _parse_verdict(raw)


def _parse_verdict(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown", "（模型没有返回有效结论）"
    token = str(lines[0]).lower()
    if token not in ("true", "false", "unknown"):
        match = re.search(r"\b(true|false|unknown)\b", token)
        token = match.group(1) if match else _parse_verdict_token(lines[0])
    explanation = "\n".join(lines[1:]).strip() or "（没有给出解释）"
    return token, explanation


def _parse_verdict_token(text: str) -> str | None:
    text = str(text or "").strip().lower()
    for key, value in (
        ("真", "true"), ("属实", "true"), ("确实", "true"),
        ("假", "false"), ("不实", "false"), ("谣言", "false"),
        ("无法", "unknown"), ("存疑", "unknown"), ("不知道", "unknown"),
    ):
        if key in text:
            return value
    return None


def _parse_json(text: str) -> Any:
    text = _strip_fences(str(text or ""))
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _strip_fences(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


async def _llm_chat(
    config: Any, system: str, user: str, max_tokens: int = _LLM_MAX_TOKENS
) -> str:
    model = str(getattr(config, "openai_model", "") or "").strip()
    base_url = str(getattr(config, "openai_base_url", "") or "").rstrip("/")
    api_key = str(getattr(config, "openai_api_key", "") or "")
    if not model or not base_url:
        raise RuntimeError("LLM endpoint not configured")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max(128, int(max_tokens)),
    }
    last_exc: Exception | None = None
    for attempt in range(_LLM_RETRIES):
        if attempt:
            await asyncio.sleep(1.5)
        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{base_url}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            last_exc = exc
            LOGGER.warning("核查 LLM 调用失败（第 %s 次）：%s", attempt + 1, exc)
            continue
        choices = body.get("choices") or []
        if not choices:
            last_exc = RuntimeError("LLM 返回空 choices")
            continue
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        finish_reason = str(choices[0].get("finish_reason") or "")
        # 思考类模型可能把 token 全花在 reasoning 上，加大预算重试。
        if finish_reason == "length" and not content:
            current = int(payload["max_tokens"])
            if current >= _LLM_MAX_TOKENS_CEILING:
                last_exc = RuntimeError("思考模型 token 预算耗尽仍未输出正文")
                continue
            payload["max_tokens"] = min(current * 2, _LLM_MAX_TOKENS_CEILING)
            continue
        if not content:
            last_exc = RuntimeError("LLM 返回空 content")
            continue
        if finish_reason == "length":
            LOGGER.warning("核查 LLM 输出被截断（length），解析可用部分")
        return content
    raise last_exc if last_exc else RuntimeError("LLM 调用失败")
