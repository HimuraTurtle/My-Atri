"""学术论文检索（OpenAlex）服务测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import atri_qq_bot.papers
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.papers import PapersService
from atri_qq_bot.papers import get_papers_service as _original_get_service
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

    def test_schema_absent_when_disabled(self):
        schemas = available_tool_schemas(_config(papers_enabled=False))
        names = [t["function"]["name"] for t in schemas]
        assert "search_papers" not in names

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
            engine._execute_paper_search_tool({"query": "graph"}, _config(papers_enabled=False))
        )
        assert "未启用" in result

    def test_empty_query_returns_notice(self):
        engine = self._engine()
        result = asyncio.run(engine._execute_paper_search_tool({"query": " "}, _config()))
        assert "query 为空" in result

    def test_success_returns_json_payload(self, monkeypatch):
        engine = self._engine()

        class StubService:
            async def search(self, query, limit=5, sort="relevance"):
                assert query == "graph"
                assert limit == 3
                return {"ok": True, "count": 1, "papers": [{"title": "Paper"}]}

        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: StubService()
        )
        result = asyncio.run(
            engine._execute_paper_search_tool(
                {"query": "graph", "limit": 3, "sort": "cited"}, _config()
            )
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert payload["papers"][0]["title"] == "Paper"

    def test_service_error_bubbles_as_notice(self, monkeypatch):
        engine = self._engine()

        class BrokenService:
            async def search(self, query, limit=5, sort="relevance"):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            atri_qq_bot.papers, "get_papers_service", lambda config: BrokenService()
        )
        result = asyncio.run(
            engine._execute_paper_search_tool({"query": "graph"}, _config())
        )
        assert "论文检索执行失败" in result
        assert "不要编造" in result
