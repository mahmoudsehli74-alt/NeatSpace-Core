"""Environment configuration contract (WP1).

All secrets enter via environment variables: .env locally (gitignored),
GitHub Actions Secrets in CI/production. Nothing secret is ever hardcoded
or stored in Mongo in plaintext (OAuth tokens are AES-GCM encrypted at the
application layer; see pinner/crypto/tokens.py, WP6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a hard dependency, guard anyway
    _load_dotenv = None  # type: ignore[assignment]

REQUIRED_KEYS = ("MONGO_URI", "MONGO_DB", "TOKEN_MASTER_KEY")


# Credentials each integration requires. Names are logged on failure;
# VALUES never are.
CREDENTIAL_REQUIREMENTS = {
    "aliexpress": ("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_TRACKING_ID"),
    "gemini": ("GEMINI_API_KEY",),
    "pinterest": ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
    "bridge": ("BRIDGE_PAT",),
}


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_db: str
    token_master_key: str

    # Populated from Phase 2 onward — kept here so the contract is frozen once.
    gemini_api_key: str = ""
    aliexpress_app_key: str = ""
    aliexpress_app_secret: str = ""
    aliexpress_tracking_id: str = ""
    bridge_pat: str = ""
    pinterest_app_id: str = ""
    pinterest_app_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def _load_env_file(explicit: Path | None) -> None:
    if explicit is not None:
        if _load_dotenv is not None:
            _load_dotenv(explicit)
        return
    if _load_dotenv is None:
        return
    cwd_env = Path.cwd() / ".env"
    repo_env = Path(__file__).resolve().parents[1] / ".env"
    if cwd_env.exists():
        _load_dotenv(cwd_env)
    elif repo_env.exists():
        _load_dotenv(repo_env)


def missing_credentials(settings: Settings, feature: str) -> list[str]:
    """Env-var NAMES missing for a feature (checked by presence only)."""
    values = {
        "ALIEXPRESS_APP_KEY": settings.aliexpress_app_key,
        "ALIEXPRESS_APP_SECRET": settings.aliexpress_app_secret,
        "ALIEXPRESS_TRACKING_ID": settings.aliexpress_tracking_id,
        "GEMINI_API_KEY": settings.gemini_api_key,
        "PINTEREST_APP_ID": settings.pinterest_app_id,
        "PINTEREST_APP_SECRET": settings.pinterest_app_secret,
        "BRIDGE_PAT": settings.bridge_pat or os.environ.get("GITHUB_BRIDGE_PAT", ""),
    }
    return [name for name in CREDENTIAL_REQUIREMENTS.get(feature, ()) if not values.get(name)]


def require_credentials(settings: Settings, *features: str) -> None:
    """Fail fast BEFORE spending API calls when integration secrets are absent.

    Raises RuntimeError naming every missing env var (never their values), plus
    where to fix them. This is the antidote to opaque downstream errors like
    "biz error None: None" caused by ALIEXPRESS_APP_KEY="".
    """
    problems: dict[str, list[str]] = {}
    for feature in features:
        missing = missing_credentials(settings, feature)
        if missing:
            problems[feature] = missing
    if problems:
        lines = [
            f"{feature}: missing {', '.join(names)}"
            for feature, names in problems.items()
        ]
        raise RuntimeError(
            "missing required credentials:\n  "
            + "\n  ".join(lines)
            + "\nFix GitHub Secrets (or local .env); names are case-sensitive."
        )


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from the environment (optionally a .env file).

    Raises RuntimeError listing every missing required key — fail fast at
    startup, never mid-run.
    """
    _load_env_file(env_file)
    missing = [key for key in REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            f"missing required environment variables: {', '.join(missing)} "
            "(see .env.example)"
        )
    return Settings(
        mongo_uri=os.environ["MONGO_URI"],
        mongo_db=os.environ["MONGO_DB"],
        token_master_key=os.environ["TOKEN_MASTER_KEY"],
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        aliexpress_app_key=os.environ.get("ALIEXPRESS_APP_KEY", ""),
        aliexpress_app_secret=os.environ.get("ALIEXPRESS_APP_SECRET", ""),
        aliexpress_tracking_id=os.environ.get("ALIEXPRESS_TRACKING_ID", ""),
        # GitHub forbids secret names starting with GITHUB_; BRIDGE_PAT is canonical.
        bridge_pat=os.environ.get("BRIDGE_PAT") or os.environ.get("GITHUB_BRIDGE_PAT", ""),
        pinterest_app_id=os.environ.get("PINTEREST_APP_ID", ""),
        pinterest_app_secret=os.environ.get("PINTEREST_APP_SECRET", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
