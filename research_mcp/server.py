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

# OpenAlex accepts at most 50 values in a single OR-filter (a|b|c).
OPENALEX_MAX_BATCH = 50

# Small stop-list for the lexical overlap signal in assess_relevance. Deliberately
# short: it only needs to strip filler so the matched/missing terms stay readable.
_STOPWORDS = frozenset("""
a an and are as at be been being between both but by can could during each for
from had has have here how if in into is it its may might more most no not of on
or other others our over paper papers should such than that the their them then
there these this those to under use used using was we were what when which who
will with within without would you your study studies research article approach
based new all any also
""".split())

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
_OPENALEX_ID_RE = re.compile(r"^W\d+$", re.IGNORECASE)

# Fields needed to summarise a work; abstract/topics only where actually used.
_SUMMARY_SELECT = "id,doi,display_name,publication_year,cited_by_count,primary_location"
_RELEVANCE_SELECT = _SUMMARY_SELECT + ",abstract_inverted_index,topics,keywords"

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


# ── OpenAlex helpers ──────────────────────────────────────────────────────────

async def _openalex_get(path: str, params: Optional[dict] = None) -> httpx.Response:
    """GET an OpenAlex endpoint, always identifying us for the polite pool."""
    merged = {"mailto": UNPAYWALL_EMAIL}
    if params:
        merged.update(params)
    return await _get_with_retry(http_client(), OPENALEX_BASE + path, params=merged)


def _classify_identifier(identifier: str) -> tuple[str, str]:
    """Classify a paper identifier as ('openalex' | 'doi' | 'scopus' | 'unknown', value)."""
    ident = (identifier or "").strip()
    for prefix in ("https://openalex.org/", "https://doi.org/", "http://dx.doi.org/", "doi:"):
        if ident.lower().startswith(prefix):
            ident = ident[len(prefix):]
            break
    if ident.upper().startswith("SCOPUS_ID:"):
        ident = ident.split(":", 1)[1]
        return ("scopus", ident) if ident.isdigit() else ("unknown", ident)
    if _OPENALEX_ID_RE.match(ident):
        return "openalex", ident.upper()
    if ident.startswith("10."):
        return "doi", ident
    if ident.isdigit():
        return "scopus", ident
    return "unknown", ident


async def _openalex_ref(identifier: str) -> tuple[Optional[str], Optional[dict]]:
    """Map any supported identifier onto an OpenAlex entity reference.

    Returns (ref, error). `ref` is either a bare work ID ("W123") or "doi:10.x/y".
    Scopus IDs cost one Scopus call to look the DOI up; DOIs are quota-free.
    """
    kind, value = _classify_identifier(identifier)
    if kind == "scopus":
        details = await get_abstract_details(value)
        if details.get("error"):
            return None, {"error": f"Could not resolve Scopus ID {value}: {details['error']}"}
        doi = details.get("doi")
        if not doi:
            return None, {"error": f"Scopus document {value} has no DOI, so it cannot be matched in OpenAlex."}
        kind, value = "doi", doi
    if kind == "openalex":
        return value, None
    if kind == "doi":
        return f"doi:{value}", None
    return None, {
        "error": f"Unrecognised identifier '{identifier}'. "
                 "Provide a DOI, an OpenAlex work ID (W...), or a Scopus ID."
    }


def _reconstruct_abstract(inverted: Optional[dict]) -> Optional[str]:
    """Rebuild plain text from OpenAlex's abstract_inverted_index ({term: [positions]})."""
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for term, idxs in inverted.items():
        for i in idxs or []:
            positions[i] = term
    return " ".join(positions[i] for i in sorted(positions)) or None


def _work_summary(w: dict) -> dict:
    oa_id = (w.get("id") or "").replace("https://openalex.org/", "")
    return {
        "openalex_id":    oa_id or None,
        "doi":            (w.get("doi") or "").replace("https://doi.org/", "") or None,
        "title":          w.get("display_name"),
        "year":           w.get("publication_year"),
        "venue":          ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
        "cited_by_count": w.get("cited_by_count"),
        "url":            f"https://openalex.org/{oa_id}" if oa_id else None,
    }


