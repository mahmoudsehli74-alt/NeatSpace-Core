"""Domain-neutral error taxonomy for ALL external integrations.

The state machine consumes exactly two failure classes:
  * TransientError -> error_class="TRANSIENT" (retry with backoff)
  * PermanentError -> error_class="PERMANENT" (poison, don't burn quota)
Adapters and tools both derive from these so the runner maps exceptions
uniformly, regardless of which integration raised them."""

from __future__ import annotations


class TransientError(Exception):
    """Rate limits, 5xx, timeouts, token-expired — worth retrying."""


class PermanentError(Exception):
    """Bad params, auth failure, resource gone — retrying only burns quota."""
