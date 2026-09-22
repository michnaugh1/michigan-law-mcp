"""The Legislature's own machine-readable XML (Chapter N.xml), cross-checked against the already-tested
whole-chapter HTML parser wherever both cover the same chapter (Chapter 37)."""

import html
from pathlib import Path

import httpx
import pytest

from mlaw import service
from mlaw.chapterdoc import parse_chapter_document
from mlaw.mclxml import parse_chapter_xml
from mlaw.pipeline import run_ingest
from mlaw.sources.mcl_web import ChapterIntegrityError, MclXmlFilesSource, MclXmlWebSource
from mlaw.web import PoliteClient

DATA = Path(__file__).parent / "data"
XML37 = (DATA / "xml" / "chapter-37.xml").read_bytes()
XML2 = (DATA / "xml" / "chapter-2.xml").read_bytes()
XML131 = (DATA / "xml" / "chapter-131.xml").read_bytes()
XML1 = (DATA / "xml" / "chapter-1.xml").read_bytes()  # the Constitution, filed as Chapter 1 -- see below
CH37_HTML = (DATA / "chapters" / "chapter-37.html").read_text(encoding="utf-8")
T1, T2 = "2026-09-21T19:00:00Z", "2026-09-22T06:00:00Z"


def _section_xml(number: str, catchline: str, body_inner: str, repealed: bool = False) -> str:
    """A minimal ``<MCLSectionInfo>`` -- enough of the real shape (see mclxml.py's module docstring) to
    exercise one parsing path without needing a multi-megabyte real chapter file as a fixture."""
    body = html.escape(f"<Section-Body><Section-Number>Sec. 1.</Section-Number>{body_inner}</Section-Body>")
    return f"""
    <MCLSectionInfo>
      <MCLNumber>{number}</MCLNumber>
      <SectRef>{number}</SectRef>
      <Label>1</Label>
      <CatchLine>{catchline}</CatchLine>
      <Repealed>{str(repealed).lower()}</Repealed>
      <HistoryText></HistoryText>
      <EditorsNotes />
      <Commentary />
      <BodyText>{body}</BodyText>
    </MCLSectionInfo>"""


def _chapter_xml(name: str, title: str, multi_chapter: bool, sections_xml: str) -> bytes:
    """A minimal but structurally faithful whole-chapter XML file, one statute holding ``sections_xml``."""
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


@pytest.fixture
def xml_dir(tmp_path):
    d = tmp_path / "xml"
    d.mkdir()
    (d / "Chapter 37.xml").write_bytes(XML37)
    (d / "not-a-chapter.xml").write_text("<html><body>nope</body></html>")
    return d


# ---- parsing, and the cross-check against the HTML source -------------------------------------------------
def test_chapter_37_xml_matches_the_html_rendering_exactly():
    """The whole point of adding the XML source: it must produce the same law as the already-tested
    HTML parser before it can be trusted, for every field that matters to the pipeline and to a reader."""
    html_doc = parse_chapter_document(CH37_HTML)
    xml_doc = parse_chapter_xml(XML37)

    assert (xml_doc.chapter, xml_doc.name) == (html_doc.chapter, html_doc.name)
    assert xml_doc.warnings == [] and xml_doc.integrity_problems() == []
    assert (len(xml_doc.statutes), len(xml_doc.sections()), len(xml_doc.ranges())) == \
           (len(html_doc.statutes), len(html_doc.sections()), len(html_doc.ranges())) == (17, 142, 1)
    # the XML source carries no currency stamp of its own (see mclxml.py's module docstring)
    assert xml_doc.currency is None and xml_doc.through_act is None
    assert html_doc.currency is not None

    html_secs = {(s.doc.citation, s.doc.variant): s for st in html_doc.statutes for s in st.sections}
    xml_secs = {(s.doc.citation, s.doc.variant): s for st in xml_doc.statutes for s in st.sections}
    assert set(html_secs) == set(xml_secs)
    fields = ["text", "catchline", "status", "effective_date", "history_note", "compilers_notes",
              "act_citation", "act_name", "chapter", "kind", "variant", "url"]
    for key, h in html_secs.items():
        x = xml_secs[key]
        for f in fields:
            assert getattr(h.doc, f) == getattr(x.doc, f), (key, f, getattr(h.doc, f), getattr(x.doc, f))
        assert h.division_path == x.division_path, (key, h.division_path, x.division_path)

    hr, xr = html_doc.ranges()[0], xml_doc.ranges()[0]
    assert (hr.start_citation, hr.end_citation, hr.note, hr.status) == (xr.start_citation, xr.end_citation, xr.note, xr.status)


def test_chapter_131_fully_repealed_chapter_has_no_sections():
    cd = parse_chapter_xml(XML131)
    assert (cd.chapter, cd.name) == ("131", "MUNICIPAL FINANCE ACT")
    assert cd.sections() == [] and cd.warnings == []
    (r,) = cd.ranges()
    assert (r.start_citation, r.end_citation, r.status) == ("MCL 131.1", "MCL 139.3", "repealed")


