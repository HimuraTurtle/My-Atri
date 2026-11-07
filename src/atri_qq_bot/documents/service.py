"""通用文档投递队列：send_document 落盘后排队，OneBot 层在文字回复
发出后消费发送（与论文整理/漫画/绘画同一交付模式）。"""

from __future__ import annotations

from pathlib import Path


class DocumentsService:
    def __init__(self) -> None:
        self._deliveries: dict[str, list[Path]] = {}

    def queue_delivery(self, conversation_id: str, path: Path) -> None:
        queue = self._deliveries.setdefault(str(conversation_id), [])
        queue.append(Path(path))

    def consume_deliveries(self, conversation_id: str) -> list[Path]:
        return self._deliveries.pop(str(conversation_id), [])
