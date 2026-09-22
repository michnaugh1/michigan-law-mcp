from pathlib import Path

from mlaw.history import parse_history
from mlaw.mclsite import object_name, parse_act_page, parse_chapter_page, parse_currency, parse_disclaimer

PAGES = Path(__file__).parent / "data" / "pages"
ACT = (PAGES / "act-175-of-1927.html").read_text(encoding="utf-8")
CHAP = (PAGES / "chapters-760-777.html").read_text(encoding="utf-8")


def test_currency_banner():
    for html in (ACT, CHAP):
        c = parse_currency(html)
        assert c.through_act == "2026 PA 91"
        assert c.text == "Michigan Compiled Laws Complete Through PA 91 of 2026"


def test_disclaimer_footer():
    d = parse_disclaimer(ACT)
    assert "not intended to replace official versions" in d and "without warranties" in d


def test_chapter_page():
    p = parse_chapter_page(CHAP)
    assert (p.object_name, p.heading, p.title) == ("mcl-chapters-760-777", "Chapter 760", "CODE OF CRIMINAL PROCEDURE")
    assert (p.previous, p.next) == ("mcl-chap752", "mcl-chap780")
    assert p.download_url.endswith("Home/Document?objectName=mcl-chapters-760-777")
    (act,) = p.acts
    assert (act.object_name, act.type, act.first_citation, act.last_citation) == (
        "mcl-Act-175-of-1927", "Statute", "MCL 760.1", "MCL 777.69")


def test_act_page():
    p = parse_act_page(ACT)
    assert (p.object_name, p.title, p.act_citation) == ("mcl-Act-175-of-1927", "THE CODE OF CRIMINAL PROCEDURE", "1927 PA 175")
    assert p.chapter_object == "mcl-chapters-760-777"
    assert (p.previous, p.next) == ("mcl-E-R-O-No-2014-1", "mcl-Act-144-of-1937")  # EROs sit in the same sequence
    assert p.long_title.startswith("AN ACT to revise, consolidate, and codify") and p.long_title.endswith("provisions of this act.")
    assert p.download_url.endswith("Home/Document?objectName=mcl-Act-175-of-1927")
    assert p.warnings == []
    assert len(p.divisions) == 20 and {d.type for d in p.divisions} == {"Division"}
    first, last = p.divisions[0], p.divisions[-1]
    assert (first.object_name, first.first_citation, first.last_citation) == ("mcl-175-1927-TITLE-AND-CONSTRUCTION", "MCL 760.1", "MCL 760.2")
    assert (last.object_name, last.first_citation, last.last_citation) == ("mcl-175-1927-XVII", "MCL 777.1", "MCL 777.69")
    # division ranges are ordered and non-overlapping, and span the act's stated range (760.1 - 777.69)
    from mlaw.citations import citation_sort_key as k
    for a, b in zip(p.divisions, p.divisions[1:]):
        assert k(a.last_citation) < k(b.first_citation), (a.description, b.description)
    assert "VIIA" in " ".join(d.object_name for d in p.divisions)
    by_desc = {d.object_name.rsplit("-", 1)[1]: (d.first_citation, d.last_citation) for d in p.divisions}
    assert by_desc["VIIA"] == ("MCL 767A.1", "MCL 767A.9")          # chapter numbers can carry a letter
    assert by_desc["VI"] == ("MCL 766.1", "MCL 766.22")             # "(766.1...766.19-766.22)"
    assert by_desc["XI"] == ("MCL 771.1", "MCL 771.24")
    assert by_desc["XIA"] == ("MCL 771A.1", "MCL 771A.8")
    assert all(d.first_citation and d.last_citation for d in p.divisions)


def test_act_history_parsed():
    p = parse_act_page(ACT)
    assert [(h.kind, h.act_citation, h.immediate_effect, h.effective_iso) for h in p.history] == [
        ("enacted", "1927 PA 175", False, "1927-09-05"),
        ("amended", "1980 PA 506", True, "1981-01-22"),
        ("amended", "1994 PA 445", True, "1995-01-10"),
    ]


def test_history_unknown_shapes_kept_verbatim():
    h = parse_history("Am. 2018, Act 1, Eff. Sept. 1, 2018 ;-- Some future grammar we have not seen")
    assert h[0].effective_iso == "2018-09-01" and h[1].kind == "other" and h[1].raw.startswith("Some future")
    assert parse_history("Am. 2025, Act 9, Eff. 90 days after adjournment")[0].effective_iso is None


