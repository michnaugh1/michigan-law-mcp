import pytest

from conftest import DAY1, DAY2, FIX
from mlaw.pipeline import PartialCrawlError, run_ingest
from mlaw.sources.fixture import FixtureSource


def count(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def test_first_ingest_all_new(conn):
    r = run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    assert (r.docs_seen, r.docs_new, r.docs_modified, r.docs_removed) == (6, 6, 0, 0)
    assert count(conn, "SELECT COUNT(*) FROM versions") == 6
    assert count(conn, "SELECT COUNT(*) FROM fts") == 6


def test_rerun_same_data_creates_no_versions(conn):
    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    r = run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY2)
    assert (r.docs_new, r.docs_modified, r.docs_removed) == (0, 0, 0)
    assert count(conn, "SELECT COUNT(*) FROM versions") == 6
    # but last_seen moved forward
    assert count(conn, "SELECT COUNT(*) FROM versions WHERE last_seen = ?", DAY2) == 6


def test_day2_modify_add_remove(loaded):
    conn = loaded
    r = conn.execute("SELECT * FROM runs WHERE id = 2").fetchone()
    assert (r["docs_new"], r["docs_modified"], r["docs_removed"]) == (1, 1, 1)
    assert count(conn, "SELECT COUNT(*) FROM versions v JOIN documents d ON d.id=v.document_id WHERE d.citation='MCL 750.83'") == 2
    assert count(conn, "SELECT is_current FROM versions v JOIN documents d ON d.id=v.document_id WHERE d.citation='MCL 750.83' ORDER BY v.id LIMIT 1") == 0
    assert count(conn, "SELECT active FROM documents WHERE citation='MCL 750.100'") == 0
    # removed docs leave the search index; new docs enter it
    assert count(conn, "SELECT COUNT(*) FROM fts WHERE citation='MCL 750.100'") == 0
    assert count(conn, "SELECT COUNT(*) FROM fts WHERE citation='MCL 750.85'") == 1


def test_removal_guard_aborts_and_rolls_back(conn):
    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    partial = FixtureSource(FIX / "day1")
    partial._docs = partial._docs[:2]  # a "complete" crawl that only saw 2 of 6 documents
    with pytest.raises(PartialCrawlError):
        run_ingest(conn, partial, now=DAY2)
    # nothing was marked removed, and the failure is on the run record
    assert count(conn, "SELECT COUNT(*) FROM documents WHERE active = 0") == 0
    run = conn.execute("SELECT status, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed" and "refusing" in run["error"]
    assert count(conn, "SELECT COUNT(*) FROM fts") == 6


def test_incomplete_source_never_removes(conn):
    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    partial = FixtureSource(FIX / "day1")
    partial._docs = partial._docs[:2]
    partial.complete = False
    r = run_ingest(conn, partial, now=DAY2)
    assert r.docs_removed == 0 and count(conn, "SELECT COUNT(*) FROM documents WHERE active = 0") == 0


def test_removed_then_restored(conn):
    run_ingest(conn, FixtureSource(FIX / "day1"), now=DAY1)
    run_ingest(conn, FixtureSource(FIX / "day2"), removal_guard=0.5, now=DAY2)
    r = run_ingest(conn, FixtureSource(FIX / "day1"), removal_guard=0.5, now="2026-09-22T06:00:00Z")
    # 750.100 returns with identical text -> restored; 750.83 reverts (new version); 750.85 removed
    assert r.docs_restored == 1
    assert count(conn, "SELECT active FROM documents WHERE citation='MCL 750.100'") == 1
    assert count(conn, "SELECT COUNT(*) FROM fts WHERE citation='MCL 750.100'") == 1


def test_bad_citation_from_parser_is_loud(conn):
    src = FixtureSource(FIX / "day1")
    src._docs[0]["citation"] = "750.83"  # not canonical
    with pytest.raises(ValueError, match="non-canonical"):
        run_ingest(conn, src, now=DAY1)
    assert count(conn, "SELECT COUNT(*) FROM documents") == 0  # rolled back


def test_xrefs_extracted_and_public_acts(loaded):
    conn = loaded
    rows = conn.execute(
        "SELECT x.to_kind, x.to_key, x.in_range FROM xrefs x JOIN documents d ON d.id=x.from_document_id"
        " WHERE d.citation='MCL 750.84'"
    ).fetchall()
    got = {(r[0], r[1], bool(r[2])) for r in rows}
    assert ("mcl", "MCL 750.83", False) in got
    assert ("mcl", "MCL 750.520b", True) in got
    assert ("public_act", "1931 PA 328", False) in got
    assert ("mcr", "MCR 6.110", False) in got
    assert count(conn, "SELECT COUNT(*) FROM public_act_targets WHERE year=2026 AND number=50") == 2
