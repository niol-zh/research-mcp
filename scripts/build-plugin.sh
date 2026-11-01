#!/usr/bin/env bash
# build-plugin.sh — package research-mcp as a Claude Cowork plugin (zip).
#
# Usage: scripts/build-plugin.sh [output.zip]
#   default output: research-mcp-v<version>-plugin.zip in the current directory
#
# The plugin is the working tree reduced to what the server needs at runtime
# (tracked and untracked-but-not-ignored files, so a release build from a clean
# checkout and a local build before committing give the same result), plus a
# uv.lock so every install resolves the same dependencies. Secrets never go in:
# users create ~/.research-mcp/config.json instead (see config.example.json).
# No top-level bin/ — that breaks the Cowork upload.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
version="$(grep -m1 '^version = ' "$repo/pyproject.toml" | cut -d'"' -f2)"
out="${1:-$PWD/research-mcp-v${version}-plugin.zip}"
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
(cd "$work" && uv lock --quiet)

rm -f "$out"
(cd "$work" && zip -q -r -X "$out" . -x '*.DS_Store')

echo "Built $out"
unzip -l "$out"
