"""Brand-safety blocklist — hardcoded, LLM-independent (the "fortress").

The Moderator LLM is good but probabilistic; this module is deterministic.
ANY product whose listing text — or any AI-generated copy — matches one of
these patterns is refused regardless of what any model says.

Current policy (operator directive 2026-09-14): the storefronts do not
carry religious-symbol merchandise. Patterns cover Jewish and Christian
symbol merch explicitly (Star of David, crosses/crucifixes, Hamsa,
menorahs, rosaries, amulets) plus the surrounding religious terminology.

Precision note: bare "cross" is deliberately NOT matched (cross-stitch
craft kits and crossbody bags are legitimate catalog items); religious
cross merchandise is caught by the paired phrases (wall cross, cross
necklace/pendant, crucifix, Jerusalem cross).

Word-boundary, case-insensitive. Extend RELIGIOUS_BLOCKLIST_PATTERNS as
new categories are identified; every match carries its pattern label for
the audit trail.
"""

from __future__ import annotations

import re

RELIGIOUS_BLOCKLIST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("star_of_david", r"\bstar\s+of\s+david\b"),
    ("crucifix", r"\bcrucifix(es)?\b"),
    ("cross_merch", r"\b(wall|altar|jerusalem|christian|catholic)\s+cross(es)?\b"),
    ("cross_jewelry", r"\bcross\s+(necklace|pendant|earring|charm|bracelet)s?\b"),
    ("hamsa", r"\bhamsa(s)?\b"),
    ("menorah", r"\bmenorahs?\b"),
    ("rosary", r"\brosar(y|ies)\b"),
    ("amulet", r"\bamulet(s)?\b"),
    ("talisman", r"\btalismans?\b"),
    ("judaica", r"\bjudaica\b"),
    ("religious_term", r"\b(christian|catholic|jewish|hebrew|torah|bible|biblical|gospel)\b"),
    ("religious_generic", r"\breligious\b"),
)

_COMPILED = [(label, re.compile(pat, re.IGNORECASE))
             for label, pat in RELIGIOUS_BLOCKLIST_PATTERNS]

PROTECTED_FIELDS = frozenset()  # placeholder for future category extensions


def scan(*texts: str | None) -> list[str]:
    """Return the sorted list of blocklist labels that match ANY of the
    given texts (None/empty allowed). Empty list = clean."""
    hits: set[str] = set()
    for text in texts:
        if not text:
            continue
        lowered = str(text)
        for label, pattern in _COMPILED:
            if pattern.search(lowered):
                hits.add(label)
    return sorted(hits)


def violations(*texts: str | None) -> list[str]:
    """Alias with the boolean-intent name used by call sites."""
    return scan(*texts)
