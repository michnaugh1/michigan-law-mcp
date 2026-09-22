"""Whole-chapter rendering (Chapter 37, saved from Home/RenderDoc), variants, tables, scoped ingest."""

import re
from pathlib import Path

import httpx
import pytest

from conftest import DAY1, DAY2, FIX
from mlaw import service
from mlaw.chapterdoc import parse_chapter_document, section_object_name
from mlaw.mclsite import parse_section_page, parse_section_wrapper
from mlaw.pipeline import PartialCrawlError, run_ingest
from mlaw.sources.fixture import FixtureSource
from mlaw.sources.mcl_web import ChapterIntegrityError, MclFilesSource, MclWebSource
from mlaw.web import PoliteClient

DATA = Path(__file__).parent / "data"
CH37 = (DATA / "chapters" / "chapter-37.html").read_text(encoding="utf-8")
T1, T2, T3 = "2026-09-21T19:00:00Z", "2026-09-22T06:00:00Z", "2026-09-23T06:00:00Z"


@pytest.fixture
def ch_dir(tmp_path):
    d = tmp_path / "chapters"
    d.mkdir()
    (d / "Chapter 37.html").write_text(CH37, encoding="utf-8")
    (d / "some-other-page.html").write_text("<html><body><h1>Daily Bill Status Report</h1></body></html>")
    return d


# ---- parsing --------------------------------------------------------------------------------------------
def test_chapter_37_parses_completely():
    cd = parse_chapter_document(CH37)
    assert (cd.chapter, cd.name) == ("37", "CIVIL RIGHTS")
    assert cd.currency == "Michigan Compiled Laws Complete Through PA 91 of 2026" and cd.through_act == "2026 PA 91"
    assert cd.rendered_on == "2026-09-21"
    assert cd.warnings == [] and cd.integrity_problems() == []
    assert (len(cd.statutes), len(cd.sections())) == (17, 142)
    by_status = {}
    for d in cd.docs():
        by_status[d.status] = by_status.get(d.status, 0) + 1
    assert by_status == {"active": 139, "repealed": 3}
    assert {d.variant for d in cd.docs()} == {""}


def test_acts_eros_and_divisions():
    cd = parse_chapter_document(CH37)
    labels = [(s.act_citation, s.kind, len(s.sections)) for s in cd.statutes]
    assert ("1976 PA 453", "act", 52) in labels and ("E.R.O. 1966-1", "ero", 1) in labels
    assert ("1963 PA 45 (2nd Ex. Sess.)", "act", 0) in labels  # wholly repealed act: only a range line
    el = next(s for s in cd.statutes if s.act_citation == "1976 PA 453")
    assert el.name == "ELLIOTT-LARSEN CIVIL RIGHTS ACT" and el.preamble.startswith("AN ACT to define civil rights")
    assert el.history.startswith("1976, Act 453, Eff. Mar. 31, 1977")
    assert {s.division_path for s in el.sections} >= {("Article 1",), ("Article 2",)}
    assert all(len(s.division_path) == 1 for s in el.sections)


def test_range_line_is_kept_as_a_range_not_dropped():
    cd = parse_chapter_document(CH37)
    (r,) = cd.ranges()
    assert (r.start_citation, r.end_citation, r.status) == ("MCL 37.1", "MCL 37.9", "repealed")
    assert r.note == "37.1-37.9 Repealed. 1976, Act 453, Eff. Mar. 31, 1977."


def test_section_details_notes_and_repealed():
    docs = {d.citation: d for d in parse_chapter_document(CH37).docs()}
    rep = docs["MCL 37.1207"]
    assert rep.status == "repealed" and rep.effective_date == "1981-01-20" and "exemptions" in rep.compilers_notes
    assert docs["MCL 37.2202"].text.startswith("Sec. 202.\n(1) An employer shall not do any of the following:")
    assert docs["MCL 37.2202"].url.endswith("objectName=mcl-37-2202")
    # Compiler's Notes, Admin Rule and Constitutionality notes all survive (the last matters to a lawyer)
    joined = "\n".join(d.compilers_notes for d in docs.values())
    assert "Admin Rule: R 37.27 et seq." in joined and "Constitutionality: Section 301(b)" in joined
    assert "Doe v Dep't of Corrections, 504 Mich 883 (2019)" in joined
    ero = docs["MCL 37.101"]
    assert ero.act_citation == "E.R.O. 1966-1" and ero.text.startswith("WHEREAS on September 21, 1966")


