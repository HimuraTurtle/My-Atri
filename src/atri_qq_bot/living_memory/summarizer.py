"""LivingMemory 总结器：攒批触发后台 LLM 总结。

每个会话的消息先进 lm_messages buffer；未总结数量达到阈值后，由后台
线程用主对话模型把整段对话总结成第一人称结构化记忆写入存储。LLM
失败时消息留在 buffer，下一轮再试，绝不丢数据。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any

import httpx

from ..prompting import render_prompt
from .atoms import EPISODIC, FACTUAL, MemoryAtom, compute_ttl
from .retrieval import tokenize

LOGGER = logging.getLogger("atri.living_memory")

_PROMPT_NAME_GROUP = "living_memory_summary_group"
_PROMPT_NAME_PRIVATE = "living_memory_summary_private"


def try_fix_json(text: str) -> str:
    """修复模型输出中常见的截断/包裹问题，尽力还原成可解析 JSON。"""
    fixed = str(text or "").strip()
    if fixed.startswith("```json"):
        fixed = fixed[7:]
    elif fixed.startswith("```"):
        fixed = fixed[3:]
    if fixed.endswith("```"):
        fixed = fixed[:-3]
    fixed = fixed.strip()
    start = fixed.find("{")
    end = fixed.rfind("}")
    if start >= 0 and end > start:
        fixed = fixed[start : end + 1]
    if fixed.count('"') % 2 != 0:
        fixed += '"'
    open_brackets = fixed.count("[") - fixed.count("]")
    if open_brackets > 0:
        fixed += "]" * open_brackets
    open_braces = fixed.count("{") - fixed.count("}")
    if open_braces > 0:
        fixed += "}" * open_braces
    fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)
    return fixed


def parse_summary(text: str) -> dict[str, Any] | None:
    for candidate in (str(text or "").strip(), try_fix_json(text)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and str(data.get("summary") or "").strip():
            return data
    return None


class ConversationSummarizer:
    """单后台线程消费总结任务；observe() 只做轻量入队。"""

    def __init__(
        self,
        store: Any,
        config: Any,
        *,
        trigger_messages: int = 16,
        max_batch_messages: int = 40,
        llm_timeout_seconds: float = 60.0,
        retrieve_handler: Any = None,
    ) -> None:
        self.store = store
        self.config = config
        self.trigger_messages = max(6, int(trigger_messages))
        self.max_batch_messages = max(10, min(80, int(max_batch_messages)))
        self.llm_timeout_seconds = max(10.0, min(180.0, float(llm_timeout_seconds)))
        self._retrieve_handler = retrieve_handler
        self._queue: queue.Queue[str] = queue.Queue(maxsize=64)
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------- 对外接口 ----------------

    def observe(
        self,
        session_id: str,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
    ) -> None:
        text = str(content or "").strip()
        if len(text) < 2:
            return
        self.store.add_message(session_id, role, text, sender_id, sender_name)
        try:
            if self.store.pending_message_count(session_id) < self.trigger_messages:
                return
        except Exception:
            return
        with self._inflight_lock:
            if session_id in self._inflight:
                return
            self._inflight.add(session_id)
        try:
            self._queue.put_nowait(session_id)
        except queue.Full:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="atri-living-memory-summarizer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ---------------- 后台循环 ----------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                session_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._summarize_session(session_id)
            except Exception:
                LOGGER.warning("living memory 总结失败 session=%s", session_id, exc_info=True)
            finally:
                with self._inflight_lock:
                    self._inflight.discard(session_id)

    def _summarize_session(self, session_id: str) -> None:
        messages = self.store.load_messages(session_id, limit=self.max_batch_messages)
        if len(messages) < self.trigger_messages:
            return
        is_group = session_id.startswith("group:")
        conversation_text = self._format_conversation(messages)
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt_name = _PROMPT_NAME_GROUP if is_group else _PROMPT_NAME_PRIVATE
        try:
            prompt = render_prompt(
                prompt_name,
                conversation=conversation_text,
                current_date=current_date,
            )
        except Exception:
            LOGGER.warning("living memory 提示词缺失：%s", prompt_name, exc_info=True)
            return

        response_text = self._call_llm(prompt)
        data = parse_summary(response_text) if response_text else None
        if data is None:
            LOGGER.info(
                "living memory 总结输出无法解析 session=%s 长度=%d，留待下轮重试",
                session_id,
                len(response_text or ""),
            )
            return

        atom = self._build_atom(session_id, data, messages)
        embedding = self._maybe_embedding(atom.content)
        memory_id = self.store.add_memory(atom, embedding, self._embedding_model_name())
        self._reinforce_similar(atom, exclude_memory_id=memory_id)
        last_message_id = int(messages[-1]["message_id"])
        self.store.delete_messages(session_id, last_message_id)
        LOGGER.info(
            "living memory 已总结 session=%s 消息=%d 重要度=%.2f 主题=%s",
            session_id,
            len(messages),
            atom.importance,
            ",".join(atom.topics[:3]) or "无",
        )

    # ---------------- 组件 ----------------

    def _format_conversation(self, messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user")
            sender = str(message.get("sender_name") or message.get("sender_id") or "未知")
            created_at = float(message.get("created_at") or 0.0)
            time_text = (
                datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M") if created_at else ""
            )
            prefix = f"[Bot: 亚托莉 | {time_text}]" if role == "assistant" else f"[{sender} | {time_text}]"
            lines.append(f"{prefix} {str(message.get('content') or '').strip()}")
        return "\n".join(lines)

    def _call_llm(self, prompt: str) -> str | None:
        base_url = str(getattr(self.config, "openai_base_url", "") or "").rstrip("/")
        api_key = str(getattr(self.config, "openai_api_key", "") or "")
        model = str(getattr(self.config, "openai_model", "") or "")
        if not base_url or not model:
            LOGGER.warning("living memory 未配置主模型，跳过总结")
            return None
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是机器人亚托莉的记忆模块，只输出符合要求的 JSON，不输出任何其他文字。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.llm_timeout_seconds) as client:
                    response = client.post(
                        f"{base_url}/chat/completions", json=payload, headers=headers
                    )
                    response.raise_for_status()
                    body = response.json()
                choices = body.get("choices") if isinstance(body, dict) else None
                if not choices:
                    return None
                message = choices[0].get("message") or {}
                return str(message.get("content") or "")
            except Exception as exc:
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                LOGGER.warning("living memory LLM 调用失败：%s", str(exc)[:160])
                return None
        return None

    def _build_atom(
        self, session_id: str, data: dict[str, Any], messages: list[dict[str, Any]]
    ) -> MemoryAtom:
        importance = 0.5
        try:
            importance = max(0.05, min(1.0, float(data.get("importance") or 0.5)))
        except (TypeError, ValueError):
            pass
        timestamps = [float(m.get("created_at") or 0.0) for m in messages if m.get("created_at")]
        source_start = min(timestamps) if timestamps else time.time()
        source_end = max(timestamps) if timestamps else time.time()
        summary = str(data.get("summary") or "").strip()
        atom = MemoryAtom(
            session_id=session_id,
            content=summary[:600],
            atom_type=EPISODIC,
            topics=[str(t)[:40] for t in list(data.get("topics") or [])[:6]],
            key_facts=[str(f)[:200] for f in list(data.get("key_facts") or [])[:8]],
            importance=importance,
            sentiment=str(data.get("sentiment") or "neutral")[:16],
            source_start=source_start,
            source_end=source_end,
            source_label=datetime.fromtimestamp(source_end).strftime("%m月%d日"),
        )
        # 事实密度高且重要的对话，按事实类 TTL 存活更久。
        if importance >= 0.75 and len(atom.key_facts) >= 3:
            atom.atom_type = FACTUAL
            atom.ttl_days, atom.decay_type = compute_ttl(FACTUAL, importance, 0)
            atom.expires_at = atom.created_at + atom.ttl_days * 86400.0
        return atom

    def _embedding_model_name(self) -> str:
        return str(
            getattr(self.config, "memory_embedding_model", "bge-m3:latest") or "bge-m3:latest"
        )

    def _maybe_embedding(self, content: str) -> list[float] | None:
        if self._retrieve_handler is None:
            return None
        try:
            return self._retrieve_handler(content)
        except Exception:
            return None

    def _reinforce_similar(self, atom: MemoryAtom, exclude_memory_id: int = 0) -> None:
        """新记忆入库后，FTS 找相似旧记忆并强化（Jaccard on 分词）。

        必须排除刚入库的这条，否则新记忆总会和自己全匹配，TTL 被无谓拉长。
        """
        tokens = set(tokenize(atom.content))
        if len(tokens) < 3:
            return
        try:
            candidates = self.store.search_fts(
                sorted(tokens)[:8], atom.session_id, limit=5
            )
        except Exception:
            return
        for row in candidates:
            memory_id = int(row.get("memory_id") or 0)
            if memory_id <= 0 or memory_id == int(exclude_memory_id):
                continue
            existing_tokens = set(tokenize(str(row.get("content") or "")))
            if not existing_tokens:
                continue
            jaccard = len(tokens & existing_tokens) / max(1, len(tokens | existing_tokens))
            if jaccard >= 0.6:
                self.store.reinforce_memory(memory_id, atom.importance, time.time())
                LOGGER.info(
                    "living memory 强化相似记忆 memory=%d jaccard=%.2f", memory_id, jaccard
                )