async def _openalex_batch(ids: list[str], select: str = _SUMMARY_SELECT) -> list[dict]:
    """Hydrate OpenAlex work IDs, chunked to the OR-filter limit."""
    out: list[dict] = []
    for i in range(0, len(ids), OPENALEX_MAX_BATCH):
        chunk = ids[i:i + OPENALEX_MAX_BATCH]
        r = await _openalex_get("works", {
            "filter": "openalex_id:" + "|".join(chunk),
            "select": select,
            "per-page": len(chunk),
        })
        if r.status_code != 200:
            logger.warning("OpenAlex batch lookup failed: HTTP %s", r.status_code)
            continue
        out.extend(r.json().get("results", []))
    return out


def _content_terms(text: str) -> set[str]:
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


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
    r = await _openalex_get("authors", {
        "search": author_name,
        "select": "id,display_name,cited_by_count,works_count,summary_stats,last_known_institutions,ids",
        "per-page": 3,
    })
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


# ── Forward citations (papers citing this one) ────────────────────────────────

async def _citing_openalex(identifier: str, count: int) -> dict:
    ref, err = await _openalex_ref(identifier)
    if err:
        return err

    # The cites: filter needs a bare work ID, so resolve a DOI reference first.
    if ref.startswith("doi:"):
        r = await _openalex_get(f"works/{ref}", {"select": "id"})
        if r.status_code == 404:
            return {"error": f"'{identifier}' not found in OpenAlex."}
        if r.status_code != 200:
            return {"error": f"OpenAlex returned HTTP {r.status_code}"}
        ref = (r.json().get("id") or "").replace("https://openalex.org/", "")
        if not ref:
            return {"error": f"Could not resolve '{identifier}' to an OpenAlex work ID."}

    r = await _openalex_get("works", {
        "filter": f"cites:{ref}",
        "select": _SUMMARY_SELECT,
        "sort": "cited_by_count:desc",
        "per-page": count,
    })
    if r.status_code != 200:
        return {"error": f"OpenAlex returned HTTP {r.status_code}"}
    data = r.json()
    return {
        "source_paper": {"openalex_id": ref},
        "direction": "forward",
        "total_citing": data.get("meta", {}).get("count"),
        "citing_papers": [_work_summary(w) for w in data.get("results", [])],
        "note": "Forward citations from OpenAlex (free, no Scopus quota), most-cited first.",
    }


async def get_citing_papers(
    scopus_id: str, count: int = 5, sort: str = "coverDate", source: str = "openalex"
) -> Any:
    """Papers citing a document.

    Defaults to OpenAlex because Scopus gates forward-citation search: REFEID() is a
    restricted field that returns HTTP 400 INVALID_INPUT without an institutional
    subscription, so source='scopus' only works for subscribed requestors.
    """
    if str(source).lower() == "openalex":
        return await _citing_openalex(scopus_id, _clamp_count(count))

    # REFEID() only accepts a Scopus ID. A DOI would build invalid query syntax and
    # come back as an opaque HTTP 400, so resolve it to a Scopus ID first.
    kind, value = _classify_identifier(scopus_id)
    if kind == "doi":
        matches = await search_scopus(f"DOI({value})", count=1)
        sid = matches[0].get("scopus_id") if matches else None
        if not sid:
            return {
                "error": f"No Scopus record found for DOI '{value}'. Scopus forward citations need "
                         "a Scopus ID; try source='openalex', which accepts DOIs directly."
            }
        value = sid
    elif kind != "scopus":
        return {
            "error": f"'{scopus_id}' is not a Scopus ID or DOI. Scopus forward citations need a "
                     "Scopus ID; use source='openalex' for DOIs and OpenAlex work IDs."
        }

    try:
        return await search_scopus(f"REFEID({value})", count=count, sort=sort)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and "INVALID_INPUT" in e.response.text:
            return {
                "error": "Scopus rejected REFEID(): forward-citation search is a restricted field "
                         "that needs an institutional subscription. Use source='openalex' — free, "
                         "no quota, and usually much better coverage.",
                "scopus_status": "INVALID_INPUT",
            }
        raise


# ── Backward citations (papers this one cites) ────────────────────────────────

async def _crossref_references(doi: str) -> list[dict]:
    """Fallback reference list from CrossRef; entries are less structured than OpenAlex."""
    try:
        r = await _get_with_retry(http_client(), CROSSREF_BASE + doi)
        if r.status_code != 200:
            return []
        refs = r.json().get("message", {}).get("reference") or []
    except httpx.HTTPError as e:
        logger.warning("CrossRef reference lookup failed: %s", e)
        return []
    return [{
        "doi":    ref.get("DOI"),
        "title":  ref.get("article-title") or ref.get("volume-title") or ref.get("unstructured"),
        "year":   ref.get("year"),
        "author": ref.get("author"),
    } for ref in refs]