def test_chapter_rejected_by_voters_stub_is_recognized_not_just_repealed_expired_reserved():
    """Chapter 260 ("TRANSPORTATION SYSTEMS", confirmed live by Mike 2026-09-22) is the real-world sibling
    of the Chapter 131 fully-repealed case above: no sections at all, and its one MCLStatuteInfo's
    LongTitle is a stub line -- but "260.1-260.10 Rejected by voters on Nov. 5, 1974.", not
    "Repealed."/"Expired."/"Reserved.". An act submitted to referendum and voted down never took effect at
    all, a distinct status from something that was law and stopped being -- the stub regex previously only
    recognized the latter three words, so this range went unrecorded and, combined with zero sections,
    tripped "no sections found; refusing to treat as empty" and aborted the whole chapter's ingest."""
    xml = """<?xml version="1.0" encoding="utf-16"?>
<MCLChapterInfo>
  <DocumentID>1</DocumentID>
  <Repealed>false</Repealed>
  <EditorsNotes />
  <Commentary />
  <History />
  <Name>260</Name>
  <Title>TRANSPORTATION SYSTEMS</Title>
  <MultiChapter>false</MultiChapter>
  <FirstSectionNumber />
  <LastSectionNumber />
  <MCLDocumentInfoCollection>
    <MCLStatuteInfo>
      <DocumentID>2</DocumentID>
      <Repealed>true</Repealed>
      <EditorsNotes />
      <Commentary />
      <HistoryText />
      <History />
      <Name>Act 245 of 1974</Name>
      <Heading>TRANSPORTATION SYSTEMS</Heading>
      <LongTitle>260.1-260.10 Rejected by voters on Nov. 5, 1974.</LongTitle>
      <ShortTitle />
      <StyleClause />
      <MCLDocumentInfoCollection />
    </MCLStatuteInfo>
  </MCLDocumentInfoCollection>
</MCLChapterInfo>""".encode("utf-16")
    cd = parse_chapter_xml(xml)
    assert (cd.chapter, cd.name) == ("260", "TRANSPORTATION SYSTEMS")
    assert cd.sections() == [] and cd.warnings == []
    assert cd.integrity_problems() == []
    (r,) = cd.ranges()
    assert (r.start_citation, r.end_citation, r.status) == ("MCL 260.1", "MCL 260.10", "rejected")


def test_chapter_2_variant_section_is_its_own_statute_and_never_current_law():
    """Section 2.13.new -- the Legislature's own example of a variant node, MCL Ch. 2 -- is represented
    as an entirely separate MCLStatuteInfo (the enacting act) alongside the real acts, not as an alternate
    version inside the real one; see mclxml.py's module docstring."""
    cd = parse_chapter_xml(XML2)
    docs = {(d.citation, d.variant): d for d in cd.docs()}
    assert ("MCL 2.13", "") not in docs  # no current section at this citation yet
    d = docs[("MCL 2.13", "new")]
    assert d.act_citation == "2026 PA 7" and d.act_name == "STATE DUCK"
    assert d.catchline == "Official state duck." and "wood duck" in d.text
    assert d.url == "https://www.legislature.mi.gov/Laws/MCL?objectName=mcl-2-13-new"
    assert "THIS NEW SECTION IS EFFECTIVE 91 DAYS AFTER ADJOURNMENT" in d.compilers_notes


def test_chapter_2_sectref_naming_extra_citations_becomes_a_range_not_a_parse_error():
    """A handful of old, fully-repealed entries give SectRef a comma list ("2.104, 2.105") instead of
    repeating MCLNumber; MCLNumber ("2.104") is always the section's own citation, and the extra citation
    is recorded as a covered-range stub rather than silently dropped or mistaken for the section's own."""
    cd = parse_chapter_xml(XML2)
    docs = {d.citation: d for d in cd.docs()}
    assert docs["MCL 2.104"].status == "repealed"
    assert "MCL 2.105" not in docs
    ranges = {(r.start_citation, r.end_citation): r for r in cd.ranges()}
    r = ranges[("MCL 2.105", "MCL 2.105")]
    assert r.status == "repealed" and "MCL 2.104" in r.note


def test_chapter_2_table_embedded_in_a_paragraph_is_rendered_not_dropped():
    """MCL 2.201 (a Wisconsin-Michigan boundary compact) embeds a literal HTML <table> inside a <P>."""
    cd = parse_chapter_xml(XML2)
    doc = next(d for d in cd.docs() if d.citation == "MCL 2.201")
    assert "A COMPACT" in doc.text
    assert any("|" in line for line in doc.text.split("\n"))
    assert cd.integrity_problems() == []


def test_chapter_2_unrecognized_statute_labels_are_warned_not_dropped():
    cd = parse_chapter_xml(XML2)
    assert any("J.R. 10 of 1897" in w for w in cd.warnings)
    assert any(d.act_name for d in cd.docs() if "J.R." in (d.act_citation or ""))


def test_catchline_status_word_requires_a_trailing_period_not_just_a_word_boundary():
    """MCL 89.4 (real Chapter 81.xml, which bundles chapter 89 alongside its own -- confirmed live by Mike
    2026-09-22): CatchLine "Repealed ordinances; re-enactment." is an ACTIVE section's title (about the
    re-enactment of repealed ordinances) with Repealed=false, not a status-prefixed catchline. A bare \\b
    word-boundary match on the first word wrongly read this as status="repealed", which then disagreed
    with the real Repealed=false flag and aborted the whole chapter's ingest under pipeline.py's
    strict-by-default integrity check. The genuine status-prefix shape ("Repealed. 1976, Act 453, Eff. ...")
    always has the word immediately followed by a period (see test_chapter_2_sectref_naming_extra_citations_
    becomes_a_range_not_a_parse_error for a real example of that shape still being detected correctly)."""
    sections = _section_xml(
        "89.4", "Repealed ordinances; re-enactment.",
        "<Paragraph><P>No repealed ordinance shall be revived unless the whole...</P></Paragraph>",
    )
    cd = parse_chapter_xml(_chapter_xml("89", "GENERAL LAW VILLAGE ACT", False, sections))
    assert cd.integrity_problems() == []
    d = next(dd for dd in cd.docs() if dd.citation == "MCL 89.4")
    assert d.status == "active"
    assert d.catchline == "Repealed ordinances; re-enactment."


