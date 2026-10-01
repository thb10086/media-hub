from __future__ import annotations

import json
from pathlib import Path

from hub.cli import main
from hub.main import create_app
from hub.settings import Settings

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
