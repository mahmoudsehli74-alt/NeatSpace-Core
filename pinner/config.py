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
    github_bridge_pat: str = ""
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
        github_bridge_pat=os.environ.get("GITHUB_BRIDGE_PAT", ""),
        pinterest_app_id=os.environ.get("PINTEREST_APP_ID", ""),
        pinterest_app_secret=os.environ.get("PINTEREST_APP_SECRET", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
