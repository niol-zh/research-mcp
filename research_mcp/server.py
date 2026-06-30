import asyncio
import json
import os
import logging
import re
from typing import Any, Optional

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research-mcp")

SCOPUS_BASE    = "https://api.elsevier.com/"
CROSSREF_BASE  = "https://api.crossref.org/works/"
OPENALEX_BASE  = "https://api.openalex.org/"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2/"

# Unpaywall and OpenAlex ask for a contact email (their "polite pool").
# Unpaywall *rejects* requests without a real address, so get_pdf_link needs this.
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "research-mcp@example.com")

MAX_COUNT = 25
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4

server = Server("research-mcp")

# Latest Scopus rate-limit headers, refreshed on every Scopus call.
_quota_info: dict[str, Any] = {}

# Shared clients (connection pooling). Created lazily, closed on shutdown.
_scopus_client: Optional[httpx.AsyncClient] = None
_generic_client: Optional[httpx.AsyncClient] = None


def get_api_key() -> str:
    key = os.environ.get("SCOPUS_API_KEY", "")
    if not key:
        raise ValueError("SCOPUS_API_KEY environment variable not set.")
    return key


def scopus_client() -> httpx.AsyncClient:
    global _scopus_client
    if _scopus_client is None or _scopus_client.is_closed:
        _scopus_client = httpx.AsyncClient(
            headers={"X-ELS-APIKey": get_api_key(), "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
        )
    return _scopus_client


def http_client() -> httpx.AsyncClient:
    global _generic_client
    if _generic_client is None or _generic_client.is_closed:
        _generic_client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": f"research-mcp (mailto:{UNPAYWALL_EMAIL})"},
        )
    return _generic_client


async def close_clients() -> None:
    for c in (_scopus_client, _generic_client):
        if c is not None and not c.is_closed:
            await c.aclose()


def _clamp_count(count: Any) -> int:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return 5
    return max(1, min(n, MAX_COUNT))


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, params: Optional[dict] = None
) -> httpx.Response:
    """GET with exponential backoff on rate-limit / transient server errors."""
    backoff = 1.0
    response = None
    for attempt in range(MAX_ATTEMPTS):
        response = await client.get(url, params=params)
        if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
            logger.warning(
                "HTTP %s from %s — retry %d/%d in %.1fs",
                response.status_code, url, attempt + 1, MAX_ATTEMPTS - 1, wait,
            )
            await asyncio.sleep(wait)
            backoff *= 2
            continue
        return response
    return response  # exhausted retries; return last response


def _update_quota(headers: httpx.Headers) -> None:
    if "X-RateLimit-Limit" in headers or "X-RateLimit-Remaining" in headers:
        _quota_info.update({
            "limit": headers.get("X-RateLimit-Limit"),
            "remaining": headers.get("X-RateLimit-Remaining"),
            "reset_epoch": headers.get("X-RateLimit-Reset"),
        })


async def _scopus_get(endpoint: str, params: Optional[dict] = None) -> httpx.Response:
    r = await _get_with_retry(scopus_client(), SCOPUS_BASE + endpoint, params=params)
    _update_quota(r.headers)
    return r


# ── Scopus Search ─────────────────────────────────────────────────────────────

async def search_scopus(query: str, count: int = 5, sort: str = "coverDate") -> list[dict]:
    r = await _scopus_get("content/search/scopus", {
        "query": query, "count": _clamp_count(count), "sort": sort, "view": "STANDARD",
    })
    r.raise_for_status()
    entries = r.json().get("search-results", {}).get("entry", [])
    return [{
        "scopus_id":        e.get("dc:identifier", "").replace("SCOPUS_ID:", ""),
        "title":            e.get("dc:title"),
        "creator":          e.get("dc:creator"),
        "publication_name": e.get("prism:publicationName"),
        "cover_date":       e.get("prism:coverDate"),
        "doi":              e.get("prism:doi"),
        "cited_by_count":   e.get("citedby-count"),
        "aggregation_type": e.get("prism:aggregationType"),
        "url": next((l["@href"] for l in e.get("link", []) if l.get("@ref") == "scopus"), None),
    } for e in entries]


# ── Abstract details: Scopus metadata + CrossRef abstract ─────────────────────

