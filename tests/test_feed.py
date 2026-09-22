from pathlib import Path

import pytest

from mlaw.feed import FeedEvent, parse_feed, record_feed, resolve_refresh_chapters, summarize

SAMPLE = Path(__file__).parent / "data" / "mcl_update_feed_sample.xml"


def _act_event(act_citation: str, kind: str = "act_deleted") -> FeedEvent:
    return FeedEvent(f"Section Act.x has been deleted", kind, None, act_citation, f"mcl-{act_citation}",
                     None, "", None, "2026-09-21T00:00:00Z")


def _section_event(citation: str, kind: str = "added") -> FeedEvent:
    return FeedEvent(f"Section x - {kind}", kind, citation, None, None, None, "", None, "2026-09-21T00:00:00Z")


@pytest.fixture
def feed():
    return parse_feed(SAMPLE.read_bytes())


def test_channel_and_counts(feed):
    assert feed.title == "Michigan Legislature Bill Updates"
    assert feed.built_at == "2026-09-02T23:55:57Z"
    s = summarize(feed)
    assert s["items"] == 14
    assert s["by_kind"] == {"added": 12, "act_deleted": 2}
    assert s["chapters_touched"] == {"333": 12}
    assert s["act_level_events"] == ["2002 PA 687", "1978 PA 368"]
    assert s["unrecognized_titles"] == []


def test_section_event_fields(feed):
    e = feed.events[0]
    assert (e.kind, e.citation) == ("added", "MCL 333.1011")
    assert e.object_name == "mcl-333-1011"
    assert e.url == "https://legislature.mi.gov/Home/GetObject?objectName=mcl-333-1011"
    assert e.catchline_hint == "State plan for poison control center network; establishment."  # boilerplate stripped
    assert e.published == "2026-09-02T23:55:57Z"


def test_status_hint_and_curly_quotes(feed):
    expired = next(e for e in feed.events if e.citation == "MCL 333.1211")
    assert expired.status_hint == "expired" and expired.catchline_hint.startswith("Expired. 1978, Act 368")
    q = next(e for e in feed.events if e.citation == "MCL 333.2202")
    assert "“administrative experience” defined." in q.catchline_hint


def test_act_level_event(feed):
    e = next(e for e in feed.events if e.act_citation == "1978 PA 368")
    assert e.kind == "act_deleted" and e.citation is None and e.url is None
    assert e.object_name == "mcl-Act-368-of-1978"


def test_unknown_shapes_are_kept_not_fatal():
    xml = """<rss><channel><title>t</title><lastBuildDate>Wed, 02 Sep 2026 23:55:57 GMT</lastBuildDate>
    <item><title>Section 750.83 - amended</title><guid>https://x/Home/GetObject?objectName=mcl-750-83</guid><description>Assault.</description></item>
    <item><title>Chapter 750 was rebuilt</title><guid>abc</guid><description>x</description></item>
    <item><title>Section Const-Article-IV-27 - added</title><guid>g</guid><description>x</description></item>
    </channel></rss>"""
    f = parse_feed(xml)
    assert [(e.kind, e.citation) for e in f.events] == [
        ("amended", "MCL 750.83"), ("unrecognized", None), ("unrecognized", None)]
    assert summarize(f)["unrecognized_titles"] == ["Chapter 750 was rebuilt", "Section Const-Article-IV-27 - added"]


def test_not_rss_rejected():
    with pytest.raises(ValueError):
        parse_feed("<html><body/></html>")


def test_record_feed_dedupes_and_lists_work(conn, feed):
    r1 = record_feed(conn, feed, "2026-09-21T10:00:00Z")
    assert r1["new_events"] == 14
    assert len(r1["refetch_sections"]) == 12 and r1["refetch_sections"][0] == "MCL 333.1011"
    assert r1["recrawl_acts"] == ["1978 PA 368", "2002 PA 687"]
    r2 = record_feed(conn, feed, "2026-09-21T10:05:00Z")  # same build polled again
    assert r2["new_events"] == 0 and r2["refetch_sections"] == []
    assert conn.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0] == 14


# ---- full-feed regression (real 2026-09-02 build: 2,410 items, all chapter 333 / 1978 PA 368 re-load) ----
import gzip

FULL = Path(__file__).parent / "data" / "mcl_update_feed_2026-09-02.xml.gz"


@pytest.fixture(scope="module")
def full():
    return parse_feed(gzip.open(FULL, "rt", encoding="utf-8").read())


def test_full_feed_every_item_classified(full):
    s = summarize(full)
    assert s["items"] == 2410
    assert s["unrecognized_titles"] == []
    assert s["by_kind"] == {
        "added": 2209, "heading_added": 137, "act_deleted": 29,
        "ero_deleted": 27, "heading_deleted": 5, "initiated_law_deleted": 2, "deleted": 1,
    }
    assert sum(s["by_kind"].values()) == 2410
    assert len(s["act_level_events"]) == 29
    assert "1978 PA 368" in s["acts_touched"]