def test_catchline_status_word_tolerates_the_real_reepaled_typo():
    """MCL 205.96a (real Chapter 205.xml, confirmed live by Mike 2026-09-22): CatchLine "Reepaled. 2006,
    Act 673, Eff. Jan. 1, 2011." -- misspelled on the Legislature's own site. Repealed=true and its real
    EditorsNotes ("The repealed section pertained to qualified athletic event.") both corroborate it's
    genuinely repealed, so this typo is recognized as an alias rather than left to disagree with the
    Repealed flag and abort the whole chapter's ingest."""
    sections = _section_xml("205.96a", "Reepaled. 2006, Act 673, Eff. Jan. 1, 2011.", "", repealed=True)
    cd = parse_chapter_xml(_chapter_xml("205", "USE TAX ACT", False, sections))
    assert cd.integrity_problems() == []
    d = next(dd for dd in cd.docs() if dd.citation == "MCL 205.96a")
    assert d.status == "repealed"
    assert d.effective_date == "2011-01-01"


def test_catchline_status_word_after_a_leading_echoed_section_number():
    """MCL 324.32724 (real Chapter 324.xml/NREPA, confirmed live by Mike 2026-09-22): CatchLine
    "324.32724 Repealed. 2008, Act 181, Imd. Eff. July 9, 2008." -- unlike every other chapter seen so
    far, this one echoes the section's own number before the status word instead of leading straight
    with it. The status word must still be recognized right after that leading number, or it disagrees
    with the real Repealed=true flag and aborts the whole chapter's ingest; the stored catchline is left
    untouched either way (the number stays in it, exactly as the compilers wrote it)."""
    sections = _section_xml("324.32724", "324.32724 Repealed. 2008, Act 181, Imd. Eff. July 9, 2008.", "", repealed=True)
    cd = parse_chapter_xml(_chapter_xml("324", "NATURAL RESOURCES AND ENVIRONMENTAL PROTECTION ACT", False, sections))
    assert cd.integrity_problems() == []
    d = next(dd for dd in cd.docs() if dd.citation == "MCL 324.32724")
    assert d.status == "repealed"
    assert d.effective_date == "2008-07-09"
    assert d.catchline == "324.32724 Repealed. 2008, Act 181, Imd. Eff. July 9, 2008."


def test_catchline_status_word_with_no_period_before_a_year_is_still_recognized():
    """MCL 388.1623g (real Chapter 388.xml/State School Aid Act, confirmed live by Mike 2026-09-22):
    CatchLine "Repealed 2026, Act 25, Imd. Eff. July 21, 2026." -- no period at all after the status word,
    unlike its own sibling section 388.1623h ("Repealed.  2025, Act 15, ..."), which does have one (both
    conventions coexist in the same chapter). The distinguishing signal from an ordinary title like MCL
    89.4's "Repealed ordinances; re-enactment." is what follows the word: history-citation text always
    starts with a period or a year, never another word."""
    sections = _section_xml("388.1623g", "Repealed 2026, Act 25, Imd. Eff. July 21, 2026.", "", repealed=True)
    cd = parse_chapter_xml(_chapter_xml("388", "STATE SCHOOL AID ACT", False, sections))
    assert cd.integrity_problems() == []
    d = next(dd for dd in cd.docs() if dd.citation == "MCL 388.1623g")
    assert d.status == "repealed"
    assert d.effective_date == "2026-07-21"


def test_catchline_status_word_recognizes_rescinded_for_executive_orders():
    """MCL 388.996 (real Chapter 388.xml, confirmed live by Mike 2026-09-22): CatchLine "Rescinded. 2005,
    E.O. No. 2005-4, Eff. Feb. 15, 2005." -- an Executive (Reorganization) Order undone by a later E.O.,
    not a statutory repeal. feed.py's _STATUS_HINT already anticipated this word; the main parser's
    status_words didn't yet. Kept as its own "rescinded" status, same reasoning as "abrogated"/"rejected".
    effective_date stays None here: history.py's _ENTRY grammar only recognizes "YYYY, Act N[, Eff. ...]",
    not an "E.O. No." citation -- a real, non-blocking gap (same category as the Constitution's own
    amendment-history grammar gap noted in mclxml.py's module docstring), not something this fix covers."""
    sections = _section_xml("388.996", "Rescinded. 2005, E.O. No. 2005-4, Eff. Feb. 15, 2005.", "", repealed=True)
    cd = parse_chapter_xml(_chapter_xml("388", "STATE SCHOOL AID ACT", False, sections))
    assert cd.integrity_problems() == []
    d = next(dd for dd in cd.docs() if dd.citation == "MCL 388.996")
    assert d.status == "rescinded"
    assert d.effective_date is None


def test_chapter_1_is_the_constitution_confirmed_against_the_real_file():
    """The Michigan Constitution is served as Chapter 1 of the same MCL XML system, confirmed by Mike
    against the real file (2026-09-22): same file shape, one MCLStatuteInfo ("CONSTITUTION OF MICHIGAN OF
    1963"), 13 MCLDivisionInfo (Articles I-XII plus a trailing "Schedule" division -- DivisionNumber
    "Schedule", not a roman numeral), 281 MCLSectionInfo, no nested divisions -- all 281 parse cleanly,
    including a ceremonial signature/vote-record block whose MCLNumber has no "§ N" of its own but whose
    SectRef does (see the next test and normalize_const_mclnumber)."""
    cd = parse_chapter_xml(XML1)
    assert (cd.chapter, cd.name) == ("1", "Constitution of Michigan of 1963")
    assert len(cd.docs()) == 281
    assert all(d.source == "const" for d in cd.docs())
    assert all(d.chapter == "1" for d in cd.docs())
    assert all(d.act_citation == "Const 1963" and d.act_name == "STATE CONSTITUTION" for d in cd.docs())
    assert all(d.url == "https://www.legislature.mi.gov/Laws/MCL?objectName=MCL-CHAP1" for d in cd.docs())
    assert cd.integrity_problems() == []


