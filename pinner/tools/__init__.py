"""Side-effect tools (Phase 2): GitHub bridge committer + Pinterest v5 client.

Both share the typed HTTP transport seam (tools/http.py) — CI tests run on
fakes and never touch the network. Errors flow through pinner.errors so the
runner maps them uniformly to the state machine's failure classes."""