def test_full_feed_shape_details(full):
    # heading nodes carry their act, derived from the object name
    h = next(e for e in full.events if e.raw_title == "BASIC HEALTH SERVICES - added")
    assert (h.kind, h.act_citation, h.object_name, h.catchline_hint) == (
        "heading_added", "1978 PA 368", "mcl-368-1978-2-23", "BASIC HEALTH SERVICES")
    # variant nodes are kept apart from the plain section
    v = {(e.citation, e.variant) for e in full.events if e.variant}
    assert ("MCL 333.16188", "added") in v and ("MCL 333.16335", "amended") in v and ("MCL 333.5474c", "[1]") in v
    assert len(v) == 26
    # a compilers' typo in an id is preserved, not "fixed"
    typo = next(e for e in full.events if e.raw_title == "Section 333.17801.amaended has been deleted")
    assert (typo.citation, typo.variant, typo.kind) == ("MCL 333.17801", "amaended", "deleted")
    # repealed / expired status shows up only in the catchline hint
    assert sum(1 for e in full.events if e.status_hint == "repealed") == 109
    assert sum(1 for e in full.events if e.status_hint == "expired") == 6
    # every item in the build shares one timestamp
    assert {e.published for e in full.events} == {"2026-09-02T23:55:57Z"}


def test_full_feed_recorded_work_lists(conn, full):
    r = record_feed(conn, full, "2026-09-21T10:00:00Z")
    assert r["new_events"] == 2410
    assert "1978 PA 368" in r["recrawl_acts"] and len(r["recrawl_acts"]) == 29
    assert "mcl-333-16188-added" in r["refetch_objects"]  # variants fetched by object name
    # every one of the 2,209 section-level events names chapter 333 directly, so the plan resolves to
    # exactly that chapter even against an empty DB (no prior ingest to look act-level events up against)
    assert r["refresh_chapters"] == ["333"]
    assert record_feed(conn, full, "2026-09-21T10:05:00Z")["new_events"] == 0


# ---- resolve_refresh_chapters: feed events -> the chapters a refresh actually needs to re-fetch --------
def test_resolve_refresh_chapters_from_section_citations_needs_no_db_lookup(conn):
    chapters, unmapped = resolve_refresh_chapters(conn, [_section_event("MCL 333.1011"), _section_event("MCL 750.83")])
    assert chapters == ["333", "750"] and unmapped == []


def test_resolve_refresh_chapters_looks_up_documents_table_for_act_events(conn):
    """An act-level event (no citation of its own) is resolved from what's already stored -- an active
    act's sections are in ``documents``."""
    conn.execute("INSERT INTO documents (source, citation, chapter, act_citation, first_seen)"
                 " VALUES ('mcl', 'MCL 333.1011', '333', '1978 PA 368', '2026-09-01T00:00:00Z')")
    chapters, unmapped = resolve_refresh_chapters(conn, [_act_event("1978 PA 368")])
    assert chapters == ["333"] and unmapped == []


def test_resolve_refresh_chapters_looks_up_ranges_table_for_fully_repealed_acts(conn):
    """A fully repealed/expired act has no live section at all -- it's stored only as a ``ranges`` stub,
    never a ``documents`` row (confirmed against the real feed sample: see resolve_refresh_chapters's own
    docstring). The chapter lookup has to check ``ranges`` too or these silently fall through as unmapped."""
    conn.execute("INSERT INTO ranges (source, start_citation, end_citation, chapter, act_citation, note, first_seen)"
                 " VALUES ('mcl', 'MCL 333.1001', 'MCL 333.1007', '333', '1978 PA 443', 'x', '2026-09-01T00:00:00Z')")
    chapters, unmapped = resolve_refresh_chapters(conn, [_act_event("1978 PA 443")])
    assert chapters == ["333"] and unmapped == []


def test_resolve_refresh_chapters_reports_unmapped_when_act_unknown(conn):
    """An act named by the feed with nothing on file yet (never ingested, or a brand-new act with no
    companion section event in the same build) can't be resolved -- reported separately, never guessed."""
    chapters, unmapped = resolve_refresh_chapters(conn, [_act_event("2026 PA 1")])
    assert chapters == [] and unmapped == ["2026 PA 1"]


def test_resolve_refresh_chapters_multichapter_act_resolves_to_every_chapter_it_touches(conn):
    """An act spanning many chapters (a MultiChapter file -- see mclxml.py, e.g. Chapter 760.xml's Code of
    Criminal Procedure) resolves an act-level event to every chapter it currently occupies, not just one."""
    for cite, chapter in [("MCL 760.1", "760"), ("MCL 761.1", "761"), ("MCL 767A.1", "767A")]:
        conn.execute("INSERT INTO documents (source, citation, chapter, act_citation, first_seen)"
                     " VALUES ('mcl', ?, ?, '1927 PA 175', '2026-09-01T00:00:00Z')", (cite, chapter))
    chapters, unmapped = resolve_refresh_chapters(conn, [_act_event("1927 PA 175")])
    assert chapters == ["760", "761", "767A"] and unmapped == []


def test_heading_node_vs_variant_disambiguation():
    xml = """<rss><channel><title>t</title><lastBuildDate>Wed, 02 Sep 2026 23:55:57 GMT</lastBuildDate>
    <item><title>Section 175.1927.II has been deleted</title><guid>mcl-175-1927-II</guid><description> </description></item>
    <item><title>Section 333.2026.amended - added</title><guid>https://x/Home/GetObject?objectName=mcl-333-2026-amended</guid><description>Powers.</description></item>
    <item><title>Section 767A.1 - added</title><guid>https://x/Home/GetObject?objectName=mcl-767A-1</guid><description>Definitions.</description></item>
    </channel></rss>"""
    e = parse_feed(xml).events
    assert (e[0].kind, e[0].act_citation, e[0].citation) == ("heading_deleted", "1927 PA 175", None)
    assert (e[1].kind, e[1].citation, e[1].variant) == ("added", "MCL 333.2026", "amended")
    assert (e[2].kind, e[2].citation) == ("added", "MCL 767A.1")