def test_chapter_1_signature_block_falls_back_to_sectref():
    """The Schedule's ceremonial signature/vote-record block (MCLNumber "Schedule SigBlock") has no "§ N"
    in MCLNumber, but SectRef ("§ 0") does -- normalize_const_mclnumber falls back to it so this real
    document isn't silently dropped (it was, before this fallback existed; dropping even one section used
    to abort the whole chapter's ingest under pipeline.py's strict-by-default integrity check, so this
    isn't just cosmetic)."""
    cd = parse_chapter_xml(XML1)
    docs = {d.citation: d for d in cd.docs()}
    d = docs["Const 1963, Schedule, § 0"]
    assert d.catchline == "Vote Record."
    assert "Adopted by the Constitutional Convention" in d.text


def test_chapter_1_article_and_schedule_citations_match_mikes_sample():
    """Cross-checked against the exact real-page sample Mike pasted (2026-09-22): the internal Sec. N.
    numbering restarts in every Article (never the citation), the true citation lives in MCLNumber (not
    BodyText), and the catchline is "§ N <title>" style rather than an ordinary MCL catchline."""
    cd = parse_chapter_xml(XML1)
    docs = {d.citation: d for d in cd.docs()}

    d = docs["Const 1963, art 1, § 3"]
    assert d.catchline == "Assembly, consultation, instruction, petition."
    assert d.history_note == "Const. 1963, Art. I, § 3, Eff. Jan. 1, 1964"
    assert d.text.startswith("Sec. 3.")
    assert "peaceably to assemble" in d.text
    assert d.status == "active"

    # Article XII (roman numeral 12) and the trailing "Schedule" division (not a numbered article) both
    # resolve through the same MCLNumber shape, "Article <roman> § N" vs "Schedule § N".
    assert "Const 1963, art 12, § 1" in docs
    d = docs["Const 1963, Schedule, § 1"]
    assert d.catchline == "Recommendations by attorney general for changes in laws."
    assert d.history_note == "Const. 1963, Schedule, § 1, Eff. Jan. 1, 1964"


def test_chapter_1_abrogated_sections_use_history_not_catchline_for_status():
    """Unlike ordinary MCL sections, the Constitution doesn't prefix a defunct provision's CatchLine with
    a status word -- Article IV §§ 4-5 (annexation/merger and island-areas provisions, both removed by the
    2018 initiated law that restructured local-government redistricting) keep a plain descriptive
    catchline, an empty BodyText, and carry the status word ("Abrogated.") in HistoryText instead."""
    cd = parse_chapter_xml(XML1)
    docs = {d.citation: d for d in cd.docs()}
    for cite, catchline in [
        ("Const 1963, art 4, § 4", "Annexation or merger with a city."),
        ("Const 1963, art 4, § 5", "Island areas, contiguity."),
    ]:
        d = docs[cite]
        assert d.status == "abrogated"
        assert d.catchline == catchline
        assert d.text == ""
        assert d.history_note.startswith("Abrogated. Initiated Law")
    assert cd.integrity_problems() == []  # neither abrogated section trips the Repealed-flag-vs-status check


def test_chapter_1_former_constitution_note_gets_a_human_label():
    """MCL 750.145p-style Compiler's/Editor's Notes are already labeled from a fixed map; the Constitution
    adds its own EditorsNoteInfo Type ("FormerConst") for "Former Constitution: See Const. 1908, ..." --
    confirmed to match the wording in Mike's real-page sample."""
    cd = parse_chapter_xml(XML1)
    d = next(d for d in cd.docs() if d.citation == "Const 1963, art 1, § 3")
    assert "Former Constitution: See Const. 1908, Art. II, § 2." in d.compilers_notes


def test_ingest_chapter_1_end_to_end(conn, tmp_path):
    """Proven through the real pipeline, not just the parser -- a single MclXmlFilesSource (.name == "mcl")
    producing both "mcl" and "const" documents in one run is new territory for pipeline.py, which used to
    assume one source always emits exactly one ParsedDoc.source. First attempt at this raised two real bugs,
    both fixed: (1) the per-document citation-source check compared against the *source's* fixed .name
    instead of the document's own declared .source, which rejected every const document outright; (2)
    previously-active/removal accounting was scoped to source.name alone, which would have silently
    excluded const documents from the removal-guard safety net entirely. See pipeline.py's Source.source_kinds
    and mcl_web.py's _ChapterSourceBase."""
    (tmp_path / "Chapter 1.xml").write_bytes(XML1)
    report = run_ingest(conn, MclXmlFilesSource(tmp_path), now=T1)
    assert (report.docs_seen, report.docs_new, report.docs_modified) == (281, 281, 0)

    outline = service.get_chapter_outline(conn, "1", source="const", limit=500)
    assert outline["total"] == 281
    doc = service.get_document(conn, "Const 1963, art 1, § 3")
    assert doc["status"] == "active" and "peaceably to assemble" in doc["text"]