def test_object_name_helper():
    assert object_name("https://www.legislature.mi.gov/Laws/MCL?objectName=mcl-750-83") == "mcl-750-83"
    assert object_name("https://x/Laws/Index?ObjectName=mcl-chap450") == "mcl-chap450"
    assert object_name(None) is None and object_name("https://x/y") is None


# ---- division, section, document pages (saved from legislature.mi.gov) --------------------------------
from mlaw.mclsite import parse_division_page, parse_document_page, parse_section_page, section_from_object_name, act_from_object_name

SEC = (PAGES / "section-750-145p.html").read_text(encoding="utf-8")
REP = (PAGES / "section-750-171-repealed.html").read_text(encoding="utf-8")
DIV = (PAGES / "division-328-1931-XXA.html").read_text(encoding="utf-8")
DOC = (PAGES / "document-750-145p.html").read_text(encoding="utf-8")


def test_section_page_normal():
    p = parse_section_page(SEC)
    assert (p.object_name, p.citation, p.variant, p.status) == ("mcl-750-145p", "MCL 750.145p", "", "active")
    assert (p.act_name, p.act_citation, p.act_label) == ("THE MICHIGAN PENAL CODE", "1931 PA 328", "Act 328 of 1931")
    assert (p.chapter_object, p.act_object, p.division_object) == ("mcl-chap750", "mcl-Act-328-of-1931", "mcl-328-1931-XXA")
    assert (p.previous, p.next) == ("mcl-750-145o", "mcl-750-145q")
    assert p.catchline.startswith("Caregiver, other person with authority over vulnerable adult, or licensee;")
    assert p.catchline.endswith("felony; penalty.")
    assert p.text.startswith("Sec. 145p.\n(1) A caregiver, other person") and p.text.count("\n") == 14
    assert "(5) A caregiver" in p.text and p.text.endswith("or both.")
    assert p.history_note == "Add. 1994, Act 149, Eff. Oct. 1, 1994"
    assert p.compilers_notes == "" and p.effective_date is None and p.warnings == []


def test_section_page_repealed():
    p = parse_section_page(REP)
    assert (p.citation, p.status, p.effective_date, p.text) == ("MCL 750.171", "repealed", "2010-06-22", "")
    assert p.catchline == "Repealed. 2010, Act 96, Imd. Eff. June 22, 2010."
    assert p.compilers_notes == "Compiler's Notes: The repealed section pertained to engaging in or challenging to fight duel."
    assert p.division_object == "mcl-328-1931-XXX" and (p.previous, p.next) == ("mcl-750-170", "mcl-750-172")


def test_section_page_rejects_other_pages():
    import pytest
    with pytest.raises(ValueError):
        parse_section_page(ACT)


def test_division_page():
    p = parse_division_page(DIV)
    assert (p.object_name, p.heading, p.title) == ("mcl-328-1931-XXA", "Chapter XXA", "VULNERABLE ADULTS")
    assert (p.act_object, p.chapter_object) == ("mcl-Act-328-of-1931", "mcl-chap750")
    assert (p.previous, p.next) == ("mcl-328-1931-XX", "mcl-328-1931-XXI")
    assert [r.object_name for r in p.sections] == [f"mcl-750-145{c}" for c in "mnopqr"]
    assert p.sections[0].first_citation == "MCL 750.145m" and p.sections[0].description == "Definitions."
    assert {r.type for r in p.sections} == {"Section"}


def test_document_page_has_html_and_pdf_links():
    p = parse_document_page(DOC)
    assert p.pdf_url == "https://www.legislature.mi.gov/documents/mcl/pdf/MCL-750-145P.pdf"
    assert p.html_url.endswith("/Home/RenderDoc?objectName=mcl-750-145p")
    assert (p.previous, p.next) == ("mcl-750-145o", "mcl-750-145q")


def test_object_name_to_citation():
    assert section_from_object_name("mcl-750-145p") == ("MCL 750.145p", None)
    assert section_from_object_name("mcl-767A-1") == ("MCL 767A.1", None)
    assert section_from_object_name("mcl-333-16335-amended") == ("MCL 333.16335", "amended")
    assert section_from_object_name("mcl-333-5474c[1]") == ("MCL 333.5474c", "[1]")
    assert section_from_object_name("mcl-Act-175-of-1927") is None
    assert section_from_object_name("mcl-175-1927-II") is None
    assert act_from_object_name("mcl-Act-328-of-1931") == "1931 PA 328" and act_from_object_name("mcl-chap750") is None