async def get_references(identifier: str, count: int = 10) -> dict:
    """Works cited BY the given paper (backward citations), via OpenAlex.

    Scopus cannot serve this on a free key (view=REF returns 401), so OpenAlex is
    the primary source, with CrossRef as a fallback.
    """
    n = _clamp_count(count)
    ref, err = await _openalex_ref(identifier)
    if err:
        return err

    r = await _openalex_get(f"works/{ref}", {"select": "id,doi,display_name,referenced_works"})
    if r.status_code == 404:
        return {"error": f"'{identifier}' not found in OpenAlex."}
    if r.status_code != 200:
        return {"error": f"OpenAlex returned HTTP {r.status_code}"}
    work = r.json()

    source_paper = {
        "title": work.get("display_name"),
        "doi": (work.get("doi") or "").replace("https://doi.org/", "") or None,
    }
    ref_ids = [w.replace("https://openalex.org/", "") for w in (work.get("referenced_works") or [])]

    if not ref_ids:
        fallback = await _crossref_references(source_paper["doi"]) if source_paper["doi"] else []
        return {
            "source_paper": source_paper,
            "direction": "backward",
            "total_references": len(fallback),
            "references": fallback[:n],
            "note": (
                "OpenAlex holds no reference list for this work; entries came from CrossRef "
                "and are less structured." if fallback else
                "No reference list available from OpenAlex or CrossRef for this work."
            ),
        }

    # Hydrate at most one batch, then surface the most-cited references first.
    hydrated = await _openalex_batch(ref_ids[:OPENALEX_MAX_BATCH])
    refs = sorted(
        (_work_summary(w) for w in hydrated),
        key=lambda x: x.get("cited_by_count") or 0,
        reverse=True,
    )
    result = {
        "source_paper": source_paper,
        "direction": "backward",
        "total_references": len(ref_ids),
        "references": refs[:n],
    }
    if len(ref_ids) > OPENALEX_MAX_BATCH:
        result["note"] = (
            f"This work lists {len(ref_ids)} references; ranking considered the first "
            f"{OPENALEX_MAX_BATCH} and returned the most-cited of those."
        )
    return result


# ── Relevance evidence ────────────────────────────────────────────────────────

