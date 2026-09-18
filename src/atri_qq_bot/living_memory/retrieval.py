"""LivingMemory 检索：jieba 分词 + BM25 + 向量余弦 + RRF 融合。

BM25 依赖存储层预分词的 FTS5 表；向量检索对小规模记忆做精确余弦扫描。
两路结果用 RRF（Reciprocal Rank Fusion）合并，再乘时间衰减分与重要度，
天然把"最近提过的、重要的"记忆排前面。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from ..retrieval.embeddings import OllamaEmbeddingClient
from ..runtime.inference_lock import InferenceResourceBusyError, inference_resource_lease_sync

LOGGER = logging.getLogger("atri.living_memory")

try:
    import jieba
except ImportError:  # pragma: no cover - venv 预期已安装，兜底保证可运行
    jieba = None

RRF_K = 60.0

_STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "那", "他", "她", "它", "们", "什么",
    "怎么", "这个", "那个", "还是", "就是", "但是", "然后", "所以", "因为",
    "这样", "那样", "如果", "可以", "现在", "知道", "觉得", "应该", "可能",
}


def tokenize(text: str, remove_stopwords: bool = True) -> list[str]:
    """中文优先 jieba 分词；缺库时退化为字符 bigram，保证检索不至于失效。"""
    value = str(text or "").strip()
    if not value:
        return []
    if jieba is not None:
        raw = [token.strip() for token in jieba.lcut(value)]
    else:
        compact = value.replace(" ", "")
        raw = [compact[i : i + 2] for i in range(len(compact) - 1)] or list(value)
    tokens: list[str] = []
    for token in raw:
        if not token or token.isspace():
            continue
        if not any(ch.isalnum() for ch in token):
            continue
        if len(token) == 1 and token.isascii():
            continue
        if remove_stopwords and token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rrf_fuse(
    *ranked_lists: list[int], limit: int
) -> list[tuple[int, float]]:
    """多路结果按名次融合：score = Σ 1/(k+rank)。输入是各路 memory_id 有序列表。"""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, memory_id in enumerate(ranked):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return fused[: max(1, int(limit))]


class HybridRetriever:
    """BM25 主路 + 向量副路（可选）的混合检索器。"""

    def __init__(
        self,
        store: Any,
        *,
        embedding_enabled: bool = True,
        embedding_model: str = "bge-m3:latest",
        embedding_base_url: str = "http://127.0.0.1:11434",
        embedding_timeout_seconds: float = 3.0,
    ) -> None:
        self.store = store
        self.embedding_enabled = bool(embedding_enabled)
        self.embedding_model = str(embedding_model)
        self.embedding_client = OllamaEmbeddingClient(
            self.embedding_model, embedding_base_url, embedding_timeout_seconds
        )

    def search(
        self,
        session_id: str,
        query_text: str,
        limit: int = 4,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        query_tokens = tokenize(query_text)
        if not query_tokens:
            return []

        bm25_rows = self.store.search_fts(query_tokens, session_id, limit=8)
        bm25_ids = [int(row["memory_id"]) for row in bm25_rows]

        vector_ids: list[int] = []
        if self.embedding_enabled and self.store.embedding_exists(self.embedding_model):
            try:
                with inference_resource_lease_sync(
                    f"embedding:{self.embedding_model}", timeout_seconds=0.0
                ):
                    query_vector = self.embedding_client.embed(query_text)
                vector_ids = self._vector_rank(session_id, query_vector)
            except InferenceResourceBusyError:
                pass  # 视觉等占用本地 GPU 时直接退回纯 BM25。
            except Exception:
                pass  # embedding 副路失败不影响主路。

        fused = rrf_fuse(bm25_ids, vector_ids, limit=limit) if (bm25_ids or vector_ids) else []
        if not fused:
            return []

        rows: dict[int, dict[str, Any]] = {
            int(row["memory_id"]): row
            for row in bm25_rows
        }
        for memory_id, _score in fused:
            if memory_id not in rows:
                fetched = self.store.get_memory(memory_id)
                if fetched is not None:
                    rows[memory_id] = fetched

        results: list[dict[str, Any]] = []
        for memory_id, rrf_score in fused:
            row = rows.get(memory_id)
            if row is None:
                continue
            ttl_days = float(row.get("ttl_days") or 7.0)
            decay_type = str(row.get("decay_type") or "exponential")
            last_accessed = float(row.get("last_accessed_at") or row.get("created_at") or now)
            days_since = max(0.0, (now - last_accessed) / 86400.0)
            if days_since >= ttl_days:
                continue
            from .atoms import compute_decay_score

            temporal = compute_decay_score(decay_type, ttl_days, days_since)
            importance = max(0.0, min(1.0, float(row.get("importance") or 0.5)))
            final_score = rrf_score * (0.4 + 0.6 * temporal) * (0.5 + 0.5 * importance)
            row = dict(row)
            row["_score"] = round(final_score, 4)
            row["_temporal"] = round(temporal, 3)
            results.append(row)
        return results

    def _vector_rank(self, session_id: str, query_vector: list[float], limit: int = 8) -> list[int]:
        entries = self.store.load_embeddings(session_id, self.embedding_model)
        if not entries:
            return []
        scored = [
            (cosine_similarity(query_vector, vector), memory_id)
            for memory_id, vector in entries
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [memory_id for score, memory_id in scored[:limit] if score > 0.15]
