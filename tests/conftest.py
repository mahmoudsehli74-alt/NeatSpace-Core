"""Shared test fixtures.

Mongo-backed tests need a reachable Mongo instance:
  * CI: a mongo:7 service container on localhost:27017 (MONGO_TEST_REQUIRE=1
    makes unreachability a hard failure so CI can never silently skip).
  * Local default: mongodb://localhost:27017 — with no local Mongo those tests
    SKIP automatically (the pure registry tests always run).
  * Against Atlas: set MONGO_TEST_URI to your cluster URI. Tests only ever
    touch MONGO_TEST_DB (default "affiliate-pinner-test") and DROP it before
    and after every test — never the production database.
"""

from __future__ import annotations

import os

import pytest
from pymongo import MongoClient

TEST_URI = os.environ.get("MONGO_TEST_URI", "mongodb://localhost:27017")
TEST_DB = os.environ.get("MONGO_TEST_DB", "affiliate-pinner-test")


def _mongo_up() -> bool:
    try:
        client = MongoClient(TEST_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


MONGO_UP = _mongo_up()

if os.environ.get("MONGO_TEST_REQUIRE") == "1" and not MONGO_UP:
    raise pytest.UsageError(f"MONGO_TEST_REQUIRE=1 but Mongo is unreachable at {TEST_URI}")


@pytest.fixture()
def db():
    """Fresh test database per test (dropped before and after)."""
    client = MongoClient(TEST_URI)
    client.drop_database(TEST_DB)
    yield client[TEST_DB]
    client.drop_database(TEST_DB)
    client.close()


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Auto-skip any test that needs the `db` fixture when Mongo is unreachable."""
    if MONGO_UP:
        return
    skip = pytest.mark.skip(reason=f"Mongo not reachable at {TEST_URI}")
    for item in items:
        if "db" in getattr(item, "fixturenames", []):
            item.add_marker(skip)
