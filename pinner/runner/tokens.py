"""Pinterest OAuth token store: dual-blob schema, encrypted at rest.

Stores BOTH tokens Pinterest issues (access ~30d, refresh longer-lived and
ROTATED on every refresh):
  { account_id, access_blob?, refresh_blob, last_refreshed_at, expires_at }

* ``access_token()`` prefers the stored access token; if only a refresh token
  exists it is returned as bearer-fallback — the first API call will 401 and
  the runner's refresh-and-retry path upgrades it automatically.
* ``refresh(account_id)`` exchanges the refresh token, persists the NEW
  rotated pair, and returns the fresh access token.

Store tokens via scripts/store_token.py (encrypts with TOKEN_MASTER_KEY)."""

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

    def _doc(self, account_id: str) -> dict | None:
        return self._db.oauth_tokens.find_one({"account_id": account_id})

    def access_token(self, account_id: str) -> str:
        doc = self._doc(account_id)
        if doc is None:
            raise KeyError(
                f"no oauth token stored for account {account_id} — "
                "run scripts/store_token.py"
            )
        if doc.get("access_blob"):
            return crypto.decrypt_token(self._key, doc["access_blob"])
        if doc.get("refresh_blob"):
            # Bearer-fallback: 401s on first use, triggering refresh+retry.
            return crypto.decrypt_token(self._key, doc["refresh_blob"])
        raise KeyError(f"token document for {account_id} has no usable blobs")

    def refresh(self, account_id: str, *, now: datetime | None = None) -> str:
        """Exchange the stored refresh token; persist the ROTATED pair."""
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        doc = self._doc(account_id)
        if doc is None or not doc.get("refresh_blob"):
            raise KeyError(
                f"no refresh token stored for account {account_id} — "
                "run scripts/store_token.py --refresh-token <token>"
            )
        current_refresh = crypto.decrypt_token(self._key, doc["refresh_blob"])
        result = refresh_access_token(
            self._app_id, self._app_secret, current_refresh, transport=self._transport
        )
        new_refresh = result.get("refresh_token") or current_refresh
        expires_in = result.get("expires_in") or 2_592_000
        self._db.oauth_tokens.update_one(
            {"account_id": account_id},
            {
                "$set": {
                    "access_blob": crypto.encrypt_token(self._key, result["access_token"]),
                    "refresh_blob": crypto.encrypt_token(self._key, new_refresh),
                    "last_refreshed_at": now,
                    "expires_at": now + timedelta(seconds=expires_in),
                }
            },
            upsert=True,
        )
        return result["access_token"]
