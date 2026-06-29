import asyncio
import os
import logging
import re
from typing import Any

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
# Unpaywall and OpenAlex ask for a contact email in requests (polite pool).
# Set UNPAYWALL_EMAIL in your environment; falls back to a generic address.
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "research-mcp@example.com")

server = Server("research-mcp")


def get_api_key() -> str:
    key = os.environ.get("SCOPUS_API_KEY", "")
    if not key:
        raise ValueError("SCOPUS_API_KEY environment variable not set.")
    return key


def scopus_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"X-ELS-APIKey": get_api_key(), "Accept": "application/json"},
        timeout=30.0, follow_redirects=True,
    )


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                             headers={"User-Agent": f"ScopusEnhanced/0.1 (mailto:{UNPAYWALL_EMAIL})"})


# ── Scopus Search ──────────────────────────────────────────────────────────────

async def search_scopus(query: str, count: int = 5, sort: str = "coverDate") -> list[dict]:
    async with scopus_client() as c:
        r = await c.get(SCOPUS_BASE + "content/search/scopus",
                        params={"query": query, "count": count, "sort": sort, "view": "STANDARD"})
        r.raise_for_status()
        data = r.json()
    entries = data.get("search-results", {}).get("entry", [])
    results = []
    for e in entries:
        results.append({
            "scopus_id": e.get("dc:identifier", "").replace("SCOPUS_ID:", ""),
            "title":     e.get("dc:title"),
            "creator":   e.get("dc:creator"),
            "publication_name": e.get("prism:publicationName"),
            "cover_date":  e.get("prism:coverDate"),
            "doi":         e.get("prism:doi"),
            "cited_by_count": e.get("citedby-count"),
            "aggregation_type": e.get("prism:aggregationType"),
            "url": next((l["@href"] for l in e.get("link", []) if l.get("@ref") == "scopus"), None),
        })
    return results


# ── Abstract details: Scopus metadata + CrossRef abstract ─────────────────────

async def get_abstract_details(scopus_id: str) -> dict:
    sid = scopus_id.replace("SCOPUS_ID:", "")

    # Scopus standard view (no FULL — no institutional access needed)
    async with scopus_client() as c:
        r = await c.get(SCOPUS_BASE + f"content/abstract/scopus_id/{sid}")
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

    # Try to get abstract text from CrossRef (free, no auth)
    abstract = None
    if doi:
        try:
            async with http_client() as c:
                cr = await c.get(CROSSREF_BASE + doi)
                if cr.status_code == 200:
                    msg = cr.json().get("message", {})
                    raw = msg.get("abstract", "")
                    # Strip JATS XML tags
                    abstract = re.sub(r"<[^>]+>", "", raw).strip() or None
        except Exception as e:
            logger.warning(f"CrossRef abstract lookup failed: {e}")

    return {
        "scopus_id":       coredata.get("dc:identifier", "").replace("SCOPUS_ID:", ""),
        "doi":             doi,
        "title":           coredata.get("dc:title"),
        "abstract":        abstract or "(Abstract not available via CrossRef — may require institutional Scopus access)",
        "publication_name": coredata.get("prism:publicationName"),
        "cover_date":      coredata.get("prism:coverDate"),
        "cited_by_count":  coredata.get("citedby-count"),
        "volume":          coredata.get("prism:volume"),
        "page_range":      coredata.get("prism:pageRange"),
        "open_access":     coredata.get("openaccessFlag"),
        "authors":         authors,
        "url": next((l["@href"] for l in coredata.get("link", []) if l.get("@ref") == "scopus"), None),
    }


# ── Author profile: Semantic Scholar (h-index, no institutional access needed) ─

