"""Unit tests for research-mcp tools.

All HTTP traffic is mocked with httpx.MockTransport, so these run offline and
require no API key. We monkeypatch the module-level client getters to return
clients backed by a mock transport.
"""
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


# ── search_scopus ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_scopus_parses_entries(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "content/search/scopus" in str(request.url)
        return httpx.Response(200, json={
            "search-results": {"entry": [{
                "dc:identifier": "SCOPUS_ID:12345",
                "dc:title": "Agile teams",
                "prism:doi": "10.1/x",
                "citedby-count": "7",
                "link": [{"@ref": "scopus", "@href": "https://scopus.com/x"}],
            }]}
        }, headers={"X-RateLimit-Remaining": "19999", "X-RateLimit-Limit": "20000"})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    out = await server.search_scopus("TITLE(agile)")
    assert out[0]["scopus_id"] == "12345"
    assert out[0]["title"] == "Agile teams"
    assert out[0]["url"] == "https://scopus.com/x"
    # quota headers should have been captured
    assert server._quota_info["remaining"] == "19999"


@pytest.mark.asyncio
async def test_search_scopus_clamps_count(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["count"] = request.url.params.get("count")
        return httpx.Response(200, json={"search-results": {"entry": []}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    await server.search_scopus("q", count=999)
    assert captured["count"] == str(server.MAX_COUNT)


# ── get_author_profile (OpenAlex) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_author_profile_openalex(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "openalex.org/authors" in str(request.url)
        return httpx.Response(200, json={"results": [{
            "id": "https://openalex.org/A123",
            "display_name": "Amy Edmondson",
            "cited_by_count": 45000,
            "works_count": 358,
            "summary_stats": {"h_index": 66, "i10_index": 130},
            "last_known_institutions": [{"display_name": "Harvard"}],
            "ids": {"orcid": "https://orcid.org/0000-0003-4409-913X"},
        }]})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_author_profile("Amy Edmondson")
    top = out["matches"][0]
    assert top["h_index"] == 66
    assert top["orcid"] == "0000-0003-4409-913X"
    assert top["affiliations"] == ["Harvard"]


@pytest.mark.asyncio
async def test_get_author_profile_no_results(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_author_profile("Nobody Xyz")
    assert "error" in out


# ── get_pdf_link (Unpaywall) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pdf_link_found(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "oa_status": "gold", "journal_is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://x/paper.pdf", "host_type": "publisher"},
        })

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_pdf_link("10.1/x")
    assert out["oa_pdf_url"] == "https://x/paper.pdf"
    assert out["source"] == "publisher"


@pytest.mark.asyncio
async def test_get_pdf_link_invalid_email(monkeypatch):
    def handler(request):
        return httpx.Response(422, json={"error": True})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_pdf_link("10.1/x")
    assert "UNPAYWALL_EMAIL" in out["error"]


# ── retry/backoff ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_on_429(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"ok": True})

    # no real sleeping
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)

    r = await server._get_with_retry(_client(handler), "https://example.com/x")
    assert r.status_code == 200
    assert calls["n"] == 2


# ── get_quota_status ──────────────────────────────────────────────────────────

def test_quota_status_empty():
    assert "No Scopus request" in server.get_quota_status()["note"]


def test_quota_status_populated():
    server._quota_info.update({"limit": "20000", "remaining": "19998", "reset_epoch": "123"})
    out = server.get_quota_status()
    assert out["remaining"] == "19998"
    assert out["scopus_weekly_limit"] == "20000"


# ── count clamping helper ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [(0, 1), (5, 5), (25, 25), (999, 25), ("8", 8), (None, 5), ("x", 5)])
def test_clamp_count(value, expected):
    assert server._clamp_count(value) == expected