async def get_abstract_details(scopus_id: str) -> dict:
    sid = scopus_id.replace("SCOPUS_ID:", "")

    # Scopus STANDARD view — the FULL view needs an institutional subscription.
    r = await _scopus_get(f"content/abstract/scopus_id/{sid}")
    if r.status_code == 404:
        return {"error": "Document not found"}
    r.raise_for_status()
    data = r.json()

    root = data.get("abstracts-retrieval-response") or data.get("abstract-retrieval-response") or {}
    coredata = root.get("coredata", {})
    authors_raw = root.get("authors", {}).get("author", [])
    if isinstance(authors_raw, dict):
        authors_raw = [authors_raw]

    authors = [{
        "auth_id":  a.get("@auid"),
        "name":     a.get("ce:indexed-name"),
        "surname":  a.get("ce:surname"),
        "initials": a.get("ce:initials"),
    } for a in authors_raw]

    doi = coredata.get("prism:doi")

    # Abstract text comes from CrossRef (free, no auth) since Scopus gates it.
    abstract = None
    if doi:
        try:
            cr = await _get_with_retry(http_client(), CROSSREF_BASE + doi)
            if cr.status_code == 200:
                raw = cr.json().get("message", {}).get("abstract", "")
                abstract = re.sub(r"<[^>]+>", "", raw).strip() or None  # strip JATS tags
        except httpx.HTTPError as e:
            logger.warning("CrossRef abstract lookup failed: %s", e)

    return {
        "scopus_id":        coredata.get("dc:identifier", "").replace("SCOPUS_ID:", ""),
        "doi":              doi,
        "title":            coredata.get("dc:title"),
        "abstract":         abstract or "(Abstract not available via CrossRef — may require institutional Scopus access)",
        "publication_name": coredata.get("prism:publicationName"),
        "cover_date":       coredata.get("prism:coverDate"),
        "cited_by_count":   coredata.get("citedby-count"),
        "volume":           coredata.get("prism:volume"),
        "page_range":       coredata.get("prism:pageRange"),
        "open_access":      coredata.get("openaccessFlag"),
        "authors":          authors,
        "url": next((l["@href"] for l in coredata.get("link", []) if l.get("@ref") == "scopus"), None),
    }


# ── Author profile via OpenAlex (free, no institutional access needed) ────────

async def get_author_profile(author_name: str) -> dict:
    """Search OpenAlex for an author by name; return h-index and citation metrics."""
    r = await _get_with_retry(
        http_client(),
        OPENALEX_BASE + "authors",
        params={
            "search": author_name,
            "select": "id,display_name,cited_by_count,works_count,summary_stats,last_known_institutions,ids",
            "per-page": 3,
        },
    )
    if r.status_code != 200:
        return {"error": f"OpenAlex returned HTTP {r.status_code}"}

    results = r.json().get("results", [])
    if not results:
        return {"error": f"No author found for '{author_name}' in OpenAlex"}

    matches = []
    for a in results:
        stats = a.get("summary_stats", {})
        institutions = [i.get("display_name", "") for i in (a.get("last_known_institutions") or [])]
        oa_id = (a.get("id") or "").replace("https://openalex.org/", "")
        matches.append({
            "openalex_id":    oa_id,
            "name":           a.get("display_name"),
            "h_index":        stats.get("h_index"),
            "citation_count": a.get("cited_by_count"),
            "paper_count":    a.get("works_count"),
            "i10_index":      stats.get("i10_index"),
            "affiliations":   institutions,
            "orcid":          ((a.get("ids") or {}).get("orcid") or "").replace("https://orcid.org/", "") or None,
            "url":            f"https://openalex.org/{oa_id}",
        })
    return {"matches": matches, "note": "Top 3 matches from OpenAlex — verify by name/affiliation."}


# ── Citing papers ─────────────────────────────────────────────────────────────

async def get_citing_papers(scopus_id: str, count: int = 5, sort: str = "coverDate") -> list[dict]:
    sid = scopus_id.replace("SCOPUS_ID:", "")
    return await search_scopus(f"REFEID({sid})", count=count, sort=sort)


# ── PDF link via Unpaywall ────────────────────────────────────────────────────

async def get_pdf_link(doi: str) -> dict:
    doi = doi.strip()
    result: dict[str, Any] = {"doi": doi, "oa_pdf_url": None, "oa_status": None, "source": None}
    try:
        r = await _get_with_retry(http_client(), UNPAYWALL_BASE + doi, params={"email": UNPAYWALL_EMAIL})
        if r.status_code == 200:
            uw = r.json()
            result["oa_status"] = uw.get("oa_status")
            result["journal_is_oa"] = uw.get("journal_is_oa")
            best = uw.get("best_oa_location") or {}
            pdf = best.get("url_for_pdf") or best.get("url")
            if pdf:
                result["oa_pdf_url"] = pdf
                result["source"] = best.get("host_type", "unpaywall")
            else:
                result["note"] = "No open-access PDF found. Article may be subscription-only."
            return result
        if r.status_code == 404:
            result["note"] = "DOI not found in Unpaywall."
            return result
        if r.status_code == 422:
            result["error"] = (
                "Unpaywall rejected the request: a valid contact email is required. "
                "Set the UNPAYWALL_EMAIL environment variable to your own email address."
            )
            return result
        result["error"] = f"Unpaywall returned HTTP {r.status_code}"
        return result
    except httpx.HTTPError as e:
        logger.warning("Unpaywall request failed: %s", e)
        result["error"] = "Unpaywall request failed (network error)."
        return result


