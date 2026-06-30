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
| `search_scopus` | Elsevier Scopus | Documents matching a query (title, DOI, citations, …) |
| `get_abstract_details` | Scopus + [CrossRef](https://www.crossref.org) | Metadata + abstract text |
| `get_author_profile` | [OpenAlex](https://openalex.org) | h-index, citation count, paper count, affiliations, ORCID |
| `get_citing_papers` | Elsevier Scopus | Papers that cite a given document (forward citations) |
| `get_pdf_link` | [Unpaywall](https://unpaywall.org) | Open-access PDF link for a DOI |
| `get_quota_status` | — | Reminder about Scopus API quota |

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
| `get_citing_papers` | ✅ | Uses the search endpoint |
| Scopus full abstract (`view=FULL`) | ❌ | Needs institutional subscription → this server falls back to **CrossRef** |
| Scopus author retrieval | ❌ | Needs institutional subscription → this server uses **OpenAlex** |

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
- Don't hammer the API in tight loops; the search tools cap at 25 results per
  call by design.
- The key is a secret. Keep it in the `env` block of your MCP config, never
  commit it to git (this repo's `.gitignore` already excludes `.env` and keys).

---

## Installation

### Option A — Claude Code / Claude Desktop (recommended, no server needed)

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
        "UNPAYWALL_EMAIL": "you@example.com"
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

### Option B — Claude Cowork (remote connector, needs an HTTPS URL)

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

uvx mcp-proxy --port 8000 --transport streamablehttp \
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
| `query` | string | — | Scopus query, e.g. `TITLE(agile) AND PUBYEAR > 2020` |
| `count` | int | 5 | Results to return (max 25) |
| `sort` | string | `coverDate` | `coverDate` or `relevancy` |

### `get_abstract_details`
| Param | Type | Description |
|-------|------|-------------|
| `scopus_id` | string | Scopus document ID |

### `get_author_profile`
| Param | Type | Description |
|-------|------|-------------|
| `author_name` | string | Author's full name, e.g. `Amy Edmondson`. Returns the top 3 OpenAlex matches — verify by name/affiliation. |

### `get_citing_papers`
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scopus_id` | string | — | Scopus document ID |
| `count` | int | 5 | Results to return (max 25) |
| `sort` | string | `coverDate` | `coverDate` or `relevancy` |

### `get_pdf_link`
| Param | Type | Description |
|-------|------|-------------|
| `doi` | string | DOI, e.g. `10.1016/j.tourman.2026.105478` |

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
