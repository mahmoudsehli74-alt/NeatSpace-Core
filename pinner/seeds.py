"""Seed data + idempotent seeding (WP7).

Adding your 20th account later = adding one tuple to ACCOUNTS (or calling
``seed_one_account``) and re-running scripts/seed.py. Re-seeding is SAFE:
existing accounts keep their status, warm-up progress, and stats
($setOnInsert for lifecycle fields; $set only for deterministic config).
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_QUOTAS = {
    "pins_daily_cap": 10,
    "min_pin_interval_min": 20,
    "max_products_per_run": 10,
}

NICHES = [
    {
        "name": "kitchen",
        "tone_guidelines": (
            "Warm, practical, homey voice. Lead with the everyday problem the "
            "product solves (counter clutter, meal prep time). Budget-luxury "
            "angle: 'looks expensive, costs less'."
        ),
        "board_keywords": ["kitchen organization", "kitchen decor", "meal prep", "storage ideas"],
        "banned_topics": ["knives", "adult", "weapons"],
        "content_style": {"hashtag_count_range": [3, 6], "title_style": "sentence",
                          "emoji_policy": "sparse"},
    },
    {
        "name": "aesthetics",
        "tone_guidelines": (
            "Aspirational, visual-first voice. Emphasize mood, palette, and "
            "transformation (before/after, room glow-up). Short evocative "
            "sentences; let the image carry the story."
        ),
        "board_keywords": ["room aesthetic", "home decor", "aesthetic bedroom", "interior inspo"],
        "banned_topics": ["adult", "weapons", "pharma"],
        "content_style": {"hashtag_count_range": [4, 8], "title_style": "lowercase",
                          "emoji_policy": "none"},
    },
    {
        "name": "selfcare",
        "tone_guidelines": (
            "Calm, supportive, wellness voice. Focus on rituals, rest, and "
            "small daily comforts. Never medical claims; never body-shaming language."
        ),
        "board_keywords": ["self care routine", "cozy night", "wellness tips", "morning ritual"],
        "banned_topics": ["adult", "supplements claims", "medical devices", "weapons"],
        "content_style": {"hashtag_count_range": [3, 6], "title_style": "sentence",
                          "emoji_policy": "moderate"},
    },
]

# (account name, niche, repo suffix)
ACCOUNTS = [
    ("NeatSpace Kitchen", "kitchen", "neatspace-kitchen"),
    ("NeatSpace Aesthetics", "aesthetics", "neatspace-aesthetics"),
    ("NeatSpace Selfcare", "selfcare", "neatspace-selfcare"),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def seed_niches(db, *, now: datetime | None = None) -> list[str]:
    """Upsert all niche docs by name. Returns the niche names."""
    now = now if now is not None else _utcnow()
    names = []
    for niche in NICHES:
        db.niches.update_one(
            {"name": niche["name"]},
            {"$set": {**niche, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        names.append(niche["name"])
    return names


def seed_one_account(
    db,
    *,
    name: str,
    niche: str,
    repo_full_name: str,
    quotas: dict | None = None,
    now: datetime | None = None,
) -> str:
    """Upsert one account by name, preserving lifecycle state on re-seed."""
    now = now if now is not None else _utcnow()
    niche_doc = db.niches.find_one({"name": niche})
    if niche_doc is None:
        raise ValueError(f"unknown niche {niche!r} — seed niches first")
    db.accounts.update_one(
        {"name": name},
        {
            "$set": {
                "niche_id": niche_doc["_id"],
                "quotas": quotas or dict(DEFAULT_QUOTAS),
                "site": {"repo_full_name": repo_full_name, "branch": "main", "cdn": "github_pages"},
                "boards_cache": [],
                "boards_fetched_at": None,
                "updated_at": now,
            },
            # Lifecycle fields are ONLY set on first insert — re-seeding must
            # never reset warm-up progress, status, or stats.
            "$setOnInsert": {
                "status": "WARMUP",
                "warmup": {"started_at": now},
                "stats": {"pins_today": 0, "pins_today_date": None, "last_pin_at": None},
                "created_at": now,
            },
        },
        upsert=True,
    )
    return name


def seed_accounts(db, *, github_user: str, now: datetime | None = None) -> dict:
    """Seed all niches + the three launch accounts. Idempotent."""
    niches = seed_niches(db, now=now)
    accounts = [
        seed_one_account(
            db, name=name, niche=niche, repo_full_name=f"{github_user}/{repo}", now=now
        )
        for name, niche, repo in ACCOUNTS
    ]
    return {"niches": niches, "accounts": accounts}