async def assess_relevance(identifiers: Any, research_context: str) -> dict:
    """Gather structured evidence for judging whether papers fit a research context.

    Deliberately returns NO verdict: the calling model decides. The server's job is
    to supply the abstract, topical metadata and term overlap needed to decide well,
    for many papers in a single round-trip.
    """
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    idents = [str(i).strip() for i in (identifiers or []) if str(i).strip()][:MAX_COUNT]
    if not idents:
        return {"error": "Provide at least one identifier (DOI, OpenAlex ID or Scopus ID)."}
    if not (research_context or "").strip():
        return {"error": "research_context must describe what you are looking for."}

    dois: list[str] = []
    oa_ids: list[str] = []
    unresolved: list[dict] = []
    for ident in idents:
        ref, err = await _openalex_ref(ident)
        if err:
            unresolved.append({"identifier": ident, "error": err["error"]})
        elif ref.startswith("doi:"):
            dois.append(ref[4:])
        else:
            oa_ids.append(ref)

    works: list[dict] = []
    if oa_ids:
        works.extend(await _openalex_batch(oa_ids, select=_RELEVANCE_SELECT))
    for i in range(0, len(dois), OPENALEX_MAX_BATCH):
        chunk = dois[i:i + OPENALEX_MAX_BATCH]
        r = await _openalex_get("works", {
            "filter": "doi:" + "|".join(chunk),
            "select": _RELEVANCE_SELECT,
            "per-page": len(chunk),
        })
        if r.status_code == 200:
            works.extend(r.json().get("results", []))
        else:
            logger.warning("OpenAlex DOI batch failed: HTTP %s", r.status_code)

    context_terms = _content_terms(research_context)
    papers = []
    for w in works:
        paper = _work_summary(w)
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        topics = [{"name": t.get("display_name"), "score": round(t.get("score") or 0, 2)}
                  for t in (w.get("topics") or [])[:4]]
        keywords = [{"name": k.get("display_name"), "score": round(k.get("score") or 0, 2)}
                    for k in (w.get("keywords") or [])[:8]]
        haystack = " ".join([
            paper.get("title") or "",
            abstract or "",
            " ".join(t["name"] or "" for t in topics),
            " ".join(k["name"] or "" for k in keywords),
        ])
        matched = sorted(context_terms & _content_terms(haystack))
        missing = sorted(context_terms - _content_terms(haystack))
        paper.update({
            "abstract": abstract or "(no abstract available in OpenAlex)",
            "topics": topics,
            "keywords": keywords,
            "lexical_overlap": {
                "matched_terms": matched[:25],
                "missing_terms": missing[:25],
                "coverage": round(len(matched) / len(context_terms), 2) if context_terms else None,
            },
        })
        papers.append(paper)

    out: dict[str, Any] = {
        "research_context": research_context,
        "papers": papers,
        "note": (
            "No relevance verdict is computed server-side — judge each paper yourself from its "
            "abstract, topics and keywords against the research context. 'lexical_overlap' is a "
            "raw word-match signal that ignores synonyms and meaning: treat it as weak supporting "
            "evidence, never as a relevance score."
        ),
    }
    if unresolved:
        out["unresolved"] = unresolved
    return out


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
_IDENTIFIER_SCHEMA = {
    "type": "string",
    "description": "Paper identifier: a DOI (preferred — free, no Scopus quota), an OpenAlex work ID "
                   "(e.g. \"W2156435103\"), or a Scopus ID (costs one Scopus call to resolve the DOI).",
}


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
            description=(
                "Forward citations: papers that CITE a given document, most-cited first. Defaults to "
                "OpenAlex (free, no quota, wide coverage, accepts a DOI or OpenAlex ID). "
                "source='scopus' uses REFEID(), a restricted field that only works with an "
                "institutional Scopus subscription."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "Document identifier: a DOI or OpenAlex work ID (default "
                                       "OpenAlex source), or a Scopus ID.",
                    },
                    "count": _COUNT_SCHEMA,
                    "sort": _SORT_SCHEMA,
                    "source": {
                        "type": "string",
                        "enum": ["openalex", "scopus"],
                        "description": "Which database to query. Default 'openalex'; 'scopus' "
                                       "requires an institutional subscription.",
                        "default": "openalex",
                    },
                },
                "required": ["scopus_id"],
            },
        ),
        types.Tool(
            name="get_references",
            description=(
                "Backward citations: the works a given paper CITES (its reference list), via OpenAlex "
                "with a CrossRef fallback. Free and does not consume Scopus quota — Scopus itself "
                "cannot serve reference lists without an institutional subscription. Returns the "
                "most-cited references first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "identifier": _IDENTIFIER_SCHEMA,
                    "count": {"type": "integer", "description": f"References to return (default 10, max {MAX_COUNT}).", "default": 10, "maximum": MAX_COUNT},
                },
                "required": ["identifier"],
            },
        ),
        types.Tool(
            name="assess_relevance",
            description=(
                "Screen one or more papers against a research context. Returns, per paper, the "
                "abstract (from OpenAlex — broader coverage than CrossRef), scored topics and "
                "keywords, and which of your context's terms do and do not appear. Use it to triage "
                "a whole search result set in one call, then judge relevance yourself: the server "
                "deliberately returns evidence, not a verdict or score."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "identifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"Up to {MAX_COUNT} paper identifiers (DOI, OpenAlex ID or Scopus ID).",
                        "maxItems": MAX_COUNT,
                    },
                    "research_context": {
                        "type": "string",
                        "description": "What you are looking for, e.g. \"psychological safety in "
                                       "distributed software teams\". Be specific: the term-overlap "
                                       "signal is computed from this text.",
                    },
                },
                "required": ["identifiers", "research_context"],
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
            result = await get_citing_papers(
                args.get("scopus_id") or args["identifier"],
                count=args.get("count", 5),
                sort=args.get("sort", "coverDate"),
                source=args.get("source", "openalex"),
            )
        elif name == "get_references":
            result = await get_references(
                args.get("identifier") or args["scopus_id"], count=args.get("count", 10)
            )
        elif name == "assess_relevance":
            result = await assess_relevance(args["identifiers"], args["research_context"])
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
