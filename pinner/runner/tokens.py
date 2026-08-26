"""Pinterest OAuth token store: encrypted at rest, rotated on refresh.

Bridges oauth_tokens (WP6 envelopes) with the Pinterest refresh flow: when a
call comes back 401 the runner refreshes here, persists the ROTATED refresh
token, and retries once with the new access token."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pinner.crypto import tokens as crypto
from pinner.tools.http import Transport
from pinner.tools.pinterest import refresh_access_token


class TokenStore:
    def __init__(
        self,
        db,
        master_key: bytes,
        *,
        app_id: str,
        app_secret: str,
        transport: Transport | None = None,
    ) -> None:
        self._db = db
        self._key = master_key
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport = transport

    def access_token(self, account_id: str) -> str:
        doc = self._db.oauth_tokens.find_one({"account_id": account_id})
        if doc is None:
            raise KeyError(f"no oauth token stored for account {account_id}")
        return crypto.decrypt_token(self._key, doc["encrypted_blob"])

    def refresh(self, account_id: str, *, now: datetime | None = None) -> str:
        """Exchange the stored refresh token; persist the rotated pair."""
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        current = self.access_token(account_id)  # stored value IS the refresh token
        result = refresh_access_token(
            self._app_id, self._app_secret, current, transport=self._transport
        )
        expires_in = result.get("expires_in") or 2_592_000
        blob = crypto.encrypt_token(self._key, result["refresh_token"] or current)
        self._db.oauth_tokens.update_one(
            {"account_id": account_id},
            {
                "$set": {
                    "encrypted_blob": blob,
                    "last_refreshed_at": now,
                    "expires_at": now + timedelta(seconds=expires_in),
                }
            },
            upsert=True,
        )
        return result["access_token"]
