"""通用文档输出（send_document）：渲染、执行器、投递与 schema 契约测试。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import atri_qq_bot.documents
from atri_qq_bot.documents import DocumentsService, build_text_document
from atri_qq_bot.documents import get_documents_service as _original_get_service
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(**overrides):
    base = {
        "documents_enabled": True,
        "llm_tools_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


_CONTENT = """\
## 研究背景
大数据流格式杂、量大，清洗是预处理的关键一步。

## 主要内容
- 汇总缺失值、噪声、重复数据等方向的清洗做法
- 对比各方法的适用场景和局限

> 综述指出实时性与可扩展性仍是开放问题。

[原文链接](https://doi.org/10.3390/technologies11040101)
"""


class TestWriter:
    def test_build_docx_renders_all_block_types(self, tmp_path):
        result = build_text_document(
            title="大数据流清洗综述概要",
            content=_CONTENT,
            output_dir=tmp_path,
        )
        assert result["ok"] is True
        assert result["format"] == "docx"
        path = Path(result["path"])
        assert path.exists() and path.suffix == ".docx"
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "大数据流清洗综述概要" in document
        assert "研究背景" in document and "主要内容" in document  # 小节标题
        assert "汇总缺失值" in document  # 列表项
        assert "开放问题" in document  # 引用行
        assert 'Target="https://doi.org/10.3390/technologies11040101"' in rels
        assert "本文档由 ATRI 依据对话内容整理生成" in document

    def test_bare_url_line_becomes_link(self, tmp_path):
        result = build_text_document(
            title="链接测试",
            content="正文一段\nhttps://example.com/paper\n",
            output_dir=tmp_path,
        )
        with zipfile.ZipFile(Path(result["path"])) as archive:
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert 'Target="https://example.com/paper"' in rels

    def test_dash_without_space_stays_paragraph(self, tmp_path):
        # 「-5 分」这类负数行不应被误渲染成列表项。
        result = build_text_document(
            title="负数", content="温度 -5 度", output_dir=tmp_path
        )
        with zipfile.ZipFile(Path(result["path"])) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        assert "温度 -5 度" in document
        assert "• " not in document

    def test_docx_failure_falls_back_to_markdown(self, tmp_path, monkeypatch):
        def broken_build(*args, **kwargs):
            raise RuntimeError("docx broken")

        monkeypatch.setattr(
            "atri_qq_bot.documents.writer._build_docx", broken_build
        )
        result = build_text_document(
            title="降级测试", content="正文内容", output_dir=tmp_path
        )
        assert result["ok"] is True
        assert result["format"] == "md"
        text = Path(result["path"]).read_text(encoding="utf-8")
        assert "# 降级测试" in text
        assert "正文内容" in text

    def test_empty_title_or_content_rejected(self, tmp_path):
        assert build_text_document(title="", content="x", output_dir=tmp_path)["ok"] is False
        assert build_text_document(title="x", content="  ", output_dir=tmp_path)["ok"] is False

    def test_overlong_content_rejected(self, tmp_path):
        result = build_text_document(
            title="x", content="字" * 12001, output_dir=tmp_path
        )
        assert result["ok"] is False
        assert "精简" in result["error"]

    def test_illegal_filename_characters_sanitized(self, tmp_path):
        result = build_text_document(
            title='a/b:c*d?"e<>|', content="正文", output_dir=tmp_path
        )
        for ch in '\\/:*?"<>|':
            assert ch not in Path(result["path"]).name

    def test_cleanup_keeps_recent_files_only(self, tmp_path):
        for i in range(45):
            build_text_document(
                title=f"主题 {i}", content="正文", output_dir=tmp_path
            )
        assert len(list(tmp_path.iterdir())) <= 40


class TestService:
    def test_disabled_returns_none(self):
        assert _original_get_service(_config(documents_enabled=False)) is None

    def test_enabled_returns_singleton(self):
        svc = _original_get_service(_config())
        assert isinstance(svc, DocumentsService)
        assert _original_get_service(_config()) is svc

    def test_queue_and_consume_roundtrip(self):
        service = DocumentsService()
        target = Path("x.docx")
        service.queue_delivery("group:9", target)
        assert service.consume_deliveries("group:9") == [target]
        assert service.consume_deliveries("group:9") == []


class TestExecutor:
    def _engine(self):
        class Holder(EngineToolsMixin):
            pass

        return Holder()

    def test_disabled_returns_notice(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_send_document_tool(
                "private:1", {"title": "t", "content": "c"},
                _config(documents_enabled=False),
            )
        )
        assert "未启用" in result
        assert "不要承诺发文件" in result

    def test_empty_params_return_notice(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_send_document_tool(
                "private:1", {"title": " ", "content": "c"}, _config()
            )
        )
        assert "title 或 content 为空" in result
        assert "不要只口头承诺" in result

    def test_success_queues_delivery_and_returns_payload(self, monkeypatch, tmp_path):
        engine = self._engine()
        service = DocumentsService()
        monkeypatch.setattr(
            atri_qq_bot.documents, "get_documents_service", lambda config: service
        )
        target = tmp_path / "文档_x.docx"
        target.write_bytes(b"PK")
        monkeypatch.setattr(
            atri_qq_bot.documents,
            "build_text_document",
            lambda **kwargs: {
                "ok": True, "format": "docx",
                "path": str(target), "name": target.name,
            },
        )
        result = asyncio.run(
            engine._execute_send_document_tool(
                "private:1", {"title": "概要", "content": "正文"}, _config()
            )
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["file"] == target.name
        assert "不要谎称已发出" in payload["delivery"]
        assert service.consume_deliveries("private:1") == [target]

    def test_build_failure_returns_notice_without_queue(self, monkeypatch):
        engine = self._engine()
        service = DocumentsService()
        monkeypatch.setattr(
            atri_qq_bot.documents, "get_documents_service", lambda config: service
        )

        def broken_build(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            atri_qq_bot.documents, "build_text_document", broken_build
        )
        result = asyncio.run(
            engine._execute_send_document_tool(
                "private:1", {"title": "t", "content": "c"}, _config()
            )
        )
        assert "文档生成失败" in result
        assert "不要谎称已发送" in result
        assert service.consume_deliveries("private:1") == []

    def test_build_error_result_returns_notice_without_queue(self, monkeypatch):
        engine = self._engine()
        service = DocumentsService()
        monkeypatch.setattr(
            atri_qq_bot.documents, "get_documents_service", lambda config: service
        )
        monkeypatch.setattr(
            atri_qq_bot.documents,
            "build_text_document",
            lambda **kwargs: {"ok": False, "error": "正文超过 12000 字"},
        )
        result = asyncio.run(
            engine._execute_send_document_tool(
                "private:1", {"title": "t", "content": "c"}, _config()
            )
        )
        assert "文档生成失败" in result
        assert service.consume_deliveries("private:1") == []


class TestSchema:
    def test_schema_registered_when_enabled(self):
        names = [t["function"]["name"] for t in available_tool_schemas(_config())]
        assert "send_document" in names

    def test_schema_absent_when_disabled(self):
        names = [
            t["function"]["name"]
            for t in available_tool_schemas(_config(documents_enabled=False))
        ]
        assert "send_document" not in names

    def test_description_locks_promise_contract(self):
        tool = next(
            t
            for t in available_tool_schemas(_config())
            if t["function"]["name"] == "send_document"
        )
        description = tool["function"]["description"]
        params = tool["function"]["parameters"]
        # 承诺发文档必须调工具的反谎言契约必须留在描述里。
        assert "必须调用本工具" in description
        assert "绝不能只口头承诺" in description
        assert "不要谎称文件已发出" in description
        # 不与论文自动概要文档重复。
        assert "不要用本工具重复发送" in description
        assert params["required"] == ["title", "content"]


class TestDeliveryMixin:
    def _holder(self):
        from atri_qq_bot.onebot.server_parts.server_documents import (
            OneBotDocumentsMixin,
        )

        class Holder(OneBotDocumentsMixin):
            def __init__(self):
                self.config = _config()
                self.sent = []
                self.model_name = "test-model"

            def _delivery_model_name(self, model_type):
                return self.model_name

            async def send_reply(
                self, websocket, event, messages, model_type=None, model_name=None
            ):
                for message in messages:
                    self.sent.append((message.kind, message.content))
                return "ok"

        return Holder()

    def test_delivers_queued_file(self, monkeypatch, tmp_path):
        holder = self._holder()
        service = DocumentsService()
        target = tmp_path / "文档.docx"
        target.write_bytes(b"PK")
        service.queue_delivery("group:9", target)
        monkeypatch.setattr(
            atri_qq_bot.documents, "get_documents_service", lambda config: service
        )
        asyncio.run(
            holder._deliver_pending_documents(object(), {"group_id": 9}, "group:9")
        )
        assert holder.sent == [("file", str(target))]

    def test_no_queue_is_noop(self, monkeypatch):
        holder = self._holder()
        monkeypatch.setattr(
            atri_qq_bot.documents,
            "get_documents_service",
            lambda config: DocumentsService(),
        )
        asyncio.run(
            holder._deliver_pending_documents(object(), {"group_id": 9}, "group:9")
        )
        assert holder.sent == []

    def test_delivery_failure_sends_fallback_text(self, monkeypatch, tmp_path):
        holder = self._holder()
        service = DocumentsService()
        target = tmp_path / "digest.docx"
        target.write_bytes(b"PK")
        service.queue_delivery("group:9", target)
        monkeypatch.setattr(
            atri_qq_bot.documents, "get_documents_service", lambda config: service
        )
        calls = {"n": 0}

        async def flaky_send(websocket, event, messages, model_type=None, model_name=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return ""

        holder.send_reply = flaky_send
        asyncio.run(
            holder._deliver_pending_documents(object(), {"group_id": 9}, "group:9")
        )
        assert calls["n"] == 2  # 第二次是失败提示文案
