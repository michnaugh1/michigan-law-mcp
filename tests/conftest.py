from pathlib import Path

import pytest

from mlaw.db import connect, init_db
from mlaw.pipeline import run_ingest
from mlaw.sources.fixture import FixtureSource

FIX = Path(__file__).resolve().parent.parent / "fixtures"
DAY1 = "2026-09-20T06:00:00Z"
DAY2 = "2026-09-21T06:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite3")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def loaded(conn):
    """DB after day 1 and day 2 fixture ingests."""
    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    run_ingest(conn, FixtureSource(FIX / "day2"), removal_guard=0.5, now=DAY2)
    return conn
