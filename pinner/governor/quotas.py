"""Quota governor — WP5 contract (implemented next). PURE logic, no I/O.

Contract:

    decide(account_doc, pins_today, now, gemini_calls_today) -> Decision

    Decision = {allowed: bool, reason: str}

Rules (encoded as data, all testable without Mongo):
    * WARMUP accounts: day 1-14 -> 2 pins/day; day 15-30 -> 5/day; then ACTIVE.
    * ACTIVE accounts: pins_daily_cap from account.quotas.
    * Minimum spacing between pins per account (default 20 min, jittered).
    * Global Gemini RPD budget guard (free tier) — deny before burning the call.
The governor is the ONLY component allowed to say "not now"; it never
executes anything itself.
"""