def test_object_names():
    assert section_object_name("MCL 750.145p") == "mcl-750-145p"
    assert section_object_name("MCL 767A.1") == "mcl-767A-1"
    assert section_object_name("MCL 333.16335", "amended") == "mcl-333-16335-amended"
    assert section_object_name("MCL 333.5474c", "[1]") == "mcl-333-5474c[1]"


# ---- variants and tables (Section 333.16335.amended, saved from the site) ------------------------------------
def test_variant_page_keeps_every_subsection_and_table():
    p = parse_section_page((DATA / "pages" / "section-333-16335-amended.html").read_text(encoding="utf-8"))
    assert p.variant == "amended" and p.citation == "MCL 333.16335" and p.catchline == "Physical therapy; fees."
    assert p.effective_date == "2028-01-22" and p.warnings == []
    assert "THIS AMENDED SECTION IS EFFECTIVE JANUARY 22, 2028" in p.banner
    lines = p.text.split("\n")
    assert "(a) Application processing fee | $ | 20.00" in lines and "(c) License fee, per year |  | 90.00" in lines
    # a loose text node after </table> used to be silently dropped
    assert lines[-1].startswith("(2) The fee for an individual seeking to hold a compact privilege")
    assert "Site notice: ***** 333.16335.amended" in p.compilers_notes and "Popular Name: Act 368" in p.compilers_notes
    assert p.to_parsed_doc().variant == "amended"


def test_completeness_check_flags_dropped_text():
    from bs4 import BeautifulSoup

    html = (
        '<div class="sectionWrapper"><h1 class="h4">750.1 Test.</h1><p>Sec. 1.</p>'
        '<div><p class="commentary">hidden</p></div></div>'
    )
    c = parse_section_wrapper(BeautifulSoup(html, "html.parser").div)
    assert any(w.startswith("INTEGRITY") for w in c.warnings)


# ---- pipeline: variants ------------------------------------------------------------------------------------
def _amended_page_doc():
    return parse_section_page((DATA / "pages" / "section-333-16335-amended.html").read_text(encoding="utf-8")).to_parsed_doc()


class _Docs:
    name, complete, covered_chapters = "mcl", False, None

    def __init__(self, docs):
        self.docs = docs

    def iter_documents(self):
        return iter(self.docs)

    def iter_public_acts(self):
        return []


def test_variant_is_stored_but_never_presented_as_current_law(conn):
    from mlaw.models import ParsedDoc

    base = ParsedDoc(source="mcl", citation="MCL 333.16335", text="Sec. 16335.\n(1) Fees are $80.00.", catchline="Physical therapy; fees.",
                     chapter="333", act_citation="1978 PA 368", act_name="PUBLIC HEALTH CODE")
    run_ingest(conn, _Docs([base, _amended_page_doc()]), now=T1)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0] == 1  # only the ordinary node is searchable
    d = service.get_document(conn, "MCL 333.16335")
    assert "$80.00" in d["text"] and d["status"] == "active"
    (form,) = d["other_forms"]
    assert form["variant"] == "amended" and form["effective_date"] == "2028-01-22"
    assert "License fee, per year" in form["text"] and "NOT the text currently in force" in form["caution"]
    assert [x["citation"] for x in service.search(conn, "physical therapy fees")["results"]] == ["MCL 333.16335"]
    assert service.get_chapter_outline(conn, "333")["total"] == 1
    st = service.data_status(conn)["sources"][0]
    assert (st["active_documents"], st["extra_forms_listed"]) == (1, 1)
    ch = service.list_changes(conn, "2026-09-01")["changes"]
    assert {c["variant"] for c in ch} == {"", "amended"}


