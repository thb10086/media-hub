from __future__ import annotations

import pytest
from pydantic import ValidationError

from hub.settings import Settings

DTK_URL = "http://dtk-api:8000"


def test_empty_environment_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A template line such as ``HUB_DTK_API_KEY=`` must not become an empty secret."""
    monkeypatch.setenv("HUB_DTK_BASE_URL", "")
    monkeypatch.setenv("HUB_DTK_API_KEY", "")

    settings = Settings(_env_file=None)

    assert settings.dtk_base_url is None
    assert settings.dtk_api_key is None


def test_a_dtk_url_with_a_blank_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_DTK_BASE_URL", DTK_URL)
    monkeypatch.setenv("HUB_DTK_API_KEY", "")

    with pytest.raises(ValidationError, match="HUB_DTK_API_KEY"):
        Settings(_env_file=None)
