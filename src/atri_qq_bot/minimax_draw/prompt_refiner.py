"""draw_image 的前置提示词精炼：LLM 扩写画面细节 + 人物外貌联网检索。

流程：
1. LLM 第一轮：判断请求里是否含需要核实外貌的具体人物/角色，
   同时产出初版英文详细提示词（JSON：search_queries + refined_prompt）。
2. 有检索需求时逐条调 search_web（Bocha 优先，RSS 兜底）。
3. LLM 第二轮：把检索到的人物外貌证据融入，输出最终英文提示词。

任何一步失败都退回上一步结果，绝不阻断画图主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.minimax_draw")

_LLM_TIMEOUT_SECONDS = 45.0
_LLM_MAX_TOKENS = 4000
_LLM_MAX_TOKENS_CEILING = 12000
_MAX_SEARCH_QUERIES = 2
_MAX_SNIPPET_CHARS = 2500
_LLM_RETRIES = 3

_SYSTEM_STEP1 = (
    "你是文生图提示词工程师。把用户的画图请求加工成英文标签式（tag-style）绘图提示词。"
    "输出 JSON（只输出 JSON，不要任何其他文字）："
    '{"search_queries": ["..."], "refined_prompt": "..."}\n'
    "search_queries 规则：请求中出现具体人物/角色/偶像/游戏动漫人物"
    "（例如某个 VTuber、游戏角色、明星），且你不确定其发色、瞳色、服装、"
    "标志性配饰等外貌特征时，给出 1-2 条搜索查询，并在查询里加上"
    "“外貌 特征 appearance”等词帮助检索；泛指角色（如“猫娘”“女仆”）或"
    "纯场景请求给空数组。\n"
    "refined_prompt 规则（重要，务必遵守）：\n"
    "1. 逗号分隔的短标签关键词，不是完整句子。正确示例："
    '"silver-white long hair, crimson red eyes, white dress, beach at sunset, '
    'soft anime style"；错误示例：A girl who has silver hair and stands on the beach.\n'
    "2. 请求包含具体人物/角色名时，第一个标签必须是其英文名或罗马音名"
    "（如画时崎狂三 → Kurumi Tokisaki；画雷姆 → Rem (Re:Zero)；画初音未来 → "
    "Hatsune Miku），紧跟其后的是其标志性外貌特征标签——绘图模型对知名角色"
    "有先验知识，人名+特征组合的还原度最高（2026-10-13 实测：人名+特征 5/5，"
    "纯特征 4.5/5，纯人名会丢失细节）。\n"
    "3. 人物形象特征标签（发色、瞳色、发型、服装、标志性配饰、机械/种族特征）"
    "必须放在人名之后、构图/光线/风格标签之前。\n"
    "4. 总长 250-600 字符。标签要具体（写 crimson red eyes 而不是 beautiful eyes），"
    "一个特征 2-5 个词。\n"
    "5. 拟人/拟兽角色必须写出具体动物特征标签（如 crab girl 要写 crab claws, "
    "red carapace）；机器人/人偶角色要写 mechanical joints, ball-jointed arms 等。\n"
    "6. 结尾加画风标签：anime style 或 soft anime illustration style（除非用户要求写实）。"
)

_SYSTEM_STEP2 = (
    "你是文生图提示词工程师。根据联网检索到的人物资料，输出最终英文标签式"
    "绘图提示词。只输出提示词本身，不要解释，总长度 250-600 字符。\n"
    "格式要求：逗号分隔的短标签关键词（不是完整句子）；第一个标签必须是该"
    "人物的英文名或罗马音名（绘图模型对知名角色有先验知识），人名后紧跟"
    "其标志性外貌特征标签（发色、瞳色、发型、服装、标志性元素、机械/种族"
    "特征）——检索到什么特征就写什么，构图、光线、风格标签放最后。\n"
    "注意：搜索结果可能与人物无关（撞名或跑题），只把确实属于该人物的"
    "外貌特征转成具体标签融入，忽略无关信息；其余画面标签沿用初版提示词。"
)


def _llm_settings(config: Any) -> tuple[str, str, str]:
    model = str(getattr(config, "openai_model", "") or "").strip()
    base_url = str(getattr(config, "openai_base_url", "") or "").rstrip("/")
    api_key = str(getattr(config, "openai_api_key", "") or "")
    return model, base_url, api_key


async def _llm_chat(config: Any, system: str, user: str, max_tokens: int = _LLM_MAX_TOKENS) -> str:
    model, base_url, api_key = _llm_settings(config)
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
            LOGGER.warning("精炼 LLM 调用失败（第 %s 次）：%s", attempt + 1, exc)
            continue
        choices = body.get("choices") or []
        if not choices:
            last_exc = RuntimeError("LLM 返回空 choices")
            LOGGER.warning("精炼 LLM 返回空 choices（第 %s 次）", attempt + 1)
            continue
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        finish_reason = str(choices[0].get("finish_reason") or "")
        # 思考类模型（deepseek-reasoner/v4 等）可能把全部 token 花在
        # reasoning 上导致正文为空，此时加大预算重试而不是放弃。
        if finish_reason == "length" and not content:
            current = int(payload["max_tokens"])
            if current >= _LLM_MAX_TOKENS_CEILING:
                last_exc = RuntimeError("思考模型 token 预算耗尽仍未输出正文")
                LOGGER.warning("精炼 LLM 思考耗尽预算（%s tokens）且无正文，放弃", current)
                continue
            payload["max_tokens"] = min(current * 2, _LLM_MAX_TOKENS_CEILING)
            last_exc = RuntimeError("思考模型 token 预算耗尽，正在加大预算重试")
            LOGGER.warning("精炼 LLM 思考耗尽预算（%s tokens），加倍重试", current)
            continue
        if not content:
            last_exc = RuntimeError("LLM 返回空 content")
            LOGGER.warning("精炼 LLM 返回空 content（第 %s 次）", attempt + 1)
            continue
        if finish_reason == "length":
            LOGGER.warning("精炼 LLM 输出被截断（length），尝试解析可用部分")
        return content
    raise last_exc if last_exc else RuntimeError("LLM 调用失败")


def _strip_fences(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_block(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end <= start:
        return None
    return text[start : end + 1]


def _salvage_parse(text: str) -> tuple[list[str], str] | None:
    """JSON 解析失败时的兜底：正则提取 search_queries / refined_prompt 字段值。"""
    prompt_match = re.search(r'"refined_prompt"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not prompt_match:
        return None
    draft = prompt_match.group(1)
    try:
        draft = json.loads(f'"{draft}"')
    except Exception:
        pass
    queries: list[str] = []
    for query_match in re.finditer(r'"((?:[^"\\]|\\.)+)"', text[: prompt_match.start()]):
        value = query_match.group(1)
        if len(value) > 4 and value not in ("search_queries", "refined_prompt"):
            queries.append(value)
    return queries, draft


def _parse_step1(text: str) -> tuple[list[str], str]:
    """解析第一轮输出；JSON 解析失败时兜底提取，最后才把全文当初版提示词。"""
    cleaned = _strip_fences(text)
    for candidate in (cleaned, _extract_json_block(cleaned) or ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            queries: list[str] = []
            raw_queries = data.get("search_queries")
            if isinstance(raw_queries, list):
                for query in raw_queries[:_MAX_SEARCH_QUERIES]:
                    value = str(query or "").strip()
                    if value:
                        queries.append(value)
            draft = str(data.get("refined_prompt") or "").strip() or cleaned
            return queries, draft
    salvaged = _salvage_parse(cleaned)
    if salvaged:
        LOGGER.warning("step1 JSON 解析失败，兜底提取成功")
        queries, draft = salvaged
        return queries[:_MAX_SEARCH_QUERIES], draft
    LOGGER.warning("step1 JSON 解析失败，以全文作为初版提示词")
    return [], cleaned


async def refine_prompt(config: Any, raw_prompt: str) -> tuple[str, bool]:
    """精炼提示词。返回 (最终 prompt, 是否使用了联网检索)。"""
    from ..llm_tools.web_search_tool import search_web

    try:
        step1 = await _llm_chat(config, _SYSTEM_STEP1, f"用户的画图请求：{raw_prompt}")
    except Exception as exc:
        LOGGER.warning("提示词精炼 LLM 调用失败，使用原始 prompt：%s", exc)
        return raw_prompt, False
    queries, draft = _parse_step1(step1)
    if not draft:
        draft = raw_prompt
    if not queries:
        return draft, False

    snippets: list[str] = []
    for query in queries:
        try:
            result = await search_web({"query": query, "mode": "web"}, config)
        except Exception as exc:
            LOGGER.warning("提示词精炼搜索失败（%s）：%s", query, exc)
            continue
        if result and not result.startswith("搜索失败"):
            snippets.append(result[:_MAX_SNIPPET_CHARS])
    if not snippets:
        return draft, False

    user_step2 = (
        f"原始请求：{raw_prompt}\n\n初版提示词：{draft}\n\n"
        "联网检索到的人物资料：\n" + "\n---\n".join(snippets)
    )
    try:
        final = await _llm_chat(config, _SYSTEM_STEP2, user_step2)
    except Exception as exc:
        LOGGER.warning("提示词精炼第二轮 LLM 失败，使用初版 prompt：%s", exc)
        return draft, True
    final = _strip_fences(final)
    return (final or draft), True