# ---- pipeline: ingest of chapter files, scoped removal ----------------------------------------------------
def test_files_source_skips_non_chapter_files_and_reports_scope(ch_dir):
    src = MclFilesSource(ch_dir)
    assert src.covered_chapters == {"37"} and src.complete
    docs = list(src.iter_documents())
    assert len(docs) == 142 and src.currency.endswith("PA 91 of 2026") and len(list(src.iter_ranges())) == 1


def test_files_source_needs_a_chapter(tmp_path):
    (tmp_path / "x.html").write_text("<html></html>")
    with pytest.raises(ValueError, match="no chapter renderings"):
        MclFilesSource(tmp_path)


def test_ingest_chapter_end_to_end(conn, ch_dir):
    r = run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    assert (r.docs_seen, r.docs_new) == (142, 142)
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (r.run_id,)).fetchone()
    assert run["scope"] == "chapters:37" and run["source_currency"].endswith("PA 91 of 2026")
    d = service.get_document(conn, "MCL 37.2202")
    assert d["act_citation"] == "1976 PA 453" and d["provenance"]["source_currency"].endswith("PA 91 of 2026")
    assert [x["citation"] for x in service.search(conn, "polygraph examination")["results"]][0].startswith("MCL 37.2")
    with pytest.raises(ValueError, match=r"MCL 37\.1 to MCL 37\.9.*Repealed\. 1976, Act 453"):
        service.get_document(conn, "MCL 37.5")
    st = service.data_status(conn)["sources"][0]
    assert st["last_full_crawl"] is None  # a one-chapter run is not a full crawl


def test_rerun_of_identical_chapter_makes_no_new_versions(conn, ch_dir):
    run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    r = run_ingest(conn, MclFilesSource(ch_dir), now=T2)
    assert (r.docs_new, r.docs_modified, r.docs_removed) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 142


def test_chapter_run_never_removes_other_chapters(conn, ch_dir):
    run_ingest(conn, FixtureSource(FIX / "day1"), now=T1)  # chapters 257 and 750
    r = run_ingest(conn, MclFilesSource(ch_dir), now=T2)
    assert r.docs_removed == 0
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE chapter IN ('257','750') AND active = 1").fetchone()[0] == 6


def test_section_missing_from_a_chapter_is_marked_removed_and_history_kept(conn, ch_dir):
    run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    # drop one section wrapper (MCL 37.2202) from the file
    html = CH37
    start = html.index('<h1 class="h4" style="font-weight:bold;">37.2202 ')
    wrap_start = html.rfind('<div class="sectionWrapper">', 0, start)
    wrap_end = html.index('<div class="sectionWrapper">', start)
    (ch_dir / "Chapter 37.html").write_text(html[:wrap_start] + html[wrap_end:], encoding="utf-8")
    r = run_ingest(conn, MclFilesSource(ch_dir), now=T2)
    assert (r.docs_seen, r.docs_removed) == (141, 1)
    d = service.get_document(conn, "MCL 37.2202")
    assert d["in_source_currently"] is False and d["removed_from_source_at"] == T2
    assert [c["change_type"] for c in service.list_changes(conn, "2026-09-22", change_type="removed")["changes"]] == ["removed"]


def test_truncated_chapter_file_trips_the_guard_and_changes_nothing(conn, ch_dir):
    run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    # keep only the first ~40 sections: cut the file after the 40th section wrapper and close the open tags
    idx = [m.start() for m in re.finditer(r'<div class="sectionWrapper">', CH37)][40]
    (ch_dir / "Chapter 37.html").write_text(CH37[:idx] + "</div></div></main></body></html>", encoding="utf-8")
    with pytest.raises(PartialCrawlError):
        run_ingest(conn, MclFilesSource(ch_dir), now=T2)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE active = 1").fetchone()[0] == 142
    assert conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"