def test_data_status_counts_const_documents_under_their_crawler_source(conn, tmp_path):
    """Confirmed live 2026-09-22: after Mike's first full production crawl (43,929 docs_new via the "mcl"
    crawler source, MCL + the Constitution in one run), `mlaw status` reported only 43,648 total for "mcl"
    (43,424 active + 224 variants) -- short by exactly 281, the Constitution's whole section count.
    Root cause: `data_status()`'s per-source counts query filtered `documents.source = ?` using the
    *crawler's* run-source name ("mcl", from `runs.source`) -- but `documents.source` is a different
    namespace, the document's own kind (ParsedDoc.source: "mcl" | "const" | ...). Since
    MclXmlFilesSource/MclXmlWebSource (name="mcl") legitimately emit "const" documents too (see
    test_ingest_chapter_1_end_to_end above and pipeline.py's Source.source_kinds), every Constitution
    document was silently invisible to `mlaw status`'s counts -- not a data-loss bug (the ingest itself
    stored them correctly, and get_chapter_outline/get_document above already prove that), just a reporting
    one. This ingests only Chapter 1.xml (all 281 documents are source="const", none "mcl") through the
    "mcl"-named crawler, so the bug is total rather than partial: the old query would report
    active_documents=0 for "mcl" here, when it should report 281."""
    (tmp_path / "Chapter 1.xml").write_bytes(XML1)
    run_ingest(conn, MclXmlFilesSource(tmp_path), now=T1)
    st = service.data_status(conn)["sources"]
    assert len(st) == 1 and st[0]["source"] == "mcl"
    assert st[0]["active_documents"] == 281
    assert st[0]["removed_documents"] == 0
    assert st[0]["extra_forms_listed"] == 0
    sig = service.get_document(conn, "Const 1963, Schedule, § 0")
    assert sig["catchline"] == "Vote Record."

    # a repeat ingest of the same file is a true no-op, same as every other real chapter's own idempotency
    # proof elsewhere in this file/test_refresh.py -- confirms removal accounting doesn't wrongly fire on
    # the const documents it now has to track.
    report2 = run_ingest(conn, MclXmlFilesSource(tmp_path), now=T2)
    assert (report2.docs_seen, report2.docs_new, report2.docs_modified, report2.docs_removed) == (281, 0, 0, 0)


def test_chapter_xml_with_the_mclwebservice_default_namespace_still_parses():
    """Most Chapter N.xml files carry no XML namespace at all -- plain <Name>37</Name>. Chapter 115.xml
    ("FOURTH CLASS CITIES") does not, confirmed live by Mike 2026-09-22: it declares a default namespace,
    xmlns="http://localhost/MCLWebService/MCLSearchService" (an artifact of being served through the
    underlying MCLWebService), on each of MCLChapterInfo's direct children individually -- NOT on the root
    element itself -- and inherited from there by everything nested underneath, e.g.
    MCLDocumentInfoCollection's own MCLStatuteInfo/MCLSectionInfo descendants never repeat the attribute.
    Every bare el.find("Name")-style lookup in this module silently found nothing against a real copy of
    this shape, surfacing as "chapter XML file has no <Name>" for a file that plainly has one -- this
    reproduces that exact shape (trimmed from the real file) rather than the ordinary unnamespaced shape
    every other fixture in this test module uses."""
    xml = """<?xml version="1.0" encoding="utf-16"?>
<MCLChapterInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <DocumentID xmlns="http://localhost/MCLWebService/MCLSearchService">6229</DocumentID>
  <Repealed xmlns="http://localhost/MCLWebService/MCLSearchService">false</Repealed>
  <EditorsNotes xmlns="http://localhost/MCLWebService/MCLSearchService" />
  <Commentary xmlns="http://localhost/MCLWebService/MCLSearchService" />
  <History xmlns="http://localhost/MCLWebService/MCLSearchService" />
  <Name xmlns="http://localhost/MCLWebService/MCLSearchService">115</Name>
  <Title xmlns="http://localhost/MCLWebService/MCLSearchService">FOURTH CLASS CITIES</Title>
  <MultiChapter xmlns="http://localhost/MCLWebService/MCLSearchService">false</MultiChapter>
  <FirstSectionNumber xmlns="http://localhost/MCLWebService/MCLSearchService" />
  <LastSectionNumber xmlns="http://localhost/MCLWebService/MCLSearchService" />
  <MCLDocumentInfoCollection xmlns="http://localhost/MCLWebService/MCLSearchService">
    <MCLStatuteInfo>
      <DocumentID>6230</DocumentID>
      <Repealed>false</Repealed>
      <EditorsNotes />
      <Commentary />
      <HistoryText></HistoryText>
      <History />
      <Name>Act 4 of 1911</Name>
      <Heading>LEGALIZATION OF SEWER BONDS</Heading>
      <LongTitle>AN ACT legalizing sewer bonds.</LongTitle>
      <ShortTitle />
      <StyleClause>The People of the State of Michigan enact:</StyleClause>
      <MCLDocumentInfoCollection>
        <MCLSectionInfo>
          <DocumentID>6231</DocumentID>
          <Repealed>false</Repealed>
          <EditorsNotes />
          <Commentary />
          <HistoryText></HistoryText>
          <History />
          <MCLNumber>115.1</MCLNumber>
          <SectRef>115.1</SectRef>
          <Label>1</Label>
          <CatchLine>Short title.</CatchLine>
          <BodyText>&lt;Section-Body&gt;&lt;Section-Number&gt;Sec. 1.&lt;/Section-Number&gt;&lt;Paragraph&gt;&lt;P&gt;Test.&lt;/P&gt;&lt;/Paragraph&gt;&lt;/Section-Body&gt;</BodyText>
        </MCLSectionInfo>
      </MCLDocumentInfoCollection>
    </MCLStatuteInfo>
  </MCLDocumentInfoCollection>
</MCLChapterInfo>""".encode("utf-16")
    cd = parse_chapter_xml(xml)
    assert (cd.chapter, cd.name) == ("115", "FOURTH CLASS CITIES")
    assert cd.integrity_problems() == []
    doc = next(d for d in cd.docs() if d.citation == "MCL 115.1")
    assert doc.catchline == "Short title." and doc.status == "active"


