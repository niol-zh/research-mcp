# research-mcp

[![CI](https://github.com/niol-zh/research-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/niol-zh/research-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server for academic literature research.
It combines a **Scopus** search with three free, open scholarly APIs so you get
abstracts, author metrics and open-access PDFs without needing institutional
Scopus entitlements.

## What it does

| Tool | Data source | Returns |
|------|-------------|---------|
| `search_scopus` | Elsevier Scopus | Documents matching a query (title, DOI, citations, …) plus the **total hit count** and search-log fields; pages through larger sets |
| `export_scopus_results` | Elsevier Scopus | A whole result set (up to 2000 hits) written to a CSV/JSON file |
| `get_abstract_details` | Scopus + [CrossRef](https://www.crossref.org) | Metadata + abstract text |
| `get_author_profile` | [OpenAlex](https://openalex.org) | h-index, citation count, paper count, affiliations, ORCID |
| `get_citing_papers` | OpenAlex (default) or Scopus | Papers that cite a given document (**forward** citations) |
| `get_references` | OpenAlex + CrossRef | Papers cited **by** a given document (**backward** citations) |
| `assess_relevance` | OpenAlex | Abstract, topics, keywords and term overlap for screening papers against a research context |
| `get_pdf_link` | [Unpaywall](https://unpaywall.org) | Open-access PDF link for a DOI |
| `get_journal_metrics` | Scopus Serial Title or Scimago | Journal quartile per subject category, CiteScore, SJR, SNIP |
| `get_quota_status` | — | Reminder about Scopus API quota |

### Citation traversal in both directions

`get_citing_papers` (forward) and `get_references` (backward) let you walk a
citation graph from any starting paper. **Both directions rely on OpenAlex**,
because a free Scopus key can serve neither:

- backward — `view=REF` returns `401 AUTHORIZATION_ERROR`
- forward — `REFEID()` is a restricted search field and returns `400 INVALID_INPUT`

OpenAlex covers both for free, with no quota and generally wider recall, so it is
the default for each. Pass a DOI and citation traversal costs no Scopus calls at
all.

### Screening papers for relevance

`assess_relevance` takes a list of papers and a plain-language research context,
and returns the evidence needed to judge each one: the abstract (from OpenAlex,
which covers far more papers than CrossRef), scored topics and keywords, and
which of your context's terms appear or are missing.

It screens up to 25 papers **in a single call**, so a whole `search_scopus`
result set can be triaged in one turn instead of one lookup per paper.

> **It deliberately returns no verdict and no relevance score.** The server has no
> language model; the judgement is left to the model calling the tool, which reads
> the abstract itself. The `lexical_overlap` field is a raw word-match signal that
> ignores synonyms and meaning — useful supporting evidence, not a ranking.

### Why the extra APIs?

A **free** Scopus API key only grants the search endpoint. The Abstract Retrieval
(`view=FULL`) and Author Retrieval endpoints require an **institutional
subscription** and otherwise return `401 AUTHORIZATION_ERROR`. To stay useful for
everyone, this server falls back to:

- **CrossRef** for abstract text (free, no key)
- **OpenAlex** for author metrics incl. h-index (free, no key)
- **Unpaywall** for legal open-access PDFs (free, no key)

If you *do* have institutional Scopus access, the Scopus calls simply return
richer data.

## Requirements

- Python ≥ 3.10
- A free Scopus API key — register at [dev.elsevier.com](https://dev.elsevier.com/apikey/manage)
- Dependencies: only `mcp` and `httpx`

## Getting an Elsevier (Scopus) API key

The Scopus tools need an Elsevier Developer API key. It's free to register.

### Step by step

1. **Create an Elsevier account.** Go to
   [dev.elsevier.com](https://dev.elsevier.com) and click **"I want an API Key"**.
   Sign in, or register a free account (the same account works across Elsevier
   products such as ScienceDirect and Mendeley).

2. **Open the API Key management page.**
   Once signed in, go to
   [dev.elsevier.com/apikey/manage](https://dev.elsevier.com/apikey/manage).

3. **Create a new key.** Click **"Create API Key"** and fill in:
   - **Label** — any name, e.g. `research-mcp`.
   - **Website URL** — required by the form but not validated for server use.
     You can enter `http://localhost` or your GitHub repo URL.
   - Optionally a **CORS domain** — leave blank for server-side use.

4. **Accept the agreements.** Tick the
   [API Service Agreement](https://dev.elsevier.com/api_service_agreement.html)
   and the text & data mining policy, then submit.

5. **Copy the key.** A 32-character hexadecimal key appears (e.g.
   `1a2b3c4d5e6f...`). This is your `SCOPUS_API_KEY`. You can return to the
   manage page anytime to view or revoke it.

### What the free key can and cannot do

| Capability | Free key | Notes |
|------------|:--------:|-------|
| `search_scopus` | ✅ | Up to ~20,000 requests/week |
| `get_citing_papers` | ✅ | Via the default `source="openalex"` — free, no quota |
| Scopus forward citations (`REFEID()`) | ❌ | Restricted search field → HTTP 400 `INVALID_INPUT`; needs an institutional subscription |
| `get_references` | ✅ | Uses OpenAlex/CrossRef, not Scopus — no quota at all |
| `assess_relevance` | ✅ | Uses OpenAlex — no quota (unless you pass Scopus IDs) |
| Scopus full abstract (`view=FULL`) | ❌ | Needs institutional subscription → this server falls back to **CrossRef** |
| Scopus reference list (`view=REF`) | ❌ | Needs institutional subscription → `get_references` uses **OpenAlex** |
| Scopus author retrieval | ❌ | Needs institutional subscription → this server uses **OpenAlex** |
| `get_journal_metrics` | ⚠️ | Uses the Serial Title API, which is entitlement-gated. Without it the tool falls back to a local **Scimago** table |

### Unlocking full Scopus data (optional)

The richer Scopus endpoints are gated by your **institution's subscription**, not
by the key tier. To use them:

- **Register / use the key from your institution's network.** Elsevier binds
  entitlements to the institutional IP range. Create or first-use the key while
  on campus Wi-Fi or your university **VPN**.
- Alternatively, request an **Institutional Token** from your library and set it
  via the `X-ELS-Insttoken` header (not currently wired into this server — open
  an issue if you need it).

Without institutional access the server still works fully: abstracts come from
CrossRef, author metrics from OpenAlex, and PDFs from Unpaywall.

### Quota & fair use

- Watch your weekly quota — every call returns `X-RateLimit-Remaining` headers.
- Scopus serves 25 hits per request on a free key. `search_scopus` pages
  through up to 200 hits per call (8 requests) and `export_scopus_results`
  through up to 2000 (80 requests); `count_only=true` costs a single request.
  Don't run exports in tight loops.
- The key is a secret. Keep it in the `env` block of your MCP config, never
  commit it to git (this repo's `.gitignore` already excludes `.env` and keys).

---

## Installation

### Option A — One-click install (`.mcpb` bundle, recommended)

Every [release](https://github.com/niol-zh/research-mcp/releases) ships a
`research-mcp-vX.Y.Z.mcpb` bundle. Download it and double-click, or drag it into
Claude Desktop's Settings window. Claude Code and MCP for Windows install it too.

The bundle asks for your Scopus API key and contact email in a form — the key is
stored masked, so there is no config file to hand-edit and no environment
variable to set.

It carries no Python dependencies: the bundle declares the `uv` runtime, and the
host resolves `mcp` and `httpx` at first launch. That keeps it at ~23 KB and
platform-independent — vendoring would not work here, because the MCP SDK depends
on the compiled `pydantic-core`, which cannot be bundled portably across macOS,
Windows and Linux.

**It does not bundle Python either.** On first start the host runs `uv` with
`--python-preference system`: a Python ≥ 3.10 that is already installed on the
machine is used as is; only when none qualifies does uv download a private one
(~30 MB, once). That first start can take up to a minute; later starts are
instant. The server logs which interpreter it runs on
(`Starting research-mcp: Python 3.x from … (already installed on this machine)`),
visible in the extension's log in Claude Desktop's settings.

### Option B — Claude Code / Claude Desktop, configured by hand (no server needed)

This is the simplest path. The server runs **locally as a stdio process** —
[`uvx`](https://docs.astral.sh/uv/) fetches it straight from GitHub, so there is
**no web server and no tunnel involved**.

Add this to your MCP config (`~/.claude.json`, or the Claude Desktop
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "research": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/niol-zh/research-mcp", "research-mcp"],
      "env": {
        "SCOPUS_API_KEY": "your-scopus-api-key",
        "UNPAYWALL_EMAIL": "you@example.com",
        "RESEARCH_EXPORT_DIR": "/path/to/your/literature/folder"
      }
    }
  }
}
```

Restart Claude. That's it — `uvx` installs `mcp` + `httpx` in an isolated
environment automatically.

> **`UNPAYWALL_EMAIL` is required for the `get_pdf_link` tool.** Unpaywall rejects
> requests without a real contact email (HTTP 422). Use your own address. All
> other tools work without it.
>
> **`RESEARCH_EXPORT_DIR`** is the folder `export_scopus_results` writes to. Leave
> it out if you never export; the tool then explains what to set. The `.mcpb`
> bundle asks for it as "Export folder" in the install dialog.

#### Local clone instead of GitHub

If you've cloned the repo and want to run from disk:

```json
{
  "mcpServers": {
    "research": {
      "command": "uvx",
      "args": ["--from", "/path/to/research-mcp", "research-mcp"],
      "env": { "SCOPUS_API_KEY": "your-scopus-api-key" }
    }
  }
}
```

---

### Option C — Claude Cowork plugin (zip, runs on your own computer)

Cowork sessions that run on your computer load local MCP servers from
**plugins**. Plugins have no settings dialog, so the key and folders come from
a small config file instead. Three steps:

1. **Install uv** (once). Windows PowerShell:
   `winget install astral-sh.uv` — or `irm https://astral.sh/uv/install.ps1 | iex`.
   macOS: `brew install uv`. Python itself is not required; uv reuses an
   installed Python ≥ 3.10 or fetches one.
2. **Create the config file** at `%USERPROFILE%\.research-mcp\config.json`
   (macOS/Linux: `~/.research-mcp/config.json`), modelled on
   [`config.example.json`](config.example.json):

   ```json
   {
     "scopus_api_key": "your-scopus-api-key",
     "unpaywall_email": "you@example.com",
     "export_dir": "C:/Users/you/Documents/research-mcp"
   }
   ```

   Environment variables, when set, take precedence over the file. A different
   file location can be given via `RESEARCH_MCP_CONFIG`.
3. **Upload the plugin.** Download `research-mcp-vX.Y.Z-plugin.zip` from the
   [latest release](https://github.com/niol-zh/research-mcp/releases/latest)
   and add it under **Customize → Plugins → Add → Upload plugin**. Keep the
   `.zip` extension; the upload rejects `.plugin` files.

The first start installs `mcp` and `httpx` into a private environment and can
take up to a minute; later starts are instant. The server's log shows which
Python it runs on and whether the config file was found.

> Plugins serve **Cowork** sessions. Chat in Claude Desktop uses the `.mcpb`
> extension (Option A) or the hand-written config (Option B) instead.

To build the zip yourself: `scripts/build-plugin.sh` (needs `git`, `uv`, `zip`).

---

### Option D — Claude Cowork via remote connector (needs an HTTPS URL)

Cowork runs in an isolated cloud VM and **cannot reach a local stdio process**.
It needs the server exposed over **HTTPS using the Streamable HTTP transport**.
The easiest way is a free [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/).

You need two pieces running on your machine:

1. **`mcp-proxy`** — wraps the stdio server as a local HTTP server
2. **`cloudflared`** — exposes that local port over a public HTTPS URL

#### 1. Install the helpers

```bash
# mcp-proxy comes via uvx, no install needed
# cloudflared:
#   Windows:  download cloudflared.exe from
#             https://github.com/cloudflare/cloudflared/releases/latest
#   macOS:    brew install cloudflared
#   Linux:    see Cloudflare docs
```

#### 2. Start the HTTP server (terminal 1)

```bash
export SCOPUS_API_KEY="your-scopus-api-key"
export UNPAYWALL_EMAIL="you@example.com"

# --with "mcp<2": uvx resolves mcp-proxy in its own environment, so the cap in
# pyproject.toml does not reach it. Without the pin it pulls mcp 2.x, which no
# longer exports request_ctx, and mcp-proxy dies on import.
uvx --with "mcp<2" mcp-proxy --port 8000 --transport streamablehttp \
  -e SCOPUS_API_KEY "$SCOPUS_API_KEY" \
  -- uvx --from git+https://github.com/niol-zh/research-mcp research-mcp
```

#### 3. Open the tunnel (terminal 2)

```bash
cloudflared tunnel --url http://localhost:8000
```

Cloudflare prints a URL like `https://random-words.trycloudflare.com`.

#### 4. Add the connector in Cowork

- **Customize → Connectors → Add custom connector**
- **Name:** `Research`
- **Remote MCP server URL:** `https://random-words.trycloudflare.com/mcp`
  (note the **`/mcp`** suffix)

> ⚠️ A Quick Tunnel URL is **temporary** — it changes every time you restart
> `cloudflared`, and both terminals must stay open while you use Cowork. For a
> stable URL, deploy the server to a host (Railway, Render, Fly.io, a VPS) or use
> a named Cloudflare Tunnel.

**Windows users:** the repo includes [`scripts/start-tunnel.ps1`](scripts/start-tunnel.ps1),
which starts the server and the tunnel and copies the connector URL to your
clipboard (requires `uvx` and `cloudflared` on `PATH`):

```powershell
$env:SCOPUS_API_KEY = "your-key"
$env:UNPAYWALL_EMAIL = "you@example.com"
./scripts/start-tunnel.ps1
```

---

## Example

A typical research flow in Claude, chaining several tools in one turn:

> **You:** How influential is Amy Edmondson's work, and can you get me an
> open-access PDF of a recent open-science paper to read?

**1. `get_author_profile("Amy Edmondson")`** → OpenAlex returns disambiguated
matches with metrics:

```json
{
  "matches": [
    {
      "openalex_id": "A5003931866",
      "name": "Amy C. Edmondson",
      "h_index": 66,
      "citation_count": 45902,
      "paper_count": 358,
      "i10_index": 132,
      "affiliations": ["Harvard University"],
      "orcid": "0000-0003-4409-913X",
      "url": "https://openalex.org/A5003931866"
    }
  ],
  "note": "Top 3 matches from OpenAlex — verify by name/affiliation."
}
```

**2. `get_pdf_link("10.1371/journal.pone.0173664")`** → Unpaywall finds a gold
open-access PDF:

```json
{
  "doi": "10.1371/journal.pone.0173664",
  "oa_pdf_url": "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0173664&type=printable",
  "oa_status": "gold",
  "source": "publisher",
  "journal_is_oa": true
}
```

> **Claude:** Amy C. Edmondson (Harvard) has an h-index of 66 across 358 papers
> with ~45,900 citations — exceptionally influential in organizational behavior.
> Here is an open-access PDF you can read: [link].

Every tool returns indented JSON, so Claude parses the fields reliably and
combines results across calls. A full literature search would add
`search_scopus` (to find papers) and `get_abstract_details` (Scopus metadata +
abstract text from CrossRef).

## Development

```bash
# install with dev dependencies
uv pip install -e ".[dev]"      # or: pip install -e ".[dev]"

# run the test suite (offline — all HTTP is mocked, no API key needed)
pytest
```

Tests live in [`tests/`](tests/) and mock every external API with
`httpx.MockTransport`, covering result parsing, `count` clamping, retry/backoff
on HTTP 429, quota tracking and error paths.

---

## Tool reference

### `search_scopus`
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | — | Scopus query, e.g. `TITLE(agile) AND PUBYEAR > 2020`. Sent exactly as given. |
| `count` | int | 5 | Results to return (max 200). Scopus serves 25 per request; above 25 the server pages and returns **compact** records |
| `sort` | string | `coverDate` | `coverDate` or `relevancy` |
| `start` | int | 0 | 0-based offset; pass `next_start` from the previous response to page |
| `count_only` | bool | false | Only the hit count, one request, no records |
| `compact` | bool | auto | Force compact (`scopus_id, title, year, doi, cited_by_count, publication_name`) or full records |

The response is an object, not a bare list:

```json
{
  "query": "TITLE-ABS-KEY(\"IT governance\") AND PUBYEAR > 2020",
  "total_results": 1234,
  "returned": 25,
  "start": 0,
  "next_start": 25,
  "retrieved_at": "2026-11-01T13:05:00+01:00",
  "mcp_version": "0.7.0",
  "quota_remaining": "19876",
  "results": [ { "scopus_id": "…", "title": "…", "doi": "…", "…": "…" } ]
}
```

`total_results` comes from Scopus's `opensearch:totalResults`, so it is the
count for the whole query, not for the page. `query`, `retrieved_at`,
`total_results` and `mcp_version` are what a PRISMA-S search log needs per
query. Zero hits give `total_results: 0` and an empty `results` list.

Each full result carries `issn` and `e_issn`, which feed straight into
[`get_journal_metrics`](#get_journal_metrics).

### `export_scopus_results`
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | — | Scopus query, sent exactly as given |
| `filename` | string | — | Bare file name without folder; the extension is added |
| `sort` | string | `coverDate` | `coverDate` or `relevancy` |
| `max_results` | int | 2000 | Stop after this many hits (max 2000 = 80 Scopus requests) |
| `format` | string | `csv` | `csv` (semicolon-separated, UTF-8 with BOM, Excel-ready) or `json` |

Pages through the whole result set and writes it to the folder set by
`RESEARCH_EXPORT_DIR` (the "Export folder" of the `.mcpb` install dialog). A
`<name>.meta.json` with the query, hit count, retrieval time and server version
is written beside the data file. The response carries the hit count, the paths
and `truncated`, never the records: use it when a result set is too large for
the conversation. Existing files are not overwritten.

### `get_abstract_details`
| Param | Type | Description |
|-------|------|-------------|
| `scopus_id` | string | Scopus document ID |

### `get_author_profile`
| Param | Type | Description |
|-------|------|-------------|
| `author_name` | string | Author's full name, e.g. `Amy Edmondson`. Returns the top 3 OpenAlex matches — verify by name/affiliation. |

### `get_citing_papers`
Forward citations — papers that **cite** the given document.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scopus_id` | string | — | A DOI or OpenAlex work ID (default source), or a Scopus ID |
| `count` | int | 5 | Results to return (max 100) |
| `sort` | string | `coverDate` | `coverDate` or `relevancy` (Scopus only) |
| `source` | string | `openalex` | `openalex` (free, no quota, most-cited first) or `scopus` |
| `page` | int | 1 | OpenAlex page (1-based); pass `next_page` from the previous response |
| `start` | int | 0 | Scopus offset (0-based), as in `search_scopus` |

The OpenAlex response reports `total_citing`, `page`, `per_page` and
`next_page`.

> **Why OpenAlex is the default:** Scopus serves forward citations through the
> `REFEID()` search field, which is **restricted**. On a free key it returns
> `HTTP 400 INVALID_INPUT` ("Use of certain field restrictions in the search
> query is not allowed for this requestor"), so `source="scopus"` is only useful
> with an institutional subscription. If you request it anyway, the tool reports
> that restriction rather than leaking a bare HTTP error.

### `get_references`
Backward citations — papers **cited by** the given document, most-cited first.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `identifier` | string | — | DOI, OpenAlex work ID (`W…`) or Scopus ID |
| `count` | int | 10 | References to return (max 100) |
| `start` | int | 0 | Offset into the reference list; pass `next_start` from the previous response |

Reference lists come from OpenAlex, falling back to CrossRef when OpenAlex has
none. Each call ranks a window of at least 50 references from `start` by
citation count and returns the top `count`; `next_start` points at the next
window and `total_references` always reports the true count.

### `assess_relevance`
Evidence for screening papers against a research context — **no verdict, no score**.

| Param | Type | Description |
|-------|------|-------------|
| `identifiers` | string[] | Up to 25 papers (DOI, OpenAlex ID or Scopus ID) |
| `research_context` | string | What you are looking for, e.g. `psychological safety in distributed software teams` |

Returns per paper: title, DOI, year, venue, citation count, abstract, scored
topics and keywords, and `lexical_overlap` (`matched_terms`, `missing_terms`,
`coverage`). Identifiers that cannot be resolved are reported under
`unresolved` rather than silently dropped.

#### Identifiers

`get_references` and `assess_relevance` accept a **DOI** (preferred — free), an
**OpenAlex work ID**, or a **Scopus ID**. A Scopus ID costs one Scopus call to
resolve its DOI first, so pass DOIs when you have them.

### `get_pdf_link`
| Param | Type | Description |
|-------|------|-------------|
| `doi` | string | DOI, e.g. `10.1016/j.tourman.2026.105478` |

### `get_journal_metrics`
| Param | Type | Description |
|-------|------|-------------|
| `issn` | string | Journal ISSN, with or without the hyphen, e.g. `0001-8392` |

Returns the **quartile (Q1–Q4) per subject category**, plus CiteScore, SJR and
SNIP. A journal usually sits in several categories with *different* quartiles, so
`subject_areas` is a list; `best_quartile` gives the strongest one, which is what
papers normally cite.

The `source` field says where the numbers came from — `scopus` from the live
Serial Title API, `scimago` from the local fallback table. The two differ
slightly, so it is worth reporting which one you used.

The Serial Title API needs an entitlement many keys lack. When it is
unavailable the tool falls back to a Scimago table you place at
`research_mcp/data/scimago.csv` — see
[that directory's README](research_mcp/data/README.md) for how to obtain it. The
Scimago data is licensed **CC BY-NC**.

## Scopus query syntax (quick reference)

| Field | Example |
|-------|---------|
| Title | `TITLE(machine learning)` |
| Author | `AUTH(edmondson)` |
| Keywords | `KEY(psychological safety)` |
| Year | `PUBYEAR > 2020`, `PUBYEAR = 2023` |
| Affiliation | `AFFIL(harvard)` |
| Combined | `TITLE(agile) AND KEY(teams) AND PUBYEAR > 2019` |

Full syntax: [Scopus Search Tips](https://dev.elsevier.com/sc_search_tips.html).

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built on the open scholarly infrastructure of
[CrossRef](https://www.crossref.org), [OpenAlex](https://openalex.org) and
[Unpaywall](https://unpaywall.org), plus the [Elsevier Scopus API](https://dev.elsevier.com).