def get_quota_status() -> dict:
    if not _quota_info:
        return {"note": "No Scopus request made yet this session — quota headers appear after the first call."}
    return {
        "scopus_weekly_limit": _quota_info.get("limit"),
        "remaining": _quota_info.get("remaining"),
        "reset_epoch": _quota_info.get("reset_epoch"),
        "note": "Values reflect the most recent Scopus response headers (X-RateLimit-*).",
    }


# ── MCP wiring ────────────────────────────────────────────────────────────────

_COUNT_SCHEMA = {"type": "integer", "description": f"Results to return (default 5, max {MAX_COUNT}).", "default": 5, "maximum": MAX_COUNT}
_SORT_SCHEMA = {"type": "string", "description": "Sort by 'coverDate' or 'relevancy'.", "default": "coverDate"}


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_scopus",
            description="Search for documents in Scopus by query string. Returns title, DOI, Scopus ID, citation count and journal for each match.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Scopus query, e.g. \"TITLE(agile) AND PUBYEAR > 2020\"."},
                    "count": _COUNT_SCHEMA,
                    "sort": _SORT_SCHEMA,
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_abstract_details",
            description="Retrieve metadata for a Scopus document (authors, journal, citations) plus the abstract text from CrossRef when available.",
            inputSchema={
                "type": "object",
                "properties": {"scopus_id": {"type": "string", "description": "The Scopus document ID."}},
                "required": ["scopus_id"],
            },
        ),
        types.Tool(
            name="get_author_profile",
            description="Look up an author's h-index, citation count, paper count, ORCID and affiliations via OpenAlex. Provide the author's full name; returns the top 3 matches to disambiguate.",
            inputSchema={
                "type": "object",
                "properties": {"author_name": {"type": "string", "description": "Full name of the author, e.g. \"Amy Edmondson\"."}},
                "required": ["author_name"],
            },
        ),
        types.Tool(
            name="get_citing_papers",
            description="Retrieve papers that cite a given Scopus document (forward citations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {"type": "string", "description": "Scopus ID of the document."},
                    "count": _COUNT_SCHEMA,
                    "sort": _SORT_SCHEMA,
                },
                "required": ["scopus_id"],
            },
        ),
        types.Tool(
            name="get_pdf_link",
            description="Find an open-access PDF for an article by DOI via Unpaywall. Returns the PDF URL if one is legally available, otherwise a note.",
            inputSchema={
                "type": "object",
                "properties": {"doi": {"type": "string", "description": "Article DOI, e.g. \"10.1016/j.tourman.2026.105478\"."}},
                "required": ["doi"],
            },
        ),
        types.Tool(
            name="get_quota_status",
            description="Report the Scopus API rate-limit (weekly quota) from the most recent response headers. Returns a note if no Scopus call has been made yet.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    args = arguments or {}
    try:
        if name == "search_scopus":
            result = await search_scopus(args["query"], count=args.get("count", 5), sort=args.get("sort", "coverDate"))
        elif name == "get_abstract_details":
            result = await get_abstract_details(args["scopus_id"])
        elif name == "get_author_profile":
            result = await get_author_profile(args["author_name"])
        elif name == "get_citing_papers":
            result = await get_citing_papers(args["scopus_id"], count=args.get("count", 5), sort=args.get("sort", "coverDate"))
        elif name == "get_pdf_link":
            result = await get_pdf_link(args["doi"])
        elif name == "get_quota_status":
            result = get_quota_status()
        else:
            return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except KeyError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": f"Missing required argument: {e}"}))]
    except ValueError as e:
        # Configuration errors (e.g. missing API key) — message is safe to surface.
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except httpx.HTTPStatusError as e:
        logger.error("Upstream HTTP error in %s: %s", name, e)
        return [types.TextContent(type="text", text=json.dumps({"error": f"Upstream API error (HTTP {e.response.status_code})."}))]
    except Exception:
        logger.exception("Unexpected error in tool %s", name)
        return [types.TextContent(type="text", text=json.dumps({"error": "An internal error occurred; see server logs."}))]


async def main():
    try:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    finally:
        await close_clients()


def start():
    asyncio.run(main())


if __name__ == "__main__":
    start()
