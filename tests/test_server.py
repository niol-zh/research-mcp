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


# ── identifier classification ─────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("10.2307/2666999", ("doi", "10.2307/2666999")),
    ("https://doi.org/10.2307/2666999", ("doi", "10.2307/2666999")),
    ("doi:10.2307/2666999", ("doi", "10.2307/2666999")),
    ("W2156435103", ("openalex", "W2156435103")),
    ("w2156435103", ("openalex", "W2156435103")),
    ("https://openalex.org/W2156435103", ("openalex", "W2156435103")),
    ("85012345678", ("scopus", "85012345678")),
    ("SCOPUS_ID:85012345678", ("scopus", "85012345678")),
    ("nonsense", ("unknown", "nonsense")),
])
def test_classify_identifier(value, expected):
    assert server._classify_identifier(value) == expected


# ── abstract inverted index ───────────────────────────────────────────────────

def test_reconstruct_abstract_orders_by_position():
    inverted = {"learning": [2], "Team": [0], "matters": [3], "safety": [1]}
    assert server._reconstruct_abstract(inverted) == "Team safety learning matters"


@pytest.mark.parametrize("value", [None, {}])
def test_reconstruct_abstract_empty(value):
    assert server._reconstruct_abstract(value) is None


# ── get_references (backward citations) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_get_references_hydrates_and_sorts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "works/doi:" in url:
            return httpx.Response(200, json={
                "id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.1/x",
                "display_name": "Source paper",
                "referenced_works": ["https://openalex.org/W10", "https://openalex.org/W11"],
            })
        # batch hydration
        assert "openalex_id:W10|W11" in request.url.params.get("filter")
        return httpx.Response(200, json={"results": [
            {"id": "https://openalex.org/W10", "display_name": "Less cited",
             "publication_year": 2001, "cited_by_count": 5},
            {"id": "https://openalex.org/W11", "display_name": "Most cited",
             "publication_year": 1999, "cited_by_count": 900},
        ]})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_references("10.1/x")
    assert out["direction"] == "backward"
    assert out["total_references"] == 2
    # most-cited reference first
    assert [r["title"] for r in out["references"]] == ["Most cited", "Less cited"]
    assert out["source_paper"]["doi"] == "10.1/x"