async def get_author_profile(author_name: str) -> dict:
    """
    Search OpenAlex for an author by name.
    Returns h-index, citation count, paper count and affiliations.
    """
    async with http_client() as c:
        r = await c.get(
            OPENALEX_BASE + "authors",
            params={
                "search": author_name,
                "select": "id,display_name,cited_by_count,works_count,summary_stats,last_known_institutions,ids",
                "per-page": 3,
            },
        )
        if r.status_code != 200:
            return {"error": f"OpenAlex returned HTTP {r.status_code}"}
        data = r.json()

    results = data.get("results", [])
    if not results:
        return {"error": f"No author found for '{author_name}' in OpenAlex"}

    authors = []
    for a in results:
        stats = a.get("summary_stats", {})
        institutions = [i.get("display_name", "") for i in (a.get("last_known_institutions") or [])]
        oa_id = (a.get("id") or "").replace("https://openalex.org/", "")
        authors.append({
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
    return {"matches": authors, "note": "Top 3 matches from OpenAlex — verify by name/affiliation."}


# ── Citing papers ─────────────────────────────────────────────────────────────

async def get_citing_papers(scopus_id: str, count: int = 5, sort: str = "coverDate") -> list[dict]:
    sid = scopus_id.replace("SCOPUS_ID:", "")
    return await search_scopus(f"REFEID({sid})", count=count, sort=sort)


# ── PDF link via Unpaywall ────────────────────────────────────────────────────

async def get_pdf_link(doi: str) -> dict:
    doi = doi.strip()
    result = {"doi": doi, "oa_pdf_url": None, "oa_status": None, "source": None}
    try:
        async with http_client() as c:
            r = await c.get(UNPAYWALL_BASE + doi, params={"email": UNPAYWALL_EMAIL})
            if r.status_code == 200:
                uw = r.json()
                result["oa_status"]   = uw.get("oa_status")
                result["journal_is_oa"] = uw.get("journal_is_oa")
                best = uw.get("best_oa_location") or {}
                pdf = best.get("url_for_pdf") or best.get("url")
                if pdf:
                    result["oa_pdf_url"] = pdf
                    result["source"]     = best.get("host_type", "unpaywall")
                    return result
            elif r.status_code == 404:
                result["note"] = "DOI not found in Unpaywall."
                return result
            elif r.status_code == 422:
                # Unpaywall rejects placeholder / missing emails.
                result["error"] = (
                    "Unpaywall rejected the request: a valid contact email is required. "
                    "Set the UNPAYWALL_EMAIL environment variable to your own email address."
                )
                return result
    except Exception as e:
        logger.warning(f"Unpaywall failed: {e}")

    result["note"] = "No open-access PDF found. Article may require institutional/subscription access."
    return result


# ── MCP wiring ────────────────────────────────────────────────────────────────

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_scopus",
            description="Search for documents in Scopus using a query string.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Scopus query (e.g. 'TITLE(agile) AND PUBYEAR > 2020')."},
                    "count": {"type": "integer", "description": "Results to return (default 5, max 25).", "default": 5, "maximum": 25},
                    "sort":  {"type": "string", "description": "Sort by 'coverDate' or 'relevancy'.", "default": "coverDate"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_abstract_details",
            description="Retrieve full metadata for a Scopus document plus abstract text from CrossRef (where available).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {"type": "string", "description": "The Scopus document ID."},
                },
                "required": ["scopus_id"],
            },
        ),
        types.Tool(
            name="get_author_profile",
            description="Look up an author's h-index, citation count, paper count and affiliations via Semantic Scholar. Input the author's name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "author_name": {"type": "string", "description": "Full name of the author (e.g. 'Jane Smith')."},
                },
                "required": ["author_name"],
            },
        ),
        types.Tool(
            name="get_citing_papers",
            description="Retrieve papers that have cited the specified Scopus document (forward citations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {"type": "string", "description": "Scopus ID of the document."},
                    "count": {"type": "integer", "description": "Results to return (default 5, max 25).", "default": 5, "maximum": 25},
                    "sort":  {"type": "string", "description": "Sort by 'coverDate' or 'relevancy'.", "default": "coverDate"},
                },
                "required": ["scopus_id"],
            },
        ),
        types.Tool(
            name="get_pdf_link",
            description="Find an open-access PDF for an article via DOI using Unpaywall. Returns the PDF URL if freely available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {"type": "string", "description": "The DOI of the article (e.g. '10.1016/j.tourman.2026.105478')."},
                },
                "required": ["doi"],
            },
        ),
        types.Tool(
            name="get_quota_status",
            description="Reminder about Scopus API quota (updated per-request by Elsevier response headers).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    if not arguments:
        arguments = {}
    try:
        if name == "search_scopus":
            result = await search_scopus(arguments["query"], count=arguments.get("count", 5), sort=arguments.get("sort", "coverDate"))
        elif name == "get_abstract_details":
            result = await get_abstract_details(arguments["scopus_id"])
        elif name == "get_author_profile":
            result = await get_author_profile(arguments["author_name"])
        elif name == "get_citing_papers":
            result = await get_citing_papers(arguments["scopus_id"], count=arguments.get("count", 5), sort=arguments.get("sort", "coverDate"))
        elif name == "get_pdf_link":
            result = await get_pdf_link(arguments["doi"])
        elif name == "get_quota_status":
            result = {"info": "Scopus API quota is tracked per-request via Elsevier response headers."}
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=str(result))]
    except Exception as e:
        logger.error(f"Error in {name}: {e}")
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


def start():
    asyncio.run(main())


if __name__ == "__main__":
    start()
