"""Hit counts, paging and export for search_scopus; paging for the OpenAlex
citation tools. Mirrors the acceptance criteria of the v0.7.0 requirements
(total count, zero hits, count_only, start, export to file).

All HTTP traffic is mocked with httpx.MockTransport.
"""
import csv
import json
from datetime import datetime

import httpx
import pytest

from research_mcp import server


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_quota():
    server._quota_info.clear()
    yield
    server._quota_info.clear()


def _entry(i: int) -> dict:
    return {
        "dc:identifier": f"SCOPUS_ID:{1000 + i}",
        "dc:title": f"Paper {i}",
        "dc:creator": "Müller A.",
        "prism:publicationName": "Journal of Things",
        "prism:coverDate": "2024-05-01",
        "prism:doi": f"10.1/p{i}",
        "citedby-count": str(i),
        "link": [{"@ref": "scopus", "@href": f"https://scopus.com/{i}"}],
    }


def _scopus_mock(total: int, calls: list):
    """A Scopus Search stub that honours `start` and `count` against `total` hits."""
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        count = int(request.url.params.get("count", 25))
        calls.append((start, count))
        if total == 0:
            entries = [{"@_fa": "true", "error": "Result set was empty"}]
        else:
            entries = [_entry(i) for i in range(start, min(start + count, total))]
        return httpx.Response(200, json={"search-results": {
            "opensearch:totalResults": str(total),
            "entry": entries,
        }}, headers={"X-RateLimit-Remaining": "19876", "X-RateLimit-Limit": "20000"})
    return handler


# ── search_scopus: total count and search-log fields (R1, R5) ─────────────────

@pytest.mark.asyncio
async def test_search_scopus_reports_total_and_log_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(1234, calls)))
    out = await server.search_scopus('TITLE-ABS-KEY("digital transformation")', count=25)
    assert out["query"] == 'TITLE-ABS-KEY("digital transformation")'
    assert out["total_results"] == 1234
    assert out["returned"] == 25
    assert out["start"] == 0
    assert out["next_start"] == 25
    assert out["quota_remaining"] == "19876"
    assert out["mcp_version"]
    # retrieved_at is ISO 8601 with a UTC offset
    assert datetime.fromisoformat(out["retrieved_at"]).utcoffset() is not None
    assert calls == [(0, 25)]
    # count <= 25 keeps the full record
    assert "aggregation_type" in out["results"][0] and "url" in out["results"][0]


@pytest.mark.asyncio
async def test_search_scopus_small_result_set(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(12, calls)))
    out = await server.search_scopus("IT governance", count=25)
    assert out["total_results"] == 12
    assert out["returned"] == 12
    assert out["next_start"] is None


# ── zero hits (R2) ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_scopus_zero_hits_is_empty_list(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(0, calls)))
    out = await server.search_scopus("task demands")
    assert out["total_results"] == 0
    assert out["results"] == []
    assert out["returned"] == 0
    assert out["next_start"] is None


# ── count_only (R3) ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_scopus_count_only_costs_one_request(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(1234, calls)))
    out = await server.search_scopus("q", count_only=True)
    assert out["total_results"] == 1234
    assert "results" not in out
    assert out["returned"] == 0
    assert calls == [(0, 1)]


# ── start / paging (R4) ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_scopus_start_fetches_next_page(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(60, calls)))
    out = await server.search_scopus("q", count=25, start=25)
    assert calls == [(25, 25)]
    assert [r["scopus_id"] for r in out["results"]][:2] == ["1025", "1026"]
    assert out["start"] == 25
    assert out["next_start"] == 50


@pytest.mark.asyncio
async def test_search_scopus_auto_pages_and_compacts(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(1234, calls)))
    out = await server.search_scopus("q", count=60)
    assert calls == [(0, 25), (25, 25), (50, 10)]
    assert out["returned"] == 60
    assert out["next_start"] == 60
    hit = out["results"][0]
    assert set(hit) == set(server._SCOPUS_COMPACT_FIELDS)
    assert hit["year"] == "2024"
    assert "Compact" in out["note"]


@pytest.mark.asyncio
async def test_search_scopus_auto_paging_stops_at_short_page(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(32, calls)))
    out = await server.search_scopus("q", count=60)
    assert calls == [(0, 25), (25, 25)]
    assert out["returned"] == 32
    assert out["next_start"] is None


@pytest.mark.asyncio
async def test_search_scopus_compact_can_be_forced_either_way(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(100, calls)))
    full = await server.search_scopus("q", count=30, compact=False)
    assert "aggregation_type" in full["results"][0]
    small = await server.search_scopus("q", count=5, compact=True)
    assert set(small["results"][0]) == set(server._SCOPUS_COMPACT_FIELDS)


@pytest.mark.asyncio
async def test_search_scopus_count_is_capped(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(5000, calls)))
    out = await server.search_scopus("q", count=999)
    assert out["returned"] == server.SCOPUS_MAX_RESULTS
    assert len(calls) == server.SCOPUS_MAX_RESULTS // server.SCOPUS_PAGE_SIZE


# ── export_scopus_results ─────────────────────────────────────────────────────