def test_integrity_problem_aborts_the_run(conn, ch_dir):
    bad = CH37.replace('<h1 class="h4" style="font-weight:bold;">37.11 Short title.</h1>',
                       '<h1 class="h4" style="font-weight:bold;">Short title.</h1>', 1)
    (ch_dir / "Chapter 37.html").write_text(bad, encoding="utf-8")
    with pytest.raises(ChapterIntegrityError, match="unparseable section"):
        run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_range_no_longer_listed_is_deactivated(conn, ch_dir):
    run_ingest(conn, MclFilesSource(ch_dir), now=T1)
    (ch_dir / "Chapter 37.html").write_text(CH37.replace("37.1-37.9 Repealed.", "Note: 37.1-37.9 was here."), encoding="utf-8")
    run_ingest(conn, MclFilesSource(ch_dir), now=T2)
    assert conn.execute("SELECT active FROM ranges").fetchone()[0] == 0


# ---- web source with a mock server --------------------------------------------------------------------------
DOCPAGE = (DATA / "pages" / "document-chap37.html").read_text(encoding="utf-8")


def _download_page(n: int, nxt: str | None) -> str:
    html = DOCPAGE.replace("mcl-chap37", "@CUR@").replace("mcl-chap36", "@PREV@").replace("mcl-chap38", "@NEXT@")
    html = html.replace("MCL-CHAP37", f"MCL-CHAP{n}").replace("Chapter 37", f"Chapter {n}")
    html = html.replace("@CUR@", f"mcl-chap{n}").replace("@PREV@", f"mcl-chap{n-1}")
    if nxt is None:
        html = re.sub(r'<span><a href="[^"]*@NEXT@">Next Document.*?</a></span>', "", html, flags=re.S)
    else:
        html = html.replace("@NEXT@", nxt)
    return html


def make_client(handler):
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    c = PoliteClient(transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: t["now"],
                     user_agent="test/1.0", min_interval=5.0)
    return c, sleeps


def test_web_source_discovers_chapters_from_next_links_and_fetches_each():
    hits = []

    def handler(req):
        path = req.url.path
        hits.append(f"{path}?{req.url.query.decode()}")
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        obj = req.url.params.get("objectName", "")
        if path == "/Home/Document":
            n = int(obj.removeprefix("mcl-chap"))
            return httpx.Response(200, text=_download_page(n, f"mcl-chap{n+1}" if n < 37 else None))
        if path == "/Home/RenderDoc":
            if obj != "mcl-chap37":
                return httpx.Response(200, text=CH37.replace("Chapter 37", f"Chapter {obj.removeprefix('mcl-chap')}", 1))
            return httpx.Response(200, text=CH37)
        return httpx.Response(404)

    client, sleeps = make_client(handler)
    src = MclWebSource(client=client, start="mcl-chap36", chapters=None)
    assert src.discover_chapters() == ["36", "37"]
    # fetch just chapter 37 (explicit) through the same client
    src2 = MclWebSource(client=client, chapters=["37"])
    assert src2.covered_chapters == {"37"}
    docs = list(src2.iter_documents())
    assert len(docs) == 142 and src2.chapters_seen == ["37"]
    assert any(h.startswith("/Home/RenderDoc?objectName=mcl-chap37") for h in hits)
    assert max(sleeps) >= 5.0  # honors the configured pause between requests


def test_web_source_refuses_when_robots_unreadable():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(502, text="Bad gateway")
        return httpx.Response(200, text=CH37)

    client, _ = make_client(handler)
    from mlaw.web import RobotsDisallowed

    with pytest.raises(RobotsDisallowed):
        list(MclWebSource(client=client, chapters=["37"]).iter_documents())


def test_web_source_rejects_wrong_chapter_in_response():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=CH37)  # chapter 37 returned for every request

    client, _ = make_client(handler)
    with pytest.raises(ChapterIntegrityError, match="requested chapter 38"):
        list(MclWebSource(client=client, chapters=["38"]).iter_documents())


def test_web_source_requires_a_real_user_agent(monkeypatch):
    with pytest.raises(RuntimeError, match="MLAW_USER_AGENT"):
        MclWebSource(chapters=["37"])
