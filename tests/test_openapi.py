from __future__ import annotations

from typing import Any

import pytest

from hub.constants import HEADER_API_KEY
from hub.main import create_app
from hub.settings import Settings

ERROR_REF = {"$ref": "#/components/schemas/ErrorEnvelope"}


@pytest.fixture
def spec(tmp_path) -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'hub.db').as_posix()}",
        media_dir=str(tmp_path / "media"),
    )
    return create_app(settings, configure_logging=False).openapi()


def _operations(spec: dict[str, Any]):
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            yield f"{method.upper()} {path}", operation


def test_the_api_key_is_a_declared_security_scheme(spec: dict[str, Any]) -> None:
    scheme = spec["components"]["securitySchemes"]["ApiKeyAuth"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", HEADER_API_KEY)
    assert {"ApiKeyAuth": []} in spec["paths"]["/api/v1/douyin/search"]["get"]["security"]
    assert "security" not in spec["paths"]["/api/v1/admin/tenants"]["get"]


def test_no_operation_promises_a_422_the_api_never_sends(spec: dict[str, Any]) -> None:
    for where, operation in _operations(spec):
        assert "422" not in operation.get("responses", {}), where
    assert "HTTPValidationError" not in spec["components"]["schemas"]


def test_validation_failures_are_documented_as_400_in_the_envelope(spec: dict[str, Any]) -> None:
    response = spec["paths"]["/api/v1/douyin/search"]["get"]["responses"]["400"]
    assert response["content"]["application/json"]["schema"] == ERROR_REF
    envelope = spec["components"]["schemas"]["ErrorEnvelope"]
    assert set(envelope["required"]) == {"success", "data", "error", "meta"}