def test_parsed_pages_flow_through_pipeline_and_service(conn):
    from mlaw import service
    from mlaw.pipeline import run_ingest

    class PagesSource:
        name, complete = "mcl", False
        def iter_documents(self):
            yield parse_section_page(SEC).to_parsed_doc()
            yield parse_section_page(REP).to_parsed_doc()
        def iter_public_acts(self):
            return []

    run_ingest(conn, PagesSource(), now="2026-09-21T12:00:00Z")
    d = service.get_document(conn, "MCL 750.145p")
    assert d["status"] == "active" and d["chapter"] == "750" and d["act_citation"] == "1931 PA 328"
    assert d["history_entries"] == [{"kind": "added", "act": "1994 PA 149", "immediate_effect": False,
                                     "effective_date": "1994-10-01", "effective_text": "Oct. 1, 1994", "raw": "Add. 1994, Act 149, Eff. Oct. 1, 1994"}]
    assert d["provenance"]["source_url"].endswith("objectName=mcl-750-145p")
    r = service.get_document(conn, "MCL 750.171")
    assert r["status"] == "repealed" and r["effective_date"] == "2010-06-22" and "duel" in r["compilers_notes"]
    # the "being section 400.11b of the Michigan Compiled Laws" citation inside 145p is captured
    refs = service.get_cross_references(conn, "MCL 750.145p")["references"]
    assert [x["citation"] for x in refs] == ["MCL 400.11b"] and refs[0]["in_database"] is False
    # repealed text is excluded from default search but the active section is found
    assert [x["citation"] for x in service.search(conn, "vulnerable adult")["results"]] == ["MCL 750.145p"]
    assert service.get_chapter_outline(conn, "750")["total"] == 2


# ---- chapter-level download page + daily bill status report --------------------------------------------
CHAP_DOC = (PAGES / "document-chap37.html").read_text(encoding="utf-8")


def test_chapter_download_page_offers_whole_chapter_html_and_pdf():
    p = parse_document_page(CHAP_DOC)
    assert p.object_name == "mcl-chap37" and p.label == "Chapter 37" and p.heading == "CIVIL RIGHTS"
    assert p.html_url.endswith("/Home/RenderDoc?objectName=mcl-chap37")
    assert p.pdf_url == "https://www.legislature.mi.gov/documents/mcl/pdf/MCL-CHAP37.pdf"
    assert (p.previous, p.next) == ("mcl-chap36", "mcl-chap38")


def test_section_download_page_label():
    p = parse_document_page(DOC)
    assert p.object_name == "mcl-750-145p" and p.label.startswith("Section")


def test_bill_status_report_parses_stages_and_targets():
    from mlaw.billstatus import parse_bill_status_report

    r = parse_bill_status_report((PAGES / "daily-bill-status-2026-09-21.html").read_text(encoding="utf-8"))
    assert (r.date_from, r.date_to) == ("2026-09-01", "2026-09-21")
    assert r.stages() == {"Introduced": 55, "Passed by Chamber": 26, "Enrolled": 12, "Adopted": 5}
    by = {x.label: x for x in r.rows if x.stage == "Enrolled"}
    sb22 = by["SB 0022 of 2025"]
    assert (sb22.object_name, sb22.type, sb22.number, sb22.is_bill) == ("2025-SB-0022", "SB", 22, True)
    assert sb22.amends_acts == ["1972 PA 348"] and sb22.amends_mcl == ["MCL 554.609"]
    assert by["SB 0527 of 2025"].amends_mcl == ["MCL 600.3208", "MCL 600.3212"]
    assert by["SB 1013 of 2026"].amends_mcl == ["MCL 500.2109", "MCL 500.2119"]  # "secs. 2109 & 2119"
    assert sb22.url.endswith("Home/GetObject?objectName=2025-SB-0022")
    intro = {x.label: x for x in r.rows if x.stage == "Introduced"}
    assert intro["SR 0136 of 2026"].is_bill is False
    assert intro["SB 1048 of 2026"].amends_ranges == [("MCL 460.1", "MCL 460.11")]  # act span, not sections
    assert intro["SB 1048 of 2026"].amends_mcl == []
    assert intro["SB 1144 of 2026"].amends_ranges == [("MCL 722.622", None)]  # "et seq."
    assert intro["SB 1050 of 2026"].creates_new_act is True
