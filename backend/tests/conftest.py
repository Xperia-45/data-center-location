"""
Shared test fixtures.

LABELLING POLICY (enforced in test names and docstrings):
  * `synthetic_payload` - a locally generated stand-in for an upstream response
    (fake Overpass JSON, a synthetic WorldCover-shaped COG, a formula-based DEM).
    These are NEVER presented as live upstream data.
  * `fixture_only`      - pure logic on in-test data, no HTTP involved.
  * `live`              - real upstream requests; deselected by default because the
    CI sandbox has no outbound internet.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db import conn  # noqa: E402
from app.services import elevation as elev_service  # noqa: E402
from app.services import landscape as landscape_service  # noqa: E402
from app.services import osm as osm_service  # noqa: E402


@pytest.fixture(autouse=True)
def _no_overpass_pacing(monkeypatch):
    """
    Neutralise production request pacing so the suite does not sleep.

    The real defaults (a per-request stagger plus bounded exponential backoff) exist to keep a
    deployed service inside Overpass's fair-use limits. Paying them in every test added ~2
    minutes of pure sleeping to the suite and asserts nothing. Tests that exercise pacing or
    backoff behaviour set these values explicitly.
    """
    monkeypatch.setattr(osm_service, "OVERPASS_REQUEST_DELAY_SEC", 0.0, raising=False)
    monkeypatch.setattr(osm_service, "OVERPASS_BACKOFF_BASE_SEC", 0.0, raising=False)
    yield


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    """
    Every test gets its own SQLite file so cached geography can never leak between
    tests (and a test can never accidentally read another site's cached layer).
    """
    db_path = tmp_path / "platform_test.db"
    monkeypatch.setenv("DATABASE_URL_SQLITE", str(db_path))
    from app.db import init_db

    init_db()
    elev_service.clear_elevation_memory_cache()
    landscape_service.clear_landscape_cache()
    yield
    elev_service.clear_elevation_memory_cache()
    landscape_service.clear_landscape_cache()


@pytest.fixture
def clear_sqlite_caches():
    def _clear():
        elev_service.clear_elevation_memory_cache()
        landscape_service.clear_landscape_cache()
        db = conn()
        for table in ("elevation_cache", "landscape_cache"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.close()

    return _clear
