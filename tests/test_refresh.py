"""Feed-driven refresh end to end: a feed build in, only the affected chapters re-ingested, proven against
the real 2026-09-02 feed sample (see tests/test_feed.py) and small synthetic chapter files."""

import gzip
import html
import os
from pathlib import Path

import httpx
import pytest

from mlaw import service
from mlaw.pipeline import run_ingest
from mlaw.refresh import RefreshPlan, plan_refresh, refresh_from_files, refresh_from_web
from mlaw.sources.mcl_web import MclXmlFilesSource
from mlaw.web import PoliteClient

DATA = Path(__file__).parent / "data"


def _section_xml(number: str, catchline: str, body_inner: str) -> str:
    """Same minimal shape as tests/test_mclxml.py's helper of the same name (duplicated rather than
    imported: tests/ isn't a package, so cross-test-module imports don't work here)."""
    body = html.escape(f"<Section-Body><Section-Number>Sec. 1.</Section-Number>{body_inner}</Section-Body>")
    return f"""
    <MCLSectionInfo>
      <MCLNumber>{number}</MCLNumber>
      <SectRef>{number}</SectRef>
      <Label>1</Label>
      <CatchLine>{catchline}</CatchLine>
      <Repealed>false</Repealed>
      <HistoryText></HistoryText>
      <EditorsNotes />
      <Commentary />
      <BodyText>{body}</BodyText>
    </MCLSectionInfo>"""


def _chapter_xml(name: str, title: str, multi_chapter: bool, sections_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-16"?>
<MCLChapterInfo>
  <DocumentID>1</DocumentID>
  <Repealed>false</Repealed>
  <EditorsNotes />
  <Commentary />
  <History />
  <Name>{name}</Name>
  <Title>{title}</Title>
  <MultiChapter>{str(multi_chapter).lower()}</MultiChapter>
  <FirstSectionNumber />
  <LastSectionNumber />
  <MCLDocumentInfoCollection>
    <MCLStatuteInfo>
      <DocumentID>2</DocumentID>
      <Repealed>false</Repealed>
      <EditorsNotes />
      <Commentary />
      <HistoryText></HistoryText>
      <Name>Act 1 of 2000</Name>
      <Heading>Test Act</Heading>
      <LongTitle>AN ACT to test.</LongTitle>
      <ShortTitle></ShortTitle>
      <StyleClause></StyleClause>
      <MCLDocumentInfoCollection>
        {sections_xml}
      </MCLDocumentInfoCollection>
    </MCLStatuteInfo>
  </MCLDocumentInfoCollection>
</MCLChapterInfo>""".encode("utf-16")
XML37 = (DATA / "xml" / "chapter-37.xml").read_bytes()
FULL_FEED = gzip.open(DATA / "mcl_update_feed_2026-09-02.xml.gz", "rb").read()
T1 = "2026-09-21T19:00:00Z"


@pytest.fixture
def chapters_dir(tmp_path):
    (tmp_path / "Chapter 37.xml").write_bytes(XML37)
    return tmp_path


def test_plan_refresh_is_idempotent_on_a_repeat_poll(conn):
    p1 = plan_refresh(conn, FULL_FEED, now=T1)
    assert p1.new_events == 2410 and p1.chapters == ["333"]
    p2 = plan_refresh(conn, FULL_FEED, now="2026-09-21T19:05:00Z")  # same build polled again
    assert p2.new_events == 0 and p2.chapters == []


def test_refresh_from_files_ingests_only_the_planned_chapters(conn, chapters_dir):
    other_chapter = _chapter_xml("2", "OTHER", False, _section_xml("2.1", "Untouched.", "<Paragraph><P>Text.</P></Paragraph>"))
    (chapters_dir / "Chapter 2.xml").write_bytes(other_chapter)

    plan = RefreshPlan(chapters=["37"], unmapped_acts=[], new_events=1)
    report = refresh_from_files(conn, plan, chapters_dir, now=T1)
    assert report is not None and report.docs_seen == 142  # Chapter 37 only, not Chapter 2's section too
    assert service.get_document(conn, "MCL 37.2202")["act_citation"] == "1976 PA 453"
    with pytest.raises(ValueError, match="not in this database"):
        service.get_document(conn, "MCL 2.1")


def test_refresh_from_files_is_a_noop_when_the_plan_names_no_chapters(conn, chapters_dir):
    plan = RefreshPlan(chapters=[], unmapped_acts=["2026 PA 1"], new_events=1)
    assert refresh_from_files(conn, plan, chapters_dir, now=T1) is None
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


# ---- refresh_from_web, the live equivalent, against a mock server (same shape as MclXmlWebSource's own
# mocked-server tests in tests/test_mclxml.py -- duplicated here rather than imported, since tests/ isn't a
# package) -----------------------------------------------------------------------------------------------
def _mock_client(handler):
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    return PoliteClient(transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: t["now"],
                        user_agent="test/1.0", min_interval=1.0)


def test_refresh_from_web_ingests_only_the_planned_chapters(conn):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/documents/mcl/Chapter 37.xml":
            return httpx.Response(200, content=XML37)
        return httpx.Response(404)  # e.g. Chapter 2.xml -- proves only the planned chapter is fetched

    plan = RefreshPlan(chapters=["37"], unmapped_acts=[], new_events=1)
    report = refresh_from_web(conn, plan, client=_mock_client(handler), now=T1)
    assert report is not None and report.docs_seen == 142
    assert service.get_document(conn, "MCL 37.2202")["act_citation"] == "1976 PA 453"


def test_refresh_from_web_is_a_noop_when_the_plan_names_no_chapters(conn):
    plan = RefreshPlan(chapters=[], unmapped_acts=["2026 PA 1"], new_events=1)
    # No client is given at all, and none is built: refresh_from_web must return before ever constructing
    # MclXmlWebSource, so a poll with nothing new to refresh makes no network request whatsoever.
    assert refresh_from_web(conn, plan, now=T1) is None
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_real_feed_against_real_chapter_333_resolves_with_no_unmapped_acts(conn):
    """The real regression that motivated checking `ranges` too (see resolve_refresh_chapters's own
    docstring): seed the DB the way an initial full crawl would (a real Chapter 333.xml ingested once),
    then poll the real 2026-09-02 MCLupdate.xml build. Every one of its 29 act-level "deleted" events --
    including the 5 that name a fully repealed act with no live section, only a ranges stub -- must
    resolve to chapter 333, not fall through as unmapped.

    Skipped unless MLAW_TEST_XML_DIR points at a directory holding a real Chapter 333.xml (too large,
    481 MB for the full MCL, to commit as a fixture) -- set it to re-run this against your own saved copy,
    e.g. MLAW_TEST_XML_DIR=~/Desktop/MCL pytest tests/test_refresh.py -k real_feed."""
    d = os.environ.get("MLAW_TEST_XML_DIR")
    real_333 = Path(d).expanduser() / "Chapter 333.xml" if d else None
    if not real_333 or not real_333.exists():
        pytest.skip("set MLAW_TEST_XML_DIR to a directory holding a real Chapter 333.xml to run this")
    run_ingest(conn, MclXmlFilesSource(real_333), now="2026-09-01T00:00:00Z")
    plan = plan_refresh(conn, FULL_FEED, now="2026-09-02T23:56:00Z")
    assert plan.chapters == ["333"]
    assert plan.unmapped_acts == []
