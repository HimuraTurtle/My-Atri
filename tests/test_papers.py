"""学术论文检索（OpenAlex）服务与整理文档测试。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import atri_qq_bot.papers
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.papers import PapersService, build_paper_digest
from atri_qq_bot.papers import get_papers_service as _original_get_service
from atri_qq_bot.papers.docx_writer import MiniDocx
from atri_qq_bot.papers.service import _author_names, _rebuild_abstract
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(**overrides):
    base = {
        "papers_enabled": True,
        "papers_timeout_seconds": 30.0,
        "llm_tools_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _work(title="A survey of graph neural networks", year=2021, cited=42):
    return {
        "display_name": title,
        "publication_year": year,
        "cited_by_count": cited,
        "type": "journal-article",
        "doi": "https://doi.org/10.1000/example",
        "id": "https://openalex.org/W123",
        "authorships": [
            {"author": {"display_name": "Alice"}},
            {"author": {"display_name": "Bob"}},
        ],
        "primary_location": {"source": {"display_name": "Nature Machine Intelligence"}},
        "abstract_inverted_index": {"Hello": [0], "world": [1], "graph": [2]},
    }


class TestHelpers:
    def test_rebuild_abstract_orders_by_position(self):
        inverted = {"world": [1], "!": [3], "Hello": [0], "big": [2]}
        assert _rebuild_abstract(inverted) == "Hello world big !"

    def test_rebuild_abstract_ignores_bad_input(self):
        assert _rebuild_abstract(None) == ""
        assert _rebuild_abstract("not-a-dict") == ""
        assert _rebuild_abstract({"x": ["bad", 5]}) == "x"

    def test_author_names_short_list(self):
        assert _author_names([{"author": {"display_name": "Alice"}}]) == "Alice"

    def test_author_names_et_al(self):
        items = [{"author": {"display_name": f"Author {i}"}} for i in range(5)]
        names = _author_names(items)
        assert names.startswith("Author 0, Author 1, Author 2")
        assert names.endswith("et al.")


class TestService:
    def test_disabled_returns_none(self):
        assert _original_get_service(_config(papers_enabled=False)) is None

    def test_enabled_returns_singleton(self):
        svc = _original_get_service(_config())
        assert isinstance(svc, PapersService)
        assert _original_get_service(_config()) is svc

    def test_empty_query_returns_error(self):
        svc = PapersService(_config())
        result = asyncio.run(svc.search("   "))
        assert result["ok"] is False
        assert "检索词" in result["error"]

    def test_search_parses_results(self):
        svc = PapersService(_config())

        async def fake_fetch(params, timeout):
            assert params["search"] == "graph neural network"
            assert params["per-page"] == "50"
            assert "sort" not in params
            return {"results": [_work(), _work(title="Preprint", year=2025, cited=0)]}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph neural network"))
        assert result["ok"] is True
        assert result["count"] == 2
        paper = result["papers"][0]
        assert paper["title"] == "A survey of graph neural networks"
        assert paper["authors"] == "Alice, Bob"
        assert paper["year"] == 2021
        assert paper["venue"] == "Nature Machine Intelligence"
        assert paper["cited_by_count"] == 42
        assert paper["doi"] == "https://doi.org/10.1000/example"
        assert paper["url"] == "https://doi.org/10.1000/example"
        assert paper["abstract"] == "Hello world graph"

    def test_search_url_falls_back_to_openalex_page_without_doi(self):
        svc = PapersService(_config())
        work = {**_work(), "doi": ""}

        async def fake_fetch(params, timeout):
            return {"results": [work]}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph"))
        assert result["papers"][0]["url"] == "https://openalex.org/W123"

    def test_search_cited_sort_ranks_within_relevance_pool(self):
        svc = PapersService(_config())
        pool = [
            _work(title="Low cited relevant", year=2023, cited=3),
            _work(title="High cited relevant", year=2019, cited=500),
            _work(title="Mid cited relevant", year=2021, cited=99),
        ]

        async def fake_fetch(params, timeout):
            return {"results": pool}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph", limit=3, sort="cited"))
        titles = [p["title"] for p in result["papers"]]
        assert titles == [
            "High cited relevant",
            "Mid cited relevant",
            "Low cited relevant",
        ]

    def test_search_newest_sort_ranks_within_relevance_pool(self):
        svc = PapersService(_config())
        pool = [
            {
                **_work(title="Older"),
                "publication_date": "2018-05-01",
            },
            {
                **_work(title="Newest"),
                "publication_date": "2025-12-30",
            },
        ]

        async def fake_fetch(params, timeout):
            return {"results": pool}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph", limit=2, sort="newest"))
        titles = [p["title"] for p in result["papers"]]
        assert titles == ["Newest", "Older"]

    def test_search_unknown_sort_falls_back_to_relevance(self):
        svc = PapersService(_config())
        pool = [_work(title="First"), _work(title="Second")]

        async def fake_fetch(params, timeout):
            return {"results": pool}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph", sort="bogus"))
        assert result["sort"] == "relevance"
        assert [p["title"] for p in result["papers"]] == ["First", "Second"]

    def test_search_limit_clamped_to_eight(self):
        svc = PapersService(_config())
        pool = [_work(title=f"Paper {i}") for i in range(20)]

        async def fake_fetch(params, timeout):
            assert params["per-page"] == "50"
            return {"results": pool}

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("deep learning", limit=99))
        assert result["count"] == 8

    def test_search_fetch_failure_returns_error_without_raising(self):
        svc = PapersService(_config())

        async def fake_fetch(params, timeout):
            raise RuntimeError("connection refused")

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("graph"))
        assert result["ok"] is False
        assert "OpenAlex" in result["error"]

    def test_search_skips_titleless_entries(self):
        svc = PapersService(_config())

        async def fake_fetch(params, timeout):
            return {
                "results": [
                    _work(title=""),
                    _work(title="Valid"),
                    "not-a-work",
                ]
            }

        svc._fetch = fake_fetch
        result = asyncio.run(svc.search("anything"))
        assert result["count"] == 1
        assert result["papers"][0]["title"] == "Valid"


class TestSchema:
    def test_schema_registered_when_enabled(self):
        schemas = available_tool_schemas(_config())
        names = [t["function"]["name"] for t in schemas]
        assert "search_papers" in names
        assert "compile_paper_digest" in names

    def test_schema_absent_when_disabled(self):
        schemas = available_tool_schemas(_config(papers_enabled=False))
        names = [t["function"]["name"] for t in schemas]
        assert "search_papers" not in names
        assert "compile_paper_digest" not in names

    def test_schema_contract(self):
        schemas = available_tool_schemas(_config())
        tool = next(t for t in schemas if t["function"]["name"] == "search_papers")
        params = tool["function"]["parameters"]
        props = params["properties"]
        assert params["required"] == ["query"]
        assert props["sort"]["enum"] == ["relevance", "newest", "cited"]
        assert props["limit"]["maximum"] == 8


class TestExecutor:
    def _engine(self):
        class Holder(EngineToolsMixin):
            pass

        return Holder()

    def test_disabled_returns_notice(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_paper_search_tool(
                "private:1", {"query": "graph"}, _config(papers_enabled=False)
            )
        )
        assert "未启用" in result

    def test_empty_query_returns_notice(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_paper_search_tool("private:1", {"query": " "}, _config())
        )
        assert "query 为空" in result

    def test_success_returns_json_payload(self, monkeypatch):
        engine = self._engine()

        class StubService:
            def __init__(self):
                self.remembered = []

            async def search(self, query, limit=5, sort="relevance"):
                assert query == "graph"
                assert limit == 3
                return {"ok": True, "count": 1, "papers": [{"title": "Paper"}]}

            def remember_search(self, conversation_id, payload):
                self.remembered.append((conversation_id, payload))

        stub = StubService()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: stub
        )
        result = asyncio.run(
            engine._execute_paper_search_tool(
                "private:1", {"query": "graph", "limit": 3, "sort": "cited"}, _config()
            )
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["papers"][0]["title"] == "Paper"
        assert stub.remembered == [("private:1", payload)]

    def test_service_error_bubbles_as_notice(self, monkeypatch):
        engine = self._engine()

        class BrokenService:
            async def search(self, query, limit=5, sort="relevance"):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: BrokenService()
        )
        result = asyncio.run(
            engine._execute_paper_search_tool("private:1", {"query": "graph"}, _config())
        )
        assert "论文检索执行失败" in result
        assert "不要编造" in result


class TestDigestExecutor:
    def _engine(self):
        class Holder(EngineToolsMixin):
            pass

        return Holder()

    def _search_payload(self, count=3):
        return {
            "ok": True,
            "query": "graph neural network",
            "sort": "relevance",
            "count": count,
            "papers": [
                {
                    "title": f"Paper {i}",
                    "authors": "Alice",
                    "year": 2020 + i,
                    "venue": "Nature",
                    "cited_by_count": 10 * i,
                    "url": f"https://doi.org/10.1000/p{i}",
                    "abstract": f"Abstract {i}",
                }
                for i in range(1, count + 1)
            ],
        }

    def _service(self):
        service = PapersService(_config())
        service.remember_search("private:1", self._search_payload())
        return service

    def test_no_cached_search_returns_notice(self, monkeypatch):
        engine = self._engine()
        monkeypatch.setattr(
            atri_qq_bot.papers,
            "get_papers_service",
            lambda config: PapersService(_config()),
        )
        result = asyncio.run(
            engine._execute_paper_digest_tool("private:2", {}, _config())
        )
        assert "还没有可整理的论文检索结果" in result

    def test_digest_queues_delivery_and_returns_payload(self, monkeypatch, tmp_path):
        engine = self._engine()
        service = self._service()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )

        def fake_build(*, query, sort, papers, summary, output_dir):
            target = tmp_path / "论文整理.docx"
            target.write_bytes(b"PK")
            return {"ok": True, "format": "docx", "path": str(target), "name": target.name, "count": len(papers)}

        monkeypatch.setattr(atri_qq_bot.papers, "build_paper_digest", fake_build)
        result = asyncio.run(
            engine._execute_paper_digest_tool(
                "private:1", {"summary": "脉络综述"}, _config()
            )
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["format"] == "docx"
        assert payload["count"] == 3
        deliveries = service.consume_digest_deliveries("private:1")
        assert [p.name for p in deliveries] == ["论文整理.docx"]
        assert service.consume_digest_deliveries("private:1") == []

    def test_indices_select_subset_in_order(self, monkeypatch, tmp_path):
        engine = self._engine()
        service = self._service()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )
        captured = {}

        def fake_build(*, query, sort, papers, summary, output_dir):
            captured["papers"] = papers
            target = tmp_path / "digest.docx"
            target.write_bytes(b"PK")
            return {"ok": True, "format": "docx", "path": str(target), "name": target.name, "count": len(papers)}

        monkeypatch.setattr(atri_qq_bot.papers, "build_paper_digest", fake_build)
        asyncio.run(
            engine._execute_paper_digest_tool(
                "private:1", {"indices": [3, 1]}, _config()
            )
        )
        assert [p["title"] for p in captured["papers"]] == ["Paper 1", "Paper 3"]

    def test_indices_out_of_range_rejected(self, monkeypatch):
        engine = self._engine()
        service = self._service()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )
        result = asyncio.run(
            engine._execute_paper_digest_tool("private:1", {"indices": [4]}, _config())
        )
        assert "超出范围" in result
        assert service.consume_digest_deliveries("private:1") == []

    def test_indices_invalid_type_rejected(self, monkeypatch):
        engine = self._engine()
        service = self._service()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )
        result = asyncio.run(
            engine._execute_paper_digest_tool("private:1", {"indices": "1"}, _config())
        )
        assert "indices 参数无效" in result

    def test_build_failure_returns_notice_without_queue(self, monkeypatch):
        engine = self._engine()
        service = self._service()
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )

        def fake_build(*, query, sort, papers, summary, output_dir):
            return {"ok": False, "error": "没有可整理的论文"}

        monkeypatch.setattr(atri_qq_bot.papers, "build_paper_digest", fake_build)
        result = asyncio.run(
            engine._execute_paper_digest_tool("private:1", {}, _config())
        )
        assert "论文整理文档生成失败" in result
        assert "不要谎称已发送" in result
        assert service.consume_digest_deliveries("private:1") == []


class TestServiceSearchMemory:
    def test_remember_and_last_search_roundtrip(self):
        service = PapersService(_config())
        payload = {"ok": True, "query": "q", "papers": [{"title": "T"}]}
        service.remember_search("group:9", payload)
        assert service.last_search("group:9")["papers"][0]["title"] == "T"

    def test_failed_search_not_remembered(self):
        service = PapersService(_config())
        service.remember_search("group:9", {"ok": False, "error": "x", "papers": []})
        assert service.last_search("group:9") is None

    def test_last_search_expires_after_ttl(self, monkeypatch):
        from atri_qq_bot.papers import service as service_module

        service = PapersService(_config())
        clock = {"now": 1000.0}
        monkeypatch.setattr(service_module.time, "monotonic", lambda: clock["now"])
        service.remember_search("group:9", {"ok": True, "papers": [{"title": "T"}]})
        clock["now"] += service_module._LAST_SEARCH_TTL_SECONDS + 1
        assert service.last_search("group:9") is None

    def test_remember_cap_drops_oldest(self):
        from atri_qq_bot.papers import service as service_module

        service = PapersService(_config())
        for i in range(service_module._MAX_REMEMBERED_CONVERSATIONS + 5):
            service.remember_search(f"conv:{i}", {"ok": True, "papers": [{"title": "T"}]})
        assert service.last_search("conv:0") is None
        assert service.last_search("conv:4") is None
        assert service.last_search("conv:68") is not None


class TestMiniDocx:
    def test_save_produces_valid_package(self, tmp_path):
        doc = MiniDocx()
        doc.title("论文检索整理：图神经网络")
        doc.heading("综述")
        doc.paragraph("这是综述段落。")
        doc.bullet("1. Paper A（2021，Nature，被引 42）")
        doc.link("原文链接", "https://doi.org/10.1000/example")
        path = tmp_path / "out.docx"
        doc.save(path)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "word/document.xml" in names
            assert "word/_rels/document.xml.rels" in names
            document = archive.read("word/document.xml").decode("utf-8")
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "论文检索整理：图神经网络" in document
        assert 'r:id="rId100"' in document
        assert 'Target="https://doi.org/10.1000/example"' in rels
        assert 'TargetMode="External"' in rels

    def test_escapes_xml_special_characters(self, tmp_path):
        doc = MiniDocx()
        doc.paragraph("A < B & C > \"quoted\"")
        path = tmp_path / "out.docx"
        doc.save(path)
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        # 文本节点里引号无需转义（属性节点才需要），其余特殊字符必须转义。
        assert "A &lt; B &amp; C &gt; \"quoted\"" in document


class TestBuildDigest:
    def _papers(self, count=2):
        return [
            {
                "title": f"Paper {i}",
                "authors": "Alice, Bob",
                "year": 2021,
                "venue": "Nature",
                "cited_by_count": 7,
                "url": f"https://doi.org/10.1000/p{i}",
                "abstract": f"Abstract number {i}",
            }
            for i in range(1, count + 1)
        ]

    def test_build_docx_success(self, tmp_path):
        result = build_paper_digest(
            query="graph neural network",
            sort="cited",
            papers=self._papers(),
            summary="第一段综述\n第二段综述",
            output_dir=tmp_path,
        )
        assert result["ok"] is True
        assert result["format"] == "docx"
        assert result["count"] == 2
        path = Path(result["path"])
        assert path.exists()
        assert path.suffix == ".docx"
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        assert "论文检索整理：graph neural network" in document
        assert "被引量" in document  # sort 标签
        assert "第一段综述" in document and "第二段综述" in document
        assert "Paper 1" in document and "Abstract number 2" in document

    def test_docx_failure_falls_back_to_markdown(self, tmp_path, monkeypatch):
        def broken_build(*args, **kwargs):
            raise RuntimeError("docx broken")

        monkeypatch.setattr(
            "atri_qq_bot.papers.digest._build_docx", broken_build
        )
        result = build_paper_digest(
            query="query",
            sort="relevance",
            papers=self._papers(1),
            summary="",
            output_dir=tmp_path,
        )
        assert result["ok"] is True
        assert result["format"] == "md"
        path = Path(result["path"])
        assert path.exists() and path.suffix == ".md"
        text = path.read_text(encoding="utf-8")
        assert "# 论文检索整理：query" in text
        assert "Paper 1" in text

    def test_empty_papers_returns_error(self, tmp_path):
        result = build_paper_digest(
            query="q", sort="relevance", papers=[], output_dir=tmp_path
        )
        assert result["ok"] is False

    def test_illegal_filename_characters_sanitized(self, tmp_path):
        result = build_paper_digest(
            query='a/b:c*d?"e<>|',
            sort="relevance",
            papers=self._papers(1),
            output_dir=tmp_path,
        )
        name = Path(result["path"]).name
        for ch in '\\/:*?"<>|':
            assert ch not in name

    def test_cleanup_keeps_recent_files_only(self, tmp_path):
        for i in range(45):
            build_paper_digest(
                query=f"topic {i}",
                sort="relevance",
                papers=self._papers(1),
                output_dir=tmp_path,
            )
        remaining = list(tmp_path.iterdir())
        assert len(remaining) <= 40


class TestDigestDeliveryMixin:
    def _holder(self):
        from atri_qq_bot.onebot.server_parts.server_papers import OneBotPapersMixin

        class Holder(OneBotPapersMixin):
            def __init__(self):
                self.config = _config()
                self.sent = []
                self.model_name = "test-model"

            def _delivery_model_name(self, model_type):
                return self.model_name

            async def send_reply(self, websocket, event, messages, model_type=None, model_name=None):
                for message in messages:
                    self.sent.append((message.kind, message.content))

        return Holder()

    def test_delivers_queued_file(self, monkeypatch, tmp_path):
        holder = self._holder()
        service = PapersService(_config())
        target = tmp_path / "论文整理.docx"
        target.write_bytes(b"PK")
        service.queue_digest_delivery("group:9", target)
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )
        asyncio.run(
            holder._deliver_pending_paper_digest(
                object(), {"group_id": 9}, "group:9"
            )
        )
        assert holder.sent == [("file", str(target))]

    def test_no_queue_is_noop(self, monkeypatch):
        holder = self._holder()
        monkeypatch.setattr(
            atri_qq_bot.papers,
            "get_papers_service",
            lambda config: PapersService(_config()),
        )
        asyncio.run(
            holder._deliver_pending_paper_digest(object(), {"group_id": 9}, "group:9")
        )
        assert holder.sent == []

    def test_delivery_failure_sends_fallback_text(self, monkeypatch, tmp_path):
        holder = self._holder()
        service = PapersService(_config())
        target = tmp_path / "digest.docx"
        target.write_bytes(b"PK")
        service.queue_digest_delivery("group:9", target)
        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: service
        )
        calls = {"n": 0}

        async def flaky_send(websocket, event, messages, model_type=None, model_name=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("upload failed")

        holder.send_reply = flaky_send
        asyncio.run(
            holder._deliver_pending_paper_digest(
                object(), {"group_id": 9}, "group:9"
            )
        )
        assert calls["n"] == 2  # 第二次是失败提示文案
