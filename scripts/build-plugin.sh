#!/usr/bin/env bash
# build-plugin.sh — package research-mcp as a Claude Cowork plugin (zip).
#
# Usage: scripts/build-plugin.sh [--ref <git-ref>] [output.zip]
#   default output: research-mcp-v<version>-plugin.zip in the current directory
#   --ref        git ref the plugin starts the server from (default: v<version>).
#                Use a branch or commit to test a plugin before the tag exists.
#                PLUGIN_SOURCE_REF in the environment does the same.
#
# The plugin's start command is a POSIX shell one-liner: it installs uv if
# missing, then runs `uvx --from <GitHub archive of the ref> research-mcp`.
# Claude Cowork runs plugin servers on Linux (on Windows inside its own VM), so
# nothing on the host is needed, no git, and no plugin-directory placeholders,
# which Cowork does not resolve. uvx picks an installed Python >= 3.10 before
# downloading one. The zip still carries the source (tracked and
# untracked-but-not-ignored files) for transparency, plus a uv.lock. Secrets
# never go in: users create ~/.research-mcp/config.json instead (see
# config.example.json). No top-level bin/ — that breaks the Cowork upload.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
version="$(grep -m1 '^version = ' "$repo/pyproject.toml" | cut -d'"' -f2)"
ref="${PLUGIN_SOURCE_REF:-v$version}"
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) ref="$2"; shift 2 ;;
    --ref=*) ref="${1#--ref=}"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) out="$1"; shift ;;
  esac
done
out="${out:-$PWD/research-mcp-v${version}-plugin.zip}"
case "$out" in /*) ;; *) out="$PWD/$out" ;; esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

(
  cd "$repo"
  git ls-files -z --cached --others --exclude-standard -- \
    .claude-plugin/plugin.json pyproject.toml LICENSE README.md config.example.json research_mcp \
    | tar --null -T - -cf - \
    | tar -x -C "$work"
)
rm -f "$work/research_mcp/data/scimago.csv"
find "$work" -name '__pycache__' -type d -prune -exec rm -rf {} +

# Point the start command at the requested ref: a tag lives under
# archive/refs/tags/<tag>.zip, a branch under archive/refs/heads/<branch>.zip.
manifest="$work/.claude-plugin/plugin.json"
case "$ref" in
  v[0-9]*) archive="archive/refs/tags/$ref.zip" ;;
  *)       archive="archive/refs/heads/$ref.zip" ;;
esac
jq --arg archive "$archive" '
  .mcpServers.research.args |= map(
    gsub("archive/refs/tags/v[0-9][^ ]*\\.zip"; $archive))
' "$manifest" > "$manifest.tmp" && mv "$manifest.tmp" "$manifest"

(cd "$work" && uv lock --quiet)

rm -f "$out"
(cd "$work" && zip -q -r -X "$out" . -x '*.DS_Store')

echo "Built $out (server ref: $ref)"
unzip -l "$out"
