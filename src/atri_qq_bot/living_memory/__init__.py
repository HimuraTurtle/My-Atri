"""LivingMemory 情节记忆引擎门面。

移植自 astrbot_plugin_livingmemory（AGPLv3，作者 lxfight）的核心设计：
对话攒批 -> LLM 总结成第一人称记忆 -> BM25/向量混合检索 -> TTL 衰减
与相似强化。与 ATRI 现有 L1/L2 画像记忆并存：本模块只负责"一起经历
过的事"，不碰 users.json。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..runtime.inference_lock import InferenceResourceBusyError, inference_resource_lease_sync
from .retrieval import HybridRetriever
from .store import LivingMemoryStore
from .summarizer import ConversationSummarizer

LOGGER = logging.getLogger("atri.living_memory")


class LivingMemory:
    """ATRI 的情节记忆：observe 喂消息、retrieve 出上下文、后台自总结。"""

    def __init__(self, config: Any, database_path: Path) -> None:
        self.enabled = bool(getattr(config, "living_memory_enabled", True))
        self.top_k = max(1, min(8, int(getattr(config, "living_memory_top_k", 4) or 4)))
        self.min_importance = max(
            0.0, min(1.0, float(getattr(config, "living_memory_min_importance", 0.25) or 0.25))
        )
        self.recent_count = max(0, min(4, int(getattr(config, "living_memory_recent_count", 1) or 1)))
        self.recent_max_age_hours = max(
            1.0, float(getattr(config, "living_memory_recent_max_age_hours", 72.0) or 72.0)
        )
        maintenance_interval_hours = max(
            0.5, float(getattr(config, "living_memory_maintenance_interval_hours", 6.0) or 6.0)
        )
        self._forget_delay_days = max(1.0, float(getattr(config, "living_memory_forget_delay_days", 7.0) or 7.0))
        purge_delay_days = max(
            self._forget_delay_days, float(getattr(config, "living_memory_purge_delay_days", 30.0) or 30.0)
        )
        self._purge_delay_days = purge_delay_days

        self.store = LivingMemoryStore(database_path)
        embedding_model = str(
            getattr(config, "memory_embedding_model", "bge-m3:latest") or "bge-m3:latest"
        )
        self.retriever = HybridRetriever(
            self.store,
            embedding_enabled=bool(getattr(config, "living_memory_embedding_enabled", True)),
            embedding_model=embedding_model,
            embedding_base_url=str(
                getattr(config, "memory_embedding_base_url", "http://127.0.0.1:11434")
                or "http://127.0.0.1:11434"
            ),
            embedding_timeout_seconds=float(
                getattr(config, "memory_embedding_timeout_seconds", 3.0) or 3.0
            ),
        )
        self.summarizer = ConversationSummarizer(
            self.store,
            config,
            trigger_messages=int(getattr(config, "living_memory_trigger_messages", 16) or 16),
            max_batch_messages=int(getattr(config, "living_memory_max_batch_messages", 40) or 40),
            llm_timeout_seconds=float(getattr(config, "living_memory_llm_timeout_seconds", 60.0) or 60.0),
            retrieve_handler=self._embed_content,
        )
        self._maintenance_interval = maintenance_interval_hours * 3600.0
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if not self.enabled:
            return
        self.summarizer.start()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="atri-living-memory-maintenance", daemon=True
        )
        self._maintenance_thread.start()
        LOGGER.info(
            "living memory 已启动：top_k=%d 触发阈值=%d条 维护间隔=%.1fh",
            self.top_k,
            int(getattr(self.summarizer, "trigger_messages", 16)),
            self._maintenance_interval / 3600.0,
        )

    def stop(self) -> None:
        self.summarizer.stop()
        self._maintenance_stop.set()

    def _maintenance_loop(self) -> None:
        # 启动先歇一会儿，避免和进程初始化抢资源。
        if self._maintenance_stop.wait(120.0):
            return
        while not self._maintenance_stop.wait(self._maintenance_interval):
            try:
                result = self.store.run_maintenance(
                    self._forget_delay_days, self._purge_delay_days
                )
                trimmed = self.store.trim_sessions()
                if any(result.values()) or trimmed:
                    LOGGER.info(
                        "living memory 维护完成：%s 清理buffer=%d", result, trimmed
                    )
            except Exception:
                LOGGER.warning("living memory 维护异常", exc_info=True)

    # ---------------- 对外接口 ----------------

    def observe(
        self,
        session_id: str,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
    ) -> None:
        if not self.enabled:
            return
        try:
            self.summarizer.observe(session_id, role, content, sender_id, sender_name)
        except Exception:
            LOGGER.debug("living memory observe 失败 session=%s", session_id, exc_info=True)

    def retrieve_context(
        self, session_id: str, user_text: str, now: float | None = None
    ) -> str:
        """混合检索 + 最近记忆，输出注入用的自然语言片段。"""
        if not self.enabled:
            return ""
        now = now if now is not None else time.time()
        lines: list[str] = []
        try:
            hits = self.retriever.search(session_id, user_text, limit=self.top_k, now=now)
        except Exception:
            LOGGER.warning("living memory 检索失败 session=%s", session_id, exc_info=True)
            hits = []
        for row in hits:
            if float(row.get("importance") or 0.0) < self.min_importance:
                continue
            self.store.touch_memory(int(row.get("memory_id") or 0), now)
            label = _source_label(row)
            lines.append(f"- {label}{str(row.get('content') or '').strip()}")
        if self.recent_count:
            try:
                recent = self.store.recent_memories(
                    session_id, self.recent_max_age_hours, limit=self.recent_count
                )
            except Exception:
                recent = []
            for row in recent:
                if any(str(row.get("memory_id")) == str(h.get("memory_id")) for h in hits):
                    continue
                label = _source_label(row)
                lines.append(f"- {label}{str(row.get('content') or '').strip()}")
        if not lines:
            return ""
        return (
            "更早一起经历过的事（自然想起就好，别刻意报时间；细节模糊就坦白记不清）：\n"
            + "\n".join(f"  {line}" for line in lines[: self.top_k + self.recent_count])
        )

    def recall_by_keywords(self, session_id: str, keywords: str, limit: int = 5) -> str:
        """模型主动召回工具的执行入口：返回原始记忆列表。"""
        if not self.enabled:
            return "长期记忆模块未启用。"
        try:
            hits = self.retriever.search(session_id, keywords, limit=max(1, min(8, int(limit))))
        except Exception:
            LOGGER.warning("living memory 主动召回失败", exc_info=True)
            return "召回长期记忆时出错了，先凭现有上下文回答，不要编造记忆。"
        if not hits:
            return "没有找到相关记忆。可以换个更具体或更宽泛的关键词再试一次。"
        now = time.time()
        parts: list[str] = []
        for row in hits:
            self.store.touch_memory(int(row.get("memory_id") or 0), now)
            label = _source_label(row)
            importance = float(row.get("importance") or 0.5)
            parts.append(f"- {label}{str(row.get('content') or '').strip()}（重要度{importance:.1f}）")
        return "找到这些相关记忆：\n" + "\n".join(parts)

    def _embed_content(self, content: str) -> list[float] | None:
        if not self.retriever.embedding_enabled:
            return None
        try:
            with inference_resource_lease_sync(
                f"embedding:{self.retriever.embedding_model}", timeout_seconds=0.0
            ):
                return self.retriever.embedding_client.embed(content)
        except InferenceResourceBusyError:
            return None
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        try:
            stats = self.store.stats()
        except Exception:
            stats = {"memories": 0, "active": 0, "buffered": 0, "embedded": 0}
        return {"enabled": self.enabled, **stats}


def _source_label(row: dict[str, Any]) -> str:
    start = float(row.get("source_start") or 0.0)
    end = float(row.get("source_end") or 0.0)
    label = str(row.get("source_label") or "").strip()
    if label:
        return f"（{label}）"
    if start > 0:
        try:
            if end > 0 and abs(end - start) > 45 * 60:
                return (
                    f"（{datetime.fromtimestamp(start):%m月%d日}到"
                    f"{datetime.fromtimestamp(end):%m月%d日}）"
                )
            return f"（{datetime.fromtimestamp(start):%m月%d日}）"
        except (ValueError, OSError):
            pass
    return ""