def test_not_a_chapter_xml_file_is_rejected():
    with pytest.raises(ValueError, match="MCLChapterInfo"):
        parse_chapter_xml(b"<?xml version='1.0'?><Foo/>")


def test_multichapter_file_sections_keep_their_own_citations_chapter():
    """MultiChapter=true (confirmed against the real Chapter 760.xml, "CODE OF CRIMINAL PROCEDURE", which
    is filed under "760" but actually holds sections for chapters 760-777 plus 767A and 771A): a section
    legitimately citing a chapter other than the file's own <Name> must not trip the "appears inside
    chapter X" integrity check, and must be stored (chapterdoc.py's _to_doc) under its own true chapter,
    not the file's."""
    sections = _section_xml("760.1", "Short title.", "<Paragraph><P>This act shall be known as the code.</P></Paragraph>") + \
        _section_xml("761.1", "Definitions.", "<Paragraph><P>As used in this act...</P></Paragraph>")
    cd = parse_chapter_xml(_chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", True, sections))
    assert cd.warnings == [] and cd.integrity_problems() == []
    by_cite = {d.citation: d for d in cd.docs()}
    assert by_cite["MCL 760.1"].chapter == "760"
    assert by_cite["MCL 761.1"].chapter == "761"


def test_non_multichapter_file_still_flags_a_misplaced_section():
    """Without MultiChapter=true, a section outside the declared chapter is exactly the kind of mismatch
    the integrity check exists to catch -- this must keep firing for ordinary single-chapter files."""
    sections = _section_xml("761.1", "Definitions.", "<Paragraph><P>As used in this act...</P></Paragraph>")
    cd = parse_chapter_xml(_chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", False, sections))
    assert any("appears inside chapter 760" in w for w in cd.integrity_problems())


def test_table_as_direct_child_of_paragraph_is_rendered_not_dropped():
    """MCL 333.26252/26329/26330 (E.R.O.-derived sections in the real Chapter 333.xml) embed a <table> as
    a direct child of <Paragraph>, with no <P> wrapper at all -- unlike the Chapter 2.xml case already
    covered by test_chapter_2_table_embedded_in_a_paragraph_is_rendered_not_dropped, where the <table> is
    nested inside a <P>."""
    body = '<Paragraph><table border="0"><tr><td>Term</td><td>Meaning</td></tr></table></Paragraph>'
    cd = parse_chapter_xml(_chapter_xml("333", "HEALTH", False, _section_xml("333.1", "Definitions.", body)))
    assert cd.integrity_problems() == []
    assert "Term | Meaning" in cd.docs()[0].text


def test_mclnumber_bracket_disambiguator_accepts_a_letter_not_just_a_digit():
    """MCL 752.863[a] (real Chapter 752.xml -- confirmed live 2026-09-22) is a genuine second section
    compiled to the same base citation as plain MCL 752.863: two different acts (Act 45 of 1952 and Act 14
    of 1955) each enacted their own "Section 3", and the Compiler distinguished the second one with a
    bracket suffix on the MCLNumber -- its own Compiler's Note explains it was "compiled as MCL 752.863[a]
    to distinguish it from another section 3". _NUM_VARIANT's bracket alternative previously accepted only
    digits (``\\[\\d+\\]``, for the already-known "333.5474c[1]" shape), so this real letter-valued bracket
    failed to match at all -- ValueError("unrecognized MCLNumber") -- which chapterdoc.py's caller renders
    as an "unparseable section" integrity problem and pipeline.py's strict-by-default check then aborted
    the whole chapter's ingest over. Note this is NOT the "law passed but not yet in effect" variant shape
    (.new/.amended/.added): MCL 752.863[a]'s own Act 14 of 1955 has long since taken effect (Eff. Oct. 14,
    1955) -- the bracket here means "a different, ordinary section that collides on citation", not
    "not yet in effect"."""
    sections = _section_xml("752.863", "Section repealed.", "<Paragraph><P>Section 235a ... is repealed.</P></Paragraph>") + \
        _section_xml("752.863[a]", "Definitions.", "<Paragraph><P>As used in this act...</P></Paragraph>")
    cd = parse_chapter_xml(_chapter_xml("752", "CONCEALED WEAPONS", False, sections))
    assert cd.integrity_problems() == []
    docs = {(d.citation, d.variant): d for d in cd.docs()}
    assert ("MCL 752.863", "") in docs
    assert ("MCL 752.863", "[a]") in docs
    assert docs[("MCL 752.863", "[a]")].catchline == "Definitions."


def test_p_with_a_table_followed_by_trailing_text_in_the_same_p_is_not_dropped():
    """MCL 600.9901 (the Revised Judicature Act's repeal table, real Chapter 600.xml -- confirmed live
    2026-09-22) has one giant <Paragraph> whose <P> children mix running text and <table>s: several <P>s
    hold a <table> immediately followed, still inside that same <P>, by ordinary text introducing the next
    numbered item -- "...</table>(2) Public Acts,\\n</P>". The previous approach (``child.find("table")``,
    which searches all descendants and then discards everything else in the <P> once it finds one) silently
    dropped that trailing "(2) Public Acts," text, which made the embedded-markup completeness check in
    _parse_embedded fire "INTEGRITY: extracted body differs from the text inside the embedded markup" and
    abort the whole chapter's ingest. Confirmed via live diagnostics against the real BodyText that the
    dropped text sits as a bare string node directly inside the <P>, a sibling of the <table> rather than
    nested inside it -- this fixture reproduces that exact shape (with the surrounding "(1) ..." and
    "Year of Act ..." <P>s that have no table at all, unaffected either way, on either side)."""
    body = (
        "<Paragraph>"
        "<P>(1) Revised Statutes of 1846, as amended, Chapter Section Numbers Compiled Law Sections (1948) "
        "43 11 to 14 692.311 to 692.314</P>"
        '<P><table border="0"><tr><td>Chapter</td><td>Section Numbers</td></tr>'
        "<tr><td>150</td><td>14</td></tr></table>(2) Public Acts,</P>"
        "<P>Year of Act Public Act Number Section Numbers 1959 161 691.481 to 691.492</P>"
        "</Paragraph>"
    )
    cd = parse_chapter_xml(_chapter_xml("600", "REVISED JUDICATURE ACT", False, _section_xml("600.9901", "Repeal.", body)))
    assert cd.integrity_problems() == []
    text = cd.docs()[0].text
    assert "(2) Public Acts," in text
    assert "150 | 14" in text
    assert "(1) Revised Statutes of 1846" in text
    assert "Year of Act Public Act Number" in text


def test_xml_files_source_computes_full_covered_chapters_for_multichapter_file(tmp_path):
    """covered_chapters has to be the complete set before pipeline.py starts reading documents (it is read
    up front, before iteration begins) -- a cheap header peek at <Name> alone isn't enough for a
    MultiChapter file, so MclXmlFilesSource scans the whole file for every <MCLNumber> chapter prefix."""
    sections = _section_xml("760.1", "Short title.", "<Paragraph><P>Text.</P></Paragraph>") + \
        _section_xml("767A.1", "Definitions.", "<Paragraph><P>Text.</P></Paragraph>")
    (tmp_path / "Chapter 760.xml").write_bytes(_chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", True, sections))
    src = MclXmlFilesSource(tmp_path)
    assert src.covered_chapters == {"760", "767A"}
    docs = list(src.iter_documents())
    assert {d.chapter for d in docs} == {"760", "767A"}


def test_ingest_multichapter_file_end_to_end(conn, tmp_path):
    """The scope check in pipeline.py must not reject a section whose chapter differs from the file's own
    declared chapter, once covered_chapters correctly includes every chapter the file actually covers."""
    sections = _section_xml("760.1", "Short title.", "<Paragraph><P>Text.</P></Paragraph>") + \
        _section_xml("761.1", "Definitions.", "<Paragraph><P>Text.</P></Paragraph>")
    (tmp_path / "Chapter 760.xml").write_bytes(_chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", True, sections))
    report = run_ingest(conn, MclXmlFilesSource(tmp_path), now=T1)
    assert (report.docs_seen, report.docs_new) == (2, 2)
    assert service.get_document(conn, "MCL 761.1")["chapter"] == "761"


# ---- MclXmlFilesSource ------------------------------------------------------------------------------------
def test_xml_files_source_skips_non_chapter_files_and_reports_scope(xml_dir):
    src = MclXmlFilesSource(xml_dir)
    assert src.covered_chapters == {"37"} and src.complete
    docs = list(src.iter_documents())
    assert len(docs) == 142 and len(list(src.iter_ranges())) == 1
    assert src.currency is None  # no currency stamp in the XML source


def test_xml_files_source_needs_a_chapter(tmp_path):
    (tmp_path / "x.xml").write_text("<html></html>")
    with pytest.raises(ValueError, match="no chapter XML files"):
        MclXmlFilesSource(tmp_path)


def test_xml_files_source_chapters_filter_restricts_to_matching_files(tmp_path):
    """feed-driven refresh's use case (refresh.py): point at a directory holding every chapter ever saved
    and only ingest the ones actually asked for."""
    (tmp_path / "Chapter 37.xml").write_bytes(XML37)
    (tmp_path / "Chapter 2.xml").write_bytes(XML2)
    src = MclXmlFilesSource(tmp_path, chapters=["2"])
    assert src.covered_chapters == {"2"}
    docs = list(src.iter_documents())
    assert docs and all(d.chapter == "2" for d in docs)


def test_xml_files_source_chapters_filter_no_match_raises(tmp_path):
    (tmp_path / "Chapter 37.xml").write_bytes(XML37)
    with pytest.raises(ValueError, match="matching chapters"):
        MclXmlFilesSource(tmp_path, chapters=["999"])


def test_xml_files_source_chapters_filter_pulls_in_the_whole_multichapter_file(tmp_path):
    """Asking for just one chapter that a MultiChapter file covers pulls in the whole file -- there is no
    cheaper unit to fetch or re-verify -- so covered_chapters ends up covering everything that file holds,
    a superset of what was actually asked for."""
    sections = _section_xml("760.1", "Short title.", "<Paragraph><P>Text.</P></Paragraph>") + \
        _section_xml("767A.1", "Definitions.", "<Paragraph><P>Text.</P></Paragraph>")
    (tmp_path / "Chapter 760.xml").write_bytes(_chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", True, sections))
    src = MclXmlFilesSource(tmp_path, chapters=["767A"])
    assert src.covered_chapters == {"760", "767A"}


def test_ingest_xml_chapter_end_to_end(conn, xml_dir):
    r_xml = run_ingest(conn, MclXmlFilesSource(xml_dir), now=T1)
    assert (r_xml.docs_seen, r_xml.docs_new) == (142, 142)
    d = service.get_document(conn, "MCL 37.2202")
    assert d["act_citation"] == "1976 PA 453"
    assert [x["citation"] for x in service.search(conn, "polygraph examination")["results"]][0].startswith("MCL 37.2")
    with pytest.raises(ValueError, match=r"MCL 37\.1 to MCL 37\.9.*Repealed\. 1976, Act 453"):
        service.get_document(conn, "MCL 37.5")


def test_integrity_problem_in_xml_aborts_the_run(conn, xml_dir):
    bad = XML37.decode("utf-16").replace("<MCLNumber>37.11</MCLNumber>", "<MCLNumber>BAD</MCLNumber>", 1)
    (xml_dir / "Chapter 37.xml").write_bytes(bad.encode("utf-16"))
    with pytest.raises(ChapterIntegrityError, match="unparseable section"):
        run_ingest(conn, MclXmlFilesSource(xml_dir), now=T1)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


# ---- MclXmlWebSource with a mock server -------------------------------------------------------------------
def make_client(handler):
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    return PoliteClient(transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: t["now"],
                        user_agent="test/1.0", min_interval=1.0)


LISTING = (
    '<html><body><pre>'
    ' 9/1/2026  1:00 AM        &lt;dir&gt; <A HREF="/documents/mcl/archive/">archive</A><br>'
    ' 2/6/2026  8:12 PM      1176978 <A HREF="/documents/mcl/Chapter%2037.xml">Chapter 37.xml</A><br>'
    '</pre></body></html>'
)


def test_xml_web_source_discovers_from_directory_listing_and_fetches():
    hits = []

    def handler(req):
        hits.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/documents/mcl/":
            return httpx.Response(200, text=LISTING)
        if req.url.path == "/documents/mcl/Chapter 37.xml":
            return httpx.Response(200, content=XML37)
        return httpx.Response(404)

    src = MclXmlWebSource(client=make_client(handler))
    docs = list(src.iter_documents())
    assert len(docs) == 142 and src.chapters_seen == ["37"]
    assert "/documents/mcl/" in hits and "/documents/mcl/Chapter 37.xml" in hits


def test_xml_web_source_explicit_chapters_skips_discovery():
    hits = []

    def handler(req):
        hits.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/documents/mcl/Chapter 37.xml":
            return httpx.Response(200, content=XML37)
        return httpx.Response(404)

    src = MclXmlWebSource(client=make_client(handler), chapters=["37"])
    assert src.covered_chapters == {"37"}
    docs = list(src.iter_documents())
    assert len(docs) == 142
    assert "/documents/mcl/" not in hits  # no directory listing fetched when chapters are given explicitly


def test_xml_web_source_rejects_wrong_chapter_in_response():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=XML37)  # chapter 37 returned for every request

    with pytest.raises(ChapterIntegrityError, match="requested chapter 38"):
        list(MclXmlWebSource(client=make_client(handler), chapters=["38"]).iter_documents())


def test_xml_web_source_wraps_a_raw_parse_failure_with_the_chapter_number():
    """A live crawl hit this for real (2026-09-22): a chapter file whose parse fails outright (here,
    parse_chapter_xml's ValueError for a file with no <Name>) previously escaped iter_documents with no
    chapter number anywhere in the exception, unlike every other integrity failure -- undiagnosable from
    hundreds of chapters without re-instrumenting and re-running the whole crawl."""
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content='<?xml version="1.0" encoding="utf-16"?><MCLChapterInfo/>'.encode("utf-16"))

    with pytest.raises(ChapterIntegrityError, match=r"chapter 38: ValueError: chapter XML file has no <Name>"):
        list(MclXmlWebSource(client=make_client(handler), chapters=["38"]).iter_documents())


def test_xml_web_source_resolves_a_multichapter_sub_chapter_to_its_real_file():
    """Feed-driven refresh's hard case: the feed flags "767A" specifically (a chapter with no file of its
    own -- it lives inside Chapter 760.xml, see _KNOWN_MULTI_CHAPTER_FILES). Before this was fixed, an
    explicit chapters=["767A"] request would have tried to fetch a nonexistent "Chapter 767A.xml" (or, for
    chapters=["760"], would have fetched the right file but declared covered_chapters={"760"} only and then
    raised a scope error on the very first section belonging to 761+). Now it correctly fetches the one
    real file and declares its whole real coverage up front."""
    sections = _section_xml("760.1", "Short title.", "<Paragraph><P>Text.</P></Paragraph>") + \
        _section_xml("767A.1", "Definitions.", "<Paragraph><P>Text.</P></Paragraph>")
    chapter_760_xml = _chapter_xml("760", "CODE OF CRIMINAL PROCEDURE", True, sections)

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/documents/mcl/Chapter 760.xml":
            return httpx.Response(200, content=chapter_760_xml)
        return httpx.Response(404)

    src = MclXmlWebSource(client=make_client(handler), chapters=["767A"])
    assert src.covered_chapters == {
        "760", "761", "762", "763", "764", "765", "766", "767", "767A", "768", "769", "770",
        "771", "771A", "772", "773", "774", "775", "776", "777",
    }
    docs = list(src.iter_documents())
    assert {d.citation for d in docs} == {"MCL 760.1", "MCL 767A.1"} and src.chapters_seen == ["760"]
