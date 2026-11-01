"""Export folder resolution: environment, then config file, then default — but
only a folder that can actually be created. A host can hand over an unusable
folder (Claude Desktop resolved "${DOCUMENTS}" to C:\\Users\\.<name>\\Documents
once), and that must not hide the user's own config."""
import json

import httpx
import pytest

from research_mcp import server


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setenv(server.CONFIG_FILE_ENV, str(cfg))
    monkeypatch.delenv(server.EXPORT_DIR_ENV, raising=False)
    monkeypatch.setattr(server, "_config_cache", None)
    monkeypatch.setattr(server, "DEFAULT_EXPORT_DIR", tmp_path / "home" / "research-mcp")
    monkeypatch.setattr(server, "SCOPUS_EXPORT_PAUSE", 0.0)

    def handler(request):
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": []}})
    monkeypatch.setattr(server, "scopus_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return cfg


def _unusable(tmp_path):
    # a path below a regular file cannot be created on any OS
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    return blocker / "sub" / "exports"


def test_environment_wins_when_usable(tmp_path, monkeypatch):
    monkeypatch.setenv(server.EXPORT_DIR_ENV, str(tmp_path / "env"))
    folder, source, skipped = server._resolve_export_dir()
    assert folder == tmp_path / "env" and source.startswith("environment") and skipped == []


@pytest.mark.asyncio
async def test_unusable_environment_falls_back_to_config_file(tmp_path, monkeypatch, _settings):
    monkeypatch.setenv(server.EXPORT_DIR_ENV, str(_unusable(tmp_path)))
    _settings.write_text(json.dumps({"export_dir": str(tmp_path / "from-file")}), encoding="utf-8")
    out = await server.export_scopus_results("q", "x")
    assert out["path"] == str(tmp_path / "from-file" / "x.csv")
    assert out["export_dir_source"].startswith("config file")
    assert "environment" in out["export_dir_note"]


def test_unresolved_placeholder_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv(server.EXPORT_DIR_ENV, "${user_config.export_directory}")
    folder, source, skipped = server._resolve_export_dir()
    assert source == "default" and folder == tmp_path / "home" / "research-mcp"
    assert "placeholder" in skipped[0]


def test_all_unusable_reports_every_attempt(tmp_path, monkeypatch):
    bad = _unusable(tmp_path)
    monkeypatch.setenv(server.EXPORT_DIR_ENV, str(bad))
    monkeypatch.setattr(server, "DEFAULT_EXPORT_DIR", bad / "default")
    folder, source, skipped = server._resolve_export_dir()
    assert folder is None and len(skipped) == 2