@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(server.EXPORT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(server, "SCOPUS_EXPORT_PAUSE", 0.0)
    return tmp_path


@pytest.mark.asyncio
async def test_export_writes_csv_and_meta(monkeypatch, export_dir):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(70, calls)))
    out = await server.export_scopus_results("q", "it-governance")
    assert calls == [(0, 25), (25, 25), (50, 25)]
    assert out["total_results"] == 70 and out["exported"] == 70
    assert out["truncated"] is False
    assert out["scopus_calls"] == 3
    assert out["quota_remaining"] == "19876"
    assert out["path"].endswith("it-governance.csv")
    with open(out["path"], newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert len(rows) == 70
    assert list(rows[0]) == server._SCOPUS_FIELDS
    assert rows[0]["scopus_id"] == "1000" and rows[0]["creator"] == "Müller A."
    meta = json.loads((export_dir / "it-governance.meta.json").read_text(encoding="utf-8"))
    assert meta["query"] == "q" and meta["total_results"] == 70 and meta["exported"] == 70
    assert meta["retrieved_at"] and meta["mcp_version"]


@pytest.mark.asyncio
async def test_export_json_and_truncation(monkeypatch, export_dir):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(70, calls)))
    out = await server.export_scopus_results("q", "subset.json", max_results=30, format="json")
    assert calls == [(0, 25), (25, 5)]
    assert out["exported"] == 30 and out["truncated"] is True
    assert "limit 30" in out["note"]
    data = json.loads((export_dir / "subset.json").read_text(encoding="utf-8"))
    assert data["total_results"] == 70 and len(data["results"]) == 30


@pytest.mark.asyncio
async def test_export_requires_export_dir(monkeypatch):
    monkeypatch.delenv(server.EXPORT_DIR_ENV, raising=False)
    out = await server.export_scopus_results("q", "x")
    assert server.EXPORT_DIR_ENV in out["error"]


@pytest.mark.asyncio
async def test_export_sanitises_filename_and_refuses_overwrite(monkeypatch, export_dir):
    calls = []
    monkeypatch.setattr(server, "scopus_client", lambda: _client(_scopus_mock(3, calls)))
    out = await server.export_scopus_results("q", "../../etc/pass wd?.csv")
    assert out["path"] == str(export_dir / "pass_wd.csv")
    again = await server.export_scopus_results("q", "pass_wd")
    assert "already exists" in again["error"]
    bad = await server.export_scopus_results("q", "???")
    assert "filename" in bad["error"]
    fmt = await server.export_scopus_results("q", "y", format="xlsx")
    assert "format" in fmt["error"]


# ── get_citing_papers paging (OpenAlex) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_get_citing_papers_pages_openalex(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"meta": {"count": 350}, "results": []})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_citing_papers("W1", count=100, page=3)
    assert captured["per-page"] == "100" and captured["page"] == "3"
    assert out["page"] == 3 and out["per_page"] == 100
    assert out["next_page"] == 4
    last = await server.get_citing_papers("W1", count=100, page=4)
    assert last["next_page"] is None


@pytest.mark.asyncio
async def test_get_citing_papers_openalex_count_capped_at_100(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"meta": {"count": 1}, "results": []})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    await server.get_citing_papers("W1", count=999)
    assert captured["per-page"] == str(server.OPENALEX_MAX_COUNT)


@pytest.mark.asyncio
async def test_get_citing_papers_scopus_passes_start(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": []}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    out = await server.get_citing_papers("12345", source="scopus", start=25)
    assert captured["start"] == "25"
    assert out["total_results"] == 0 and out["results"] == []


# ── get_references windows ────────────────────────────────────────────────────

def _references_mock(n_refs: int, batches: list):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "works/doi:" in url:
            return httpx.Response(200, json={
                "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x",
                "display_name": "Source",
                "referenced_works": [f"https://openalex.org/W{i}" for i in range(n_refs)],
            })
        ids = request.url.params.get("filter").split("openalex_id:")[1].split("|")
        batches.append(len(ids))
        return httpx.Response(200, json={"results": [
            {"id": f"https://openalex.org/{w}", "display_name": w, "cited_by_count": int(w[1:])}
            for w in ids
        ]})
    return handler


@pytest.mark.asyncio
async def test_get_references_window_spans_two_batches(monkeypatch):
    batches = []
    monkeypatch.setattr(server, "http_client", lambda: _client(_references_mock(120, batches)))
    out = await server.get_references("10.1/x", count=100, start=50)
    assert batches == [50, 20]
    assert out["total_references"] == 120
    assert out["start"] == 50 and out["count"] == 70
    assert out["next_start"] is None
    # most-cited first within the window
    assert out["references"][0]["openalex_id"] == "W119"


@pytest.mark.asyncio
async def test_get_references_default_ranks_a_full_batch(monkeypatch):
    batches = []
    monkeypatch.setattr(server, "http_client", lambda: _client(_references_mock(120, batches)))
    out = await server.get_references("10.1/x")
    assert batches == [50]
    assert out["count"] == 10
    assert out["references"][0]["openalex_id"] == "W49"
    assert out["next_start"] == 50
    assert "start=50" in out["note"]


# ── schemas ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_schemas_reflect_limits():
    tools = {t.name: t.inputSchema["properties"] for t in await server.handle_list_tools()}
    assert tools["search_scopus"]["count"]["maximum"] == server.SCOPUS_MAX_RESULTS
    assert {"start", "count_only", "compact"} <= set(tools["search_scopus"])
    assert tools["get_citing_papers"]["count"]["maximum"] == server.OPENALEX_MAX_COUNT
    assert "page" in tools["get_citing_papers"]
    assert tools["get_references"]["count"]["maximum"] == server.OPENALEX_MAX_COUNT
    assert "start" in tools["get_references"]
    # unchanged on purpose
    assert tools["assess_relevance"]["identifiers"]["maxItems"] == 25
    assert tools["export_scopus_results"]["max_results"]["maximum"] == server.SCOPUS_EXPORT_MAX
