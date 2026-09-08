"""Consistency checks for the Claude Desktop extension manifest (manifest.json).

The manifest is packed into the .mcpb bundle by the release workflow. These
tests catch the drift that would otherwise only surface after a release:
a version bumped in one place but not the other, a tool added to the server but
not listed in the manifest, or an entry point that no longer exists.
"""
import json
import re
from pathlib import Path

import pytest

from research_mcp import server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    # A regex instead of tomllib: tomllib only exists from Python 3.11 and the
    # release workflow greps the same line, so both agree on what "the version" is.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no version line"
    return match.group(1)


def test_manifest_uses_uv_server_type(manifest):
    assert manifest["manifest_version"] == "0.4"
    assert manifest["server"]["type"] == "uv"
    assert manifest["server"]["mcp_config"]["command"] == "uv"


def test_manifest_version_matches_pyproject(manifest):
    assert manifest["version"] == _pyproject_version()


def test_manifest_entry_point_exists(manifest):
    entry = manifest["server"]["entry_point"]
    assert (ROOT / entry).is_file(), f"entry_point {entry} does not exist"
    # The host runs the same file through `uv run`, so the args must name it too.
    assert entry in manifest["server"]["mcp_config"]["args"]


def test_manifest_prefers_existing_python(manifest):
    args = manifest["server"]["mcp_config"]["args"]
    assert args[:3] == ["run", "--python-preference", "system"]


def test_manifest_wires_user_config_into_env(manifest):
    env = manifest["server"]["mcp_config"]["env"]
    for key, var in (("scopus_api_key", "SCOPUS_API_KEY"), ("unpaywall_email", "UNPAYWALL_EMAIL")):
        assert key in manifest["user_config"], f"user_config.{key} missing"
        assert env[var] == "${user_config." + key + "}"
    assert manifest["user_config"]["scopus_api_key"]["sensitive"] is True


@pytest.mark.asyncio
async def test_manifest_lists_every_server_tool(manifest):
    served = {tool.name for tool in await server.handle_list_tools()}
    listed = [tool["name"] for tool in manifest["tools"]]
    assert len(listed) == len(set(listed)), "duplicate tool names in manifest"
    assert set(listed) == served


def test_unpaywall_email_falls_back_when_empty(monkeypatch):
    # The extension dialog can hand over an empty string; that must not become
    # an empty mailto in the User-Agent.
    import importlib

    monkeypatch.setenv("UNPAYWALL_EMAIL", "")
    reloaded = importlib.reload(server)
    try:
        assert reloaded.UNPAYWALL_EMAIL == "research-mcp@example.com"
    finally:
        monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
        importlib.reload(server)
