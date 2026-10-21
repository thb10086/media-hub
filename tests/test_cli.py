from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx

from conftest import DTK_URL, envelope
from hub.cli import main
from hub.main import create_app
from hub.settings import Settings, get_settings

COMMITTED_OPENAPI = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def _current_spec() -> dict:
    return create_app(Settings(_env_file=None), configure_logging=False).openapi()


def test_export_openapi_writes_the_document(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    main(["export-openapi", "--output", str(target)])

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == _current_spec()
    assert "/api/v1/douyin/search" in written["paths"]


def test_the_committed_openapi_document_matches_the_code() -> None:
    """docs/openapi.json is what integrators import; it must not drift from the routes."""
    committed = json.loads(COMMITTED_OPENAPI.read_text(encoding="utf-8"))
    assert committed == _current_spec(), "regenerate with: media-hub export-openapi"


@respx.mock
def test_import_jar_sends_the_www_cookies_and_prints_no_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HUB_DTK_BASE_URL", DTK_URL)
    monkeypatch.setenv("HUB_DTK_API_KEY", "dtk-key")
    monkeypatch.setenv("HUB_DOUYIN_USER_AGENT", "UA/1")
    get_settings.cache_clear()
    jar = tmp_path / "guest.json"
    jar.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "ttwid", "value": "secret-ttwid", "domain": ".douyin.com"},
                    {"name": "gfkadpd", "value": "creator", "domain": "creator.douyin.com"},
                ]
            }
        ),
        encoding="utf-8",
    )
    route = respx.post(f"{DTK_URL}/api/v1/admin/identities/import").respond(
        201,
        json=envelope(
            {
                "stored": True,
                "identity_id": "id-1",
                "report": {"authenticated": False, "usable": True, "cookie_names": ["ttwid"]},
            }
        ),
    )
    try:
        main(["import-jar", str(jar)])
    finally:
        get_settings.cache_clear()

    payload = json.loads(route.calls.last.request.content)
    assert (payload["cookies"], payload["user_agent"]) == ("ttwid=secret-ttwid", "UA/1")
    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "identity_id": "id-1",
        "authenticated": False,
        "usable": True,
        "cookie_names": ["ttwid"],
    }
    assert "secret-ttwid" not in printed
