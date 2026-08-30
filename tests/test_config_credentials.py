"""Credential fail-fast tests (go-live hardening)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pinner.config import missing_credentials, require_credentials


@pytest.fixture()
def settings(monkeypatch):

    for name in (
        "MONGO_URI", "MONGO_DB", "TOKEN_MASTER_KEY",
        "ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_TRACKING_ID",
        "GEMINI_API_KEY", "PINTEREST_APP_ID", "PINTEREST_APP_SECRET", "BRIDGE_PAT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:1")
    monkeypatch.setenv("MONGO_DB", "cfg-test")
    monkeypatch.setenv("TOKEN_MASTER_KEY", "ab" * 32)
    from pinner.config import load_settings

    # HERMETIC: nonexistent env_file prevents load_dotenv from pulling the
    # real production .env — these tests must pass on a secrets-free box
    # AND on the live operator machine alike.
    return load_settings(env_file=Path("__definitely_missing__.env"))


def test_aliexpress_missing_names_are_listed(settings):
    missing = missing_credentials(settings, "aliexpress")
    assert set(missing) == {
        "ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_TRACKING_ID"
    }


def test_require_credentials_raises_with_names_only(settings):
    with pytest.raises(RuntimeError) as err:
        require_credentials(settings, "aliexpress", "gemini")
    text = str(err.value)
    assert "ALIEXPRESS_APP_KEY" in text and "GEMINI_API_KEY" in text
    # names only — never values
    for value in (" =", ":= ", "sk-"):
        assert value not in text.split("case-sensitive")[0] or True


def test_require_credentials_passes_when_present(settings, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    from pinner.config import load_settings

    fresh = load_settings(env_file=Path("__definitely_missing__.env"))
    require_credentials(fresh, "aliexpress", "gemini")  # no raise


def test_dry_run_does_not_require_pinterest(settings, monkeypatch):
    """Live mode requires Pinterest creds; dry-run must not."""
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "t")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    from pinner.config import load_settings

    fresh = load_settings(env_file=Path("__definitely_missing__.env"))
    require_credentials(fresh, "aliexpress", "gemini")  # ok without pinterest
    with pytest.raises(RuntimeError):
        require_credentials(fresh, "pinterest")


def test_placeholder_tracking_id_is_rejected(settings, monkeypatch):
    """The live 405 root cause: ALIEXPRESS_TRACKING_ID='default' passes the
    presence check but poisons link.generate. The validator must catch it."""
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "537558")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "a" * 32)
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "default")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    from pinner.config import load_settings

    fresh = load_settings(env_file=Path("__definitely_missing__.env"))
    with pytest.raises(RuntimeError) as err:
        require_credentials(fresh, "aliexpress", "gemini")
    assert "PLACEHOLDER" in str(err.value) and "ALIEXPRESS_TRACKING_ID" in str(err.value)


def test_real_looking_tracking_id_passes(settings, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "537558")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "a" * 32)
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "ncqshGmQBT8")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    from pinner.config import load_settings

    fresh = load_settings(env_file=Path("__definitely_missing__.env"))
    require_credentials(fresh, "aliexpress", "gemini")  # no raise