@pytest.mark.asyncio
async def test_get_references_falls_back_to_crossref(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openalex" in url:
            return httpx.Response(200, json={
                "id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.1/x",
                "display_name": "Source paper",
                "referenced_works": [],
            })
        assert "api.crossref.org" in url
        return httpx.Response(200, json={"message": {"reference": [
            {"DOI": "10.1/ref", "article-title": "A cited work", "year": "1990"},
        ]}})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_references("10.1/x")
    assert out["references"][0]["doi"] == "10.1/ref"
    assert "CrossRef" in out["note"]


@pytest.mark.asyncio
async def test_get_references_rejects_bad_identifier(monkeypatch):
    monkeypatch.setattr(server, "http_client", lambda: _client(lambda r: httpx.Response(200, json={})))
    out = await server.get_references("not-an-id")
    assert "Unrecognised identifier" in out["error"]


# ── forward citations via OpenAlex ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_citing_papers_openalex(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "cites:" in (request.url.params.get("filter") or ""):
            assert request.url.params.get("filter") == "cites:W2156435103"
            return httpx.Response(200, json={
                "meta": {"count": 10892},
                "results": [{"id": "https://openalex.org/W99", "display_name": "Citing work",
                             "publication_year": 2010, "cited_by_count": 3786}],
            })
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_citing_papers("W2156435103", source="openalex")
    assert out["direction"] == "forward"
    assert out["total_citing"] == 10892
    assert out["citing_papers"][0]["title"] == "Citing work"


@pytest.mark.asyncio
async def test_get_citing_papers_defaults_to_openalex(monkeypatch):
    """Scopus gates REFEID(), so OpenAlex is the default that actually works on a free key."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "openalex.org" in str(request.url)
        return httpx.Response(200, json={"meta": {"count": 72}, "results": []})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.get_citing_papers("W123")
    assert out["direction"] == "forward"
    assert out["total_citing"] == 72


@pytest.mark.asyncio
async def test_get_citing_papers_scopus_explicit(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params.get("query")
        return httpx.Response(200, json={"search-results": {"entry": []}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    await server.get_citing_papers("12345", source="scopus")
    assert captured["query"] == "REFEID(12345)"


@pytest.mark.asyncio
async def test_get_citing_papers_explains_restricted_refeid(monkeypatch):
    """A free Scopus key rejects REFEID(); surface that, not a bare HTTP 400."""
    def handler(request):
        return httpx.Response(400, json={"service-error": {"status": {
            "statusCode": "INVALID_INPUT",
            "statusText": "Use of certain field restrictions in the search query is not allowed.",
        }}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    out = await server.get_citing_papers("12345", source="scopus")
    assert out["scopus_status"] == "INVALID_INPUT"
    assert "institutional subscription" in out["error"]


@pytest.mark.asyncio
async def test_get_citing_papers_scopus_resolves_doi(monkeypatch):
    """A DOI must be resolved to a Scopus ID — REFEID(doi) is invalid syntax (HTTP 400)."""
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query")
        queries.append(q)
        if q.startswith("DOI("):
            return httpx.Response(200, json={"search-results": {"entry": [
                {"dc:identifier": "SCOPUS_ID:98765", "dc:title": "Seed"},
            ]}})
        return httpx.Response(200, json={"search-results": {"entry": []}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    await server.get_citing_papers("10.1016/j.landusepol.2015.09.032", source="scopus")
    assert queries[0] == "DOI(10.1016/j.landusepol.2015.09.032)"
    assert queries[1] == "REFEID(98765)"


@pytest.mark.asyncio
async def test_get_citing_papers_scopus_doi_not_found(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"search-results": {"entry": []}})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    out = await server.get_citing_papers("10.1/unknown", source="scopus")
    assert "openalex" in out["error"]


@pytest.mark.asyncio
async def test_get_citing_papers_scopus_rejects_openalex_id(monkeypatch):
    monkeypatch.setattr(server, "scopus_client", lambda: _client(lambda r: httpx.Response(200, json={})))
    out = await server.get_citing_papers("W2156435103", source="scopus")
    assert "not a Scopus ID or DOI" in out["error"]


# ── assess_relevance ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assess_relevance_builds_evidence(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "doi:10.1/x" in (request.url.params.get("filter") or "")
        return httpx.Response(200, json={"results": [{
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1/x",
            "display_name": "Psychological safety in teams",
            "publication_year": 1999,
            "cited_by_count": 11007,
            "abstract_inverted_index": {"Teams": [0], "learn": [1], "safely": [2]},
            "topics": [{"display_name": "Team Dynamics", "score": 0.98}],
            "keywords": [{"display_name": "Psychological safety", "score": 0.9}],
        }]})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.assess_relevance(["10.1/x"], "psychological safety in teams")
    paper = out["papers"][0]
    assert paper["abstract"] == "Teams learn safely"
    assert paper["topics"][0]["name"] == "Team Dynamics"
    overlap = paper["lexical_overlap"]
    assert "safety" in overlap["matched_terms"]
    assert "teams" in overlap["matched_terms"]
    # no server-side verdict is ever produced
    assert "verdict" not in paper and "score" not in paper
    assert "never as a relevance score" in out["note"]


@pytest.mark.asyncio
async def test_assess_relevance_reports_unresolved(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(server, "http_client", lambda: _client(handler))
    out = await server.assess_relevance(["bogus-id"], "some context")
    assert out["unresolved"][0]["identifier"] == "bogus-id"


@pytest.mark.asyncio
async def test_assess_relevance_validates_input():
    assert "at least one identifier" in (await server.assess_relevance([], "ctx"))["error"]
    assert "research_context" in (await server.assess_relevance(["10.1/x"], "  "))["error"]


# ── ISSN normalisation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["0001-8392", "00018392", " 0001 8392 ", "issn 0001-8392"])
def test_normalise_issn_accepts_common_shapes(raw):
    assert server._normalise_issn(raw) == "0001-8392"


@pytest.mark.parametrize("raw", ["0001-8393", "123", "", None, "0001-839X"])
def test_normalise_issn_rejects_invalid(raw):
    assert server._normalise_issn(raw) is None


def test_normalise_issn_does_not_route_through_classify_identifier():
    """A hyphen-less ISSN looks exactly like a Scopus ID to _classify_identifier.

    This is why ISSNs get their own normaliser — the check digit disambiguates
    what a bare isdigit() test cannot.
    """
    assert server._classify_identifier("09565221") == ("scopus", "09565221")
    assert server._normalise_issn("09565221") == "0956-5221"


def test_normalise_issn_lenient_skips_check_digit():
    assert server._normalise_issn("0001-8393", strict=False) == "0001-8393"


# ── Quartile derivation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,expected", [
    (100, "Q1"), (99, "Q1"), (75, "Q1"),
    (74, "Q2"), (50, "Q2"),
    (49, "Q3"), (25, "Q3"),
    (24, "Q4"), (0, "Q4"),
])
def test_quartile_from_percentile_boundaries(pct, expected):
    assert server._quartile_from_percentile(pct) == expected


@pytest.mark.parametrize("bad", [None, "", "n/a"])
def test_quartile_from_percentile_rejects_junk(bad):
    assert server._quartile_from_percentile(bad) is None


def test_quartile_from_rank():
    assert server._quartile_from_rank(1, 100) == "Q1"
    assert server._quartile_from_rank(100, 100) == "Q4"
    assert server._quartile_from_rank(0, 0) is None


def test_best_quartile_picks_strongest():
    areas = [{"quartile": "Q3"}, {"quartile": "Q1"}, {"quartile": None}]
    assert server._best_quartile(areas) == "Q1"
    assert server._best_quartile([{"quartile": None}]) is None


# ── get_journal_metrics ───────────────────────────────────────────────────────

_SERIAL_TITLE_JSON = {
    "serial-metadata-response": {
        "entry": [{
            "dc:title": "Administrative Science Quarterly",
            "dc:publisher": "SAGE Publications",
            "prism:issn": "0001-8392",
            "SJRList": {"SJR": [{"@year": "2024", "$": "5.12"}]},
            "SNIPList": {"SNIP": [{"@year": "2024", "$": "3.20"}]},
            "subject-area": [{"@code": "1403", "$": "Business and International Management"}],
            "citeScoreYearInfoList": {
                "citeScoreCurrentMetric": "11.4",
                "citeScoreCurrentMetricYear": "2024",
                "citeScoreYearInfo": [{
                    "@year": "2024",
                    "citeScoreInformationList": [{
                        "citeScoreInfo": [{
                            "citeScoreSubjectRank": [
                                {"subjectCode": "1403", "rank": "3", "percentile": "98"},
                                {"subjectCode": "3312", "subjectName": "Sociology", "rank": "88", "percentile": "60"},
                            ]
                        }]
                    }]
                }]
            },
        }]
    }
}

_SCIMAGO_HEADER = "Title;Issn;SJR;SJR Best Quartile;Publisher;Year;Categories\n"
_SCIMAGO_ROW = ("Administrative Science Quarterly;00018392, 19303815;5,123;Q1;SAGE;2024;"
                "Business and International Management (Q1); Sociology and Political Science (Q2)")

# Scimago exports are semicolon-separated, but the Categories column uses "; "
# internally. Quoted is the well-formed shape; unquoted spills into extra fields
# and the loader has to stitch it back. Both are exercised below.
_SCIMAGO_FIXTURE = _SCIMAGO_HEADER + _SCIMAGO_ROW.replace(
    "Business and International", '"Business and International'
).replace("(Q2)", '(Q2)"') + "\n"
_SCIMAGO_FIXTURE_UNQUOTED = _SCIMAGO_HEADER + _SCIMAGO_ROW + "\n"


@pytest.fixture(autouse=True)
def _reset_journal_state():
    server._journal_cache.clear()
    server._scimago_index = None
    yield
    server._journal_cache.clear()
    server._scimago_index = None


def _scimago_fixture(tmp_path, monkeypatch):
    path = tmp_path / "scimago.csv"
    path.write_text(_SCIMAGO_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(server, "SCIMAGO_CSV", path)
    return path


@pytest.mark.asyncio
async def test_get_journal_metrics_parses_scopus(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "content/serial/title" in str(request.url)
        assert "issn=0001-8392" in str(request.url)
        return httpx.Response(200, json=_SERIAL_TITLE_JSON,
                              headers={"X-RateLimit-Remaining": "18000"})

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    out = await server.get_journal_metrics("0001-8392")

    assert out["source"] == "scopus"
    assert out["journal_title"] == "Administrative Science Quarterly"
    assert out["sjr"] == 5.12 and out["snip"] == 3.20 and out["citescore"] == 11.4
    assert out["best_quartile"] == "Q1"
    # subject-area supplies the readable name when the rank block omits it
    assert out["subject_areas"][0]["category"] == "Business and International Management"
    assert out["subject_areas"][0]["quartile"] == "Q1"
    assert out["subject_areas"][1]["category"] == "Sociology"
    assert out["subject_areas"][1]["quartile"] == "Q2"
    # the Serial Title call must go through _scopus_get, so quota is captured
    assert server._quota_info["remaining"] == "18000"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_get_journal_metrics_falls_back_to_scimago(status, tmp_path, monkeypatch):
    """An unentitled key or an unknown journal must degrade, not fail."""
    _scimago_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "scopus_client",
                        lambda: _client(lambda r: httpx.Response(status, json={})))

    out = await server.get_journal_metrics("0001-8392")
    assert out["source"] == "scimago"
    assert out["best_quartile"] == "Q1"
    assert out["sjr"] == 5.123
    assert [a["category"] for a in out["subject_areas"]] == [
        "Business and International Management", "Sociology and Political Science"
    ]
    assert out["subject_areas"][1]["quartile"] == "Q2"


@pytest.mark.asyncio
async def test_get_journal_metrics_matches_secondary_issn(tmp_path, monkeypatch):
    """The Scimago Issn column lists print and electronic ISSNs together."""
    _scimago_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "scopus_client",
                        lambda: _client(lambda r: httpx.Response(404, json={})))
    out = await server.get_journal_metrics("1930-3815")
    assert out["journal_title"] == "Administrative Science Quarterly"


@pytest.mark.asyncio
async def test_get_journal_metrics_reports_when_nothing_found(tmp_path, monkeypatch):
    _scimago_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "scopus_client",
                        lambda: _client(lambda r: httpx.Response(404, json={})))
    out = await server.get_journal_metrics("0000-0019")
    assert "error" in out and "Scimago" in out["error"]


@pytest.mark.asyncio
async def test_get_journal_metrics_rejects_invalid_issn():
    out = await server.get_journal_metrics("not-an-issn")
    assert "error" in out and "not a valid ISSN" in out["error"]


@pytest.mark.asyncio
async def test_get_journal_metrics_caches_by_issn(monkeypatch):
    """Several hits in one result set usually share a journal — pay Scopus once."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_SERIAL_TITLE_JSON)

    monkeypatch.setattr(server, "scopus_client", lambda: _client(handler))
    await server.get_journal_metrics("0001-8392")
    await server.get_journal_metrics("00018392")   # same journal, other spelling
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_journal_metrics_raises_on_upstream_failure(monkeypatch):
    """A 500 is not an entitlement problem — it must not be masked as a fallback."""
    monkeypatch.setattr(server, "asyncio", server.asyncio)
    monkeypatch.setattr(server, "scopus_client",
                        lambda: _client(lambda r: httpx.Response(500, json={})))

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(server.asyncio, "sleep", _no_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await server.get_journal_metrics("0001-8392")


@pytest.mark.asyncio
async def test_scimago_handles_unquoted_categories(tmp_path, monkeypatch):
    """An unquoted export must not collapse to a single category."""
    path = tmp_path / "scimago.csv"
    path.write_text(_SCIMAGO_FIXTURE_UNQUOTED, encoding="utf-8")
    monkeypatch.setattr(server, "SCIMAGO_CSV", path)
    monkeypatch.setattr(server, "scopus_client",
                        lambda: _client(lambda r: httpx.Response(404, json={})))

    out = await server.get_journal_metrics("0001-8392")
    assert [a["category"] for a in out["subject_areas"]] == [
        "Business and International Management", "Sociology and Political Science"
    ]


def test_scimago_missing_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SCIMAGO_CSV", tmp_path / "absent.csv")
    assert server._load_scimago() == {}
