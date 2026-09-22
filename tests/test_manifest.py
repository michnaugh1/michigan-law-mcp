from pathlib import Path

import pytest

from mlaw.manifest import chapter_files, parse_directory_listing, plan_fetch, record_parsed

DATA = Path(__file__).parent / "data" / "listings"
BASE = "https://legislature.mi.gov/documents/mcl/"


@pytest.fixture(scope="module")
def entries():
    return parse_directory_listing((DATA / "documents-mcl.html").read_text(encoding="utf-8"), BASE)


def test_listing_parses_files_and_directories(entries):
    dirs = sorted(e.name for e in entries if e.is_dir)
    assert dirs == ["archive", "pdf", "xml"]
    assert all(e.size is None for e in entries if e.is_dir)
    ch37 = next(e for e in entries if e.name == "Chapter 37.xml")
    assert ch37.size == 1176978 and ch37.modified == "2026-02-06T20:12"
    assert ch37.url == "https://legislature.mi.gov/documents/mcl/Chapter%2037.xml" and ch37.chapter == "37"


def test_chapter_files_are_numeric_ordered_and_complete(entries):
    cf = chapter_files(entries)
    assert len(cf) == 241
    nums = [e.chapter for e in cf]
    assert nums[:6] == ["1", "2", "3", "4", "5", "6"] and nums[-1] == "830"
    assert nums.index("8") < nums.index("10") < nums.index("115")
    assert "760" in nums and "764" not in nums  # the Code of Criminal Procedure file covers chapters 760-777
    assert round(sum(e.size for e in cf) / 1e6) == 480
    assert not any(e.name in ("CAUTION.tif", "web.config") for e in cf)


def test_archive_listing_is_years_plus_stray_files():
    es = parse_directory_listing((DATA / "documents-mcl-archive.html").read_text(encoding="utf-8"), BASE + "archive/")
    years = [e.name for e in es if e.is_dir and e.name.isdigit()]
    assert years[0] == "2013" and years[-1] == "2026" and len(years) == 14
    assert any(e.url.endswith("/archive/2026/") for e in es)


def test_plan_fetch_first_run_fetches_everything(conn, entries):
    cf = chapter_files(entries)
    plan = plan_fetch(conn, "mcl-xml", cf)
    assert len(plan.fetch) == 241 and plan.unchanged == [] and plan.vanished == []


def test_plan_fetch_skips_unchanged_and_flags_changes_and_vanished(conn, entries):
    cf = chapter_files(entries)
    for e in cf:
        record_parsed(conn, "mcl-xml", e, "abc123", "2026-09-21T20:00:00Z")
    conn.commit()
    assert plan_fetch(conn, "mcl-xml", cf).fetch == []

    # a chapter changes on the server (new mtime and size), another disappears from the listing
    changed = [e if e.name != "Chapter 333.xml" else type(e)(e.name, e.url, False, e.size + 10, "2026-09-22T08:00") for e in cf]
    changed = [e for e in changed if e.name != "Chapter 5.xml"]
    plan = plan_fetch(conn, "mcl-xml", changed)
    assert [e.name for e in plan.fetch] == ["Chapter 333.xml"]
    assert plan.vanished == ["Chapter 5.xml"]  # reported, never turned into a repeal
    assert len(plan.unchanged) == 239

    assert len(plan_fetch(conn, "mcl-xml", cf, force=True).fetch) == 241


def test_a_file_that_never_parsed_is_fetched_again(conn, entries):
    e = chapter_files(entries)[0]
    conn.executescript("CREATE TABLE IF NOT EXISTS source_files (source TEXT NOT NULL, name TEXT NOT NULL, size INTEGER,"
                       " modified TEXT, sha256 TEXT, fetched_at TEXT, PRIMARY KEY (source, name))")
    conn.execute("INSERT INTO source_files (source, name, size, modified) VALUES ('mcl-xml', ?, ?, ?)", (e.name, e.size, e.modified))
    assert [x.name for x in plan_fetch(conn, "mcl-xml", [e]).fetch] == [e.name]  # sha256 missing => last attempt failed
