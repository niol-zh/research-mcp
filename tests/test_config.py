"""Settings resolution: environment variable first, then the local config file.

The config file exists for hosts without a settings dialog (Claude Cowork
plugins), so a Scopus key never has to be shipped inside a plugin.
"""
import json

import pytest

from research_mcp import server


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point the server at a config file under tmp_path and clear the memo."""
    path = tmp_path / "config.json"
    monkeypatch.setenv(server.CONFIG_FILE_ENV, str(path))
    for var in ("SCOPUS_API_KEY", "UNPAYWALL_EMAIL", server.EXPORT_DIR_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_config_cache", None)
    yield path
    monkeypatch.setattr(server, "_config_cache", None)


def test_missing_file_means_no_settings(_isolated_config):
    assert server._load_config_file() == {}
    assert server._setting("SCOPUS_API_KEY", "scopus_api_key") == ""


def test_file_supplies_settings(_isolated_config):
    _isolated_config.write_text(json.dumps({
        "scopus_api_key": "k-from-file", "unpaywall_email": "me@example.org",
        "export_dir": "/tmp/exports", "unknown": "ignored",
    }), encoding="utf-8")
    assert server.get_api_key() == "k-from-file"
    assert server._setting("UNPAYWALL_EMAIL", "unpaywall_email") == "me@example.org"
    assert server._setting(server.EXPORT_DIR_ENV, "export_dir") == "/tmp/exports"
    assert "unknown" not in server._load_config_file()


def test_environment_wins_over_file(_isolated_config, monkeypatch):
    _isolated_config.write_text(json.dumps({"scopus_api_key": "k-from-file"}), encoding="utf-8")
    monkeypatch.setenv("SCOPUS_API_KEY", "k-from-env")
    assert server.get_api_key() == "k-from-env"


def test_empty_environment_falls_through_to_file(_isolated_config, monkeypatch):
    _isolated_config.write_text(json.dumps({"scopus_api_key": "k-from-file"}), encoding="utf-8")
    monkeypatch.setenv("SCOPUS_API_KEY", "")
    assert server.get_api_key() == "k-from-file"


def test_broken_file_is_not_fatal(_isolated_config):
    _isolated_config.write_text("{not json", encoding="utf-8")
    assert server._load_config_file() == {}
    with pytest.raises(ValueError) as exc:
        server.get_api_key()
    # the error tells the user both ways to supply the key
    assert "SCOPUS_API_KEY" in str(exc.value) and str(_isolated_config) in str(exc.value)


def test_file_with_bom_is_accepted(_isolated_config):
    # Notepad on Windows writes UTF-8 with a BOM.
    _isolated_config.write_bytes(b"\xef\xbb\xbf" + json.dumps({"scopus_api_key": "k"}).encode())
    assert server.get_api_key() == "k"


@pytest.mark.asyncio
async def test_export_dir_from_file(_isolated_config, tmp_path, monkeypatch):
    out = await server.export_scopus_results("q", "x")
    assert server.EXPORT_DIR_ENV in out["error"] and "export_dir" in out["error"]
    export_dir = tmp_path / "exports"
    _isolated_config.write_text(json.dumps({"export_dir": str(export_dir)}), encoding="utf-8")
    monkeypatch.setattr(server, "_config_cache", None)
    monkeypatch.setattr(server, "SCOPUS_EXPORT_PAUSE", 0.0)

    import httpx

    def handler(request):
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": []}})
    monkeypatch.setattr(server, "scopus_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await server.export_scopus_results("q", "x")
    assert out["path"] == str(export_dir / "x.csv")
