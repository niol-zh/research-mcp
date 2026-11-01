"""Abstracts in the export (with_abstracts) and de-duplication while paging.

Scopus only returns abstracts (dc:description) in the COMPLETE view, which an
entitled key may use; otherwise the export fills them from OpenAlex by DOI.
"""
import csv
import json

import httpx
import pytest

from research_mcp import server


@pytest.fixture(autouse=True)
def _export_env(tmp_path, monkeypatch):
    monkeypatch.setenv(server.EXPORT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(server, "_config_cache", {})
    monkeypatch.setattr(server, "SCOPUS_EXPORT_PAUSE", 0.0)
    server._quota_info.clear()
    yield tmp_path
    server._quota_info.clear()


def _entry(i, abstract=None, doi=True):
    e = {"dc:identifier": f"SCOPUS_ID:{i}", "dc:title": f"Paper {i}", "prism:coverDate": "2024-01-01"}
    if doi:
        e["prism:doi"] = f"10.1/P{i}"   # upper case on purpose: matching must be case-insensitive
    if abstract:
        e["dc:description"] = abstract
    return e


def _scopus(entitled: bool, total: int, views: list, with_abstract_for=()):
    def handler(request: httpx.Request) -> httpx.Response:
        view = request.url.params.get("view")
        views.append(view)
        if view == "COMPLETE" and not entitled:
            return httpx.Response(401, json={"service-error": {"status": {"statusCode": "AUTHORIZATION_ERROR"}}})
        start = int(request.url.params.get("start", 0)); count = int(request.url.params.get("count", 25))
        entries = [
            _entry(i, abstract=(f"Scopus abstract {i}" if view == "COMPLETE" and i in with_abstract_for else None))
            for i in range(start, min(start + count, total))
        ]
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": str(total), "entry": entries}})
    return handler


def _openalex(known: dict):
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openalex.org"
        dois = request.url.params.get("filter").split("doi:")[1].split("|")
        calls.append(len(dois))
        results = [{"doi": f"https://doi.org/{d}", "abstract_inverted_index": {w: [i] for i, w in enumerate(known[d].split())}}
                   for d in dois if d in known]
        return httpx.Response(200, json={"results": results})
    return handler, calls


@pytest.mark.asyncio
async def test_entitled_key_uses_scopus_abstracts(monkeypatch, _export_env):
    views = []
    monkeypatch.setattr(server, "scopus_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_scopus(True, 3, views, {0, 1, 2}))))
    monkeypatch.setattr(server, "http_client", lambda: (_ for _ in ()).throw(AssertionError("OpenAlex must not be called")))
    out = await server.export_scopus_results("q", "a", with_abstracts=True)
    assert views == ["COMPLETE"]
    assert out["abstracts"] == {"requested": True, "scopus_view": "COMPLETE", "from_scopus": 3,
                                "from_openalex": 0, "missing": 0, "openalex_requests": 0}
    with open(out["path"], newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert list(rows[0]) == server._EXPORT_FIELDS
    assert rows[1]["abstract"] == "Scopus abstract 1" and rows[1]["abstract_source"] == "scopus"


@pytest.mark.asyncio
async def test_unentitled_key_falls_back_to_openalex(monkeypatch, _export_env):
    views = []
    monkeypatch.setattr(server, "scopus_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_scopus(False, 60, views))))
    known = {f"10.1/p{i}": f"OpenAlex abstract {i}" for i in range(0, 60, 2)}   # every second paper
    oa_handler, oa_calls = _openalex(known)
    monkeypatch.setattr(server, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(oa_handler)))
    out = await server.export_scopus_results("q", "b", with_abstracts=True, format="json")
    # one failed COMPLETE page, then three STANDARD pages
    assert views == ["COMPLETE", "STANDARD", "STANDARD", "STANDARD"]
    assert oa_calls == [50, 10]
    assert out["abstracts"]["scopus_view"] == "STANDARD"
    assert out["abstracts"]["from_openalex"] == 30 and out["abstracts"]["missing"] == 30
    assert out["abstracts"]["openalex_requests"] == 2
    data = json.loads(open(out["path"], encoding="utf-8").read())
    assert data["results"][0]["abstract_source"] == "openalex"
    assert data["results"][1]["abstract"] is None and data["results"][1]["abstract_source"] is None


@pytest.mark.asyncio
async def test_without_abstracts_keeps_standard_view_and_empty_columns(monkeypatch, _export_env):
    views = []
    monkeypatch.setattr(server, "scopus_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_scopus(True, 2, views))))
    out = await server.export_scopus_results("q", "c")
    assert views == ["STANDARD"]
    assert out["abstracts"] == {"requested": False}
    with open(out["path"], newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert rows[0]["abstract"] == "" and rows[0]["abstract_source"] == ""


@pytest.mark.asyncio
async def test_export_drops_duplicates_across_pages(monkeypatch, _export_env):
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        # relevancy paging repeats document 24 at the top of page 2
        ids = list(range(start, start + 25)) if start == 0 else [24] + list(range(25, 49))
        entries = [_entry(i) for i in ids]
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "50", "entry": entries}})
    monkeypatch.setattr(server, "scopus_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await server.export_scopus_results("q", "d", sort="relevancy")
    assert out["exported"] == 49 and out["duplicates_removed"] == 1


@pytest.mark.asyncio
async def test_search_reports_duplicates(monkeypatch):
    server._quota_info.clear()
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        ids = list(range(start, start + 25)) if start == 0 else [24] + list(range(25, 49))
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "50", "entry": [_entry(i) for i in ids]}})
    monkeypatch.setattr(server, "scopus_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await server.search_scopus("q", count=50)
    assert out["returned"] == 49 and out["duplicates_removed"] == 1
    assert out["next_start"] is None
